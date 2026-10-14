"""HTTP tests for voice ingestion, suggestions, and isolated inspection jobs."""

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app import deps
from app.routers import ai


class Conn:
    def __init__(self):
        self.calls = []

    def transaction(self):
        return AsyncContext()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None


class AsyncContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None


class Pool:
    def __init__(self, *, order=None, rows=None, job=None, review=None, fail_execute=False):
        self.order = order
        self.rows = rows or []
        self.job = job or {"id": uuid.uuid4()}
        self.review = review
        self.fail_execute = fail_execute
        self.fetch_calls = []
        self.fetchrow_calls = []
        self.execute_calls = []
        self.conn = Conn()

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        return self.rows

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        if "INSERT INTO ai_jobs" in query:
            return self.job
        if "INSERT INTO voice_notes" in query:
            return self.job
        if "FROM ai_jobs" in query:
            return self.job
        if "FROM ai_reviews" in query:
            return self.review
        return self.order

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))
        if self.fail_execute:
            raise RuntimeError("database write failed")

    def acquire(self):
        return self.conn


@pytest.fixture
def api(monkeypatch):
    clients = []

    def create(pool=None, role="master", **fields):
        pool = pool or Pool()

        async def get_pool():
            return pool

        monkeypatch.setattr(ai, "get_pool", get_pool)
        monkeypatch.setattr(deps, "get_pool", get_pool)
        import app.db as db

        monkeypatch.setattr(db, "get_pool", get_pool)
        from app.main import create_app

        app = create_app()
        user = {
            "id": str(uuid.uuid4()),
            "role": role,
            "areaIds": [],
            "crewId": None,
            "lang": "ru",
            **fields,
        }
        app.dependency_overrides[deps.get_current_user] = lambda: user
        client = TestClient(app)
        clients.append(client)
        return client, pool, user

    yield create
    for client in clients:
        client.close()


def test_fault_suggestion_scores_matches_and_returns_ranked_alternatives(api):
    best_id, next_id = uuid.uuid4(), uuid.uuid4()
    pool = Pool(
        rows=[
            {"id": best_id, "code": "P-12", "fault_group": "Pump", "name": "Pump bearing noise"},
            {"id": next_id, "code": "P-13", "fault_group": "Pump", "name": "Bearing failure"},
            {"id": uuid.uuid4(), "code": "E-2", "fault_group": "Electric", "name": "Loose cable"},
        ]
    )
    client = api(pool, role="worker")[0]
    response = client.post(
        "/api/v1/ai/suggest-fault-code", json={"description": "pump bearing noise"}
    )
    assert response.status_code == 200
    assert response.json()["fault_code_id"] == str(best_id)
    assert response.json()["confidence"] == 0.81
    assert response.json()["alternatives"] == [{"id": str(next_id), "code": "P-13"}]
    pool.rows = []
    assert client.post(
        "/api/v1/ai/suggest-fault-code", json={"description": "unmatched event"}
    ).json() == {
        "fault_code_id": None,
        "confidence": 0.2,
        "alternatives": [],
    }


def test_fault_suggestion_is_restricted_to_worker_and_master(api):
    assert (
        api(role="manager")[0]
        .post("/api/v1/ai/suggest-fault-code", json={"description": "bearing"})
        .status_code
        == 403
    )


def test_assignee_suggestion_checks_area_and_uses_load_adjusted_rankings(api):
    area = uuid.uuid4()
    pool = Pool(
        rows=[
            {
                "id": uuid.uuid4(),
                "full_name": "Free",
                "specialty": "mechanic",
                "grade": "senior",
                "current_status": "free",
                "queue_count": 0,
            },
            {
                "id": uuid.uuid4(),
                "full_name": "Busy",
                "specialty": "electrician",
                "grade": "mid",
                "current_status": "busy",
                "queue_count": 4,
            },
        ]
    )
    client = api(pool, role="master", areaIds=[str(area)])[0]
    response = client.post("/api/v1/ai/suggest-assignee", json={"area_id": str(area)})
    assert response.status_code == 200
    suggestions = response.json()["suggestions"]
    assert suggestions[0]["employee_id"] == str(pool.rows[0]["id"])
    assert suggestions[0]["score"] > suggestions[1]["score"]
    assert pool.fetch_calls[-1][1] == (str(area),)
    denied = api(Pool(), role="master", areaIds=[])[0].post(
        "/api/v1/ai/suggest-assignee",
        json={"area_id": str(area)},
    )
    assert denied.status_code == 403


def test_recommend_worker_handles_missing_order_and_delegates_only_a_suggestion(api, monkeypatch):
    oid = uuid.uuid4()
    assert (
        api(Pool(), role="worker")[0]
        .post(f"/api/v1/ai/work-orders/{oid}/recommend-worker")
        .status_code
        == 403
    )
    assert (
        api(Pool(order=None), role="manager")[0]
        .post(f"/api/v1/ai/work-orders/{oid}/recommend-worker")
        .status_code
        == 404
    )
    area = uuid.uuid4()
    pool = Pool(
        order={"area_id": area, "equipment_id": None},
        rows=[
            {
                "id": uuid.uuid4(),
                "full_name": "Worker",
                "specialty": "mechanic",
                "grade": "mid",
                "current_status": "free",
                "queue_count": 0,
            },
        ],
    )
    client = api(pool, role="manager", lang="kk")[0]
    called = []

    async def recommendation(context, language):
        called.append((context, language))
        return {
            "recommended_worker_id": context["candidates"][0]["employee_id"],
            "provider": "mock",
        }

    monkeypatch.setattr("app.ai.worker_recommendation.recommend", recommendation)
    response = client.post(f"/api/v1/ai/work-orders/{oid}/recommend-worker")
    assert response.status_code == 200
    assert response.json()["data"]["suggestions"][0]["full_name"] == "Worker"
    assert called[0][1] == "kk"


def test_transcription_normalizes_language_persists_upload_and_rejects_bad_file(api, monkeypatch):
    import app.ai_providers as ai_providers
    import app.routers.ai as ai_module

    pool = Pool()
    client = api(pool, role="worker")[0]
    calls, stored = [], []

    async def transcribe(data, filename, language):
        calls.append((data, filename, language))
        return "насос проверен", "groq"

    monkeypatch.setattr(ai_providers, "transcribe_audio", transcribe)
    monkeypatch.setattr(ai_module, "get_pool", lambda: _immediate(pool))
    monkeypatch.setattr(
        "app.storage.put_bytes", lambda key, data, mime: stored.append((key, data, mime))
    )
    response = client.post(
        "/api/v1/ai/transcribe",
        data={"language": "KZ"},
        files={"file": ("voice.ogg", b"audio-data", "audio/ogg")},
    )
    assert response.status_code == 200
    assert response.json()["transcript"] == "насос проверен"
    assert response.json()["pending"] is False
    assert calls == [(b"audio-data", "voice.ogg", "ru")]
    assert stored[0][1:] == (b"audio-data", "audio/ogg")

    empty = client.post(
        "/api/v1/ai/transcribe",
        data={"language": "kk"},
        files={"file": ("voice.ogg", b"", "audio/ogg")},
    )
    assert empty.status_code == 422
    assert len(calls) == 1


async def _immediate(value):
    return value


def test_inspection_job_transitions_to_completed_or_failed_without_breaking_order(api, monkeypatch):
    import app.services.ai_review as ai_review

    oid = uuid.uuid4()
    pool = Pool(order={"status": "ai_review"}, job={"id": uuid.uuid4()})
    client = api(pool, role="worker")[0]

    async def success(conn, work_order_id):
        assert str(oid) == work_order_id
        return {"verdict": "accepted", "score": 90}

    monkeypatch.setattr(ai_review, "run_ai_review", success)
    response = client.post(f"/api/v1/ai/work-orders/{oid}/inspect")
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "COMPLETED"
    assert json.loads(pool.execute_calls[-1][1][1]) == {"verdict": "accepted", "score": 90}

    failing_pool = Pool(order={"status": "ai_review"}, job={"id": uuid.uuid4()})
    failing_client = api(failing_pool, role="worker")[0]

    async def failure(*_):
        raise RuntimeError("inference unavailable")

    monkeypatch.setattr(ai_review, "run_ai_review", failure)
    failed = failing_client.post(f"/api/v1/ai/work-orders/{oid}/inspect")
    assert failed.status_code == 200
    assert failed.json()["data"]["status"] == "FAILED"
    assert failed.json()["data"]["error"] == "AI_UNAVAILABLE"


def test_inspection_result_serializes_nested_uuid_and_alias_is_stable(api):
    oid, job_id, review_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    user_id = str(uuid.uuid4())
    pool = Pool(
        order={"id": oid, "assignee_id": user_id},
        job={"id": job_id, "state": "COMPLETED"},
        review={"id": review_id, "score": 90},
    )
    client = api(pool, role="worker", id=user_id)[0]
    result = client.get(f"/api/v1/ai/work-orders/{oid}/inspection").json()["data"]
    assert result["job"]["id"] == str(job_id)
    assert result["review"]["id"] == str(review_id)
    assert client.get("/api/v1/ai/insights").status_code == 200

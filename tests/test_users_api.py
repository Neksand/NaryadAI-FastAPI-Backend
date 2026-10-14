"""User profile and worker directory tests for role scope and privacy fields."""

import uuid

import pytest
from fastapi.testclient import TestClient

from app import deps
from app.routers import users


class Pool:
    def __init__(self, rows=None, row=None):
        self.rows = rows or []
        self.row = row
        self.fetch_calls = []
        self.fetchrow_calls = []
        self.execute_calls = []

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        return self.rows

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        return self.row

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))


@pytest.fixture
def api(monkeypatch):
    clients = []

    def create(pool=None, role="manager", **fields):
        pool = pool or Pool()

        async def get_pool():
            return pool

        monkeypatch.setattr(users, "get_pool", get_pool)
        from app.main import create_app

        app = create_app()
        user = {"id": str(uuid.uuid4()), "role": role, "areaIds": [], "crewId": None, **fields}
        app.dependency_overrides[deps.get_current_user] = lambda: user
        client = TestClient(app)
        clients.append(client)
        return client, pool, user

    yield create
    for client in clients:
        client.close()


def worker_row(worker_id=None):
    return {
        "id": worker_id or uuid.uuid4(),
        "login": "worker-1",
        "full_name": "Worker One",
        "role": "worker",
        "specialty": "mechanic",
        "grade": "senior",
        "crew_id": uuid.uuid4(),
        "shift": "day",
        "current_status": "available",
        "lang": "kk",
        "pin_hash": "never expose",
    }


def test_worker_directory_scopes_workers_and_applies_limit_by_role(api):
    row = worker_row()
    pool = Pool(rows=[row])
    master = api(pool, role="master", areaIds=["area-1"])[0]
    response = master.get("/api/v1/workers", params={"limit": 17})
    assert response.status_code == 200
    query, args = pool.fetch_calls[-1]
    assert "employee_areas" in query and args == (["area-1"], 17)
    assert response.json()["data"][0]["crew_id"] == str(row["crew_id"])
    assert "pin_hash" not in response.text

    manager = api(pool, role="manager")[0]
    manager.get("/api/v1/workers", params={"limit": 9})
    assert pool.fetch_calls[-1][1] == (9,)

    worker = api(pool, role="worker", id=str(row["id"]))[0]
    worker.get("/api/v1/workers")
    assert pool.fetch_calls[-1][1] == (str(row["id"]),)


@pytest.mark.parametrize("limit", [0, 501, -2])
def test_worker_directory_rejects_out_of_range_page_sizes(api, limit):
    client, _, _ = api()
    assert client.get("/api/v1/workers", params={"limit": limit}).status_code == 422


def test_me_profiles_serialize_optional_fields_and_handle_missing_worker(api):
    row = worker_row()
    client, pool, user = api(Pool(row=row), role="worker", id=str(row["id"]))
    profile = client.get("/api/v1/workers/me")
    assert profile.status_code == 200
    data = profile.json()["data"]
    assert data["language"] == "kk" and data["crew_id"] == str(row["crew_id"])
    assert "pin_hash" not in profile.text
    assert client.get("/api/v1/users/me").json()["data"]["full_name"] == "Worker One"
    assert pool.fetchrow_calls[0][1] == (user["id"],)

    missing = api(Pool(), role="worker")[0].get("/api/v1/workers/me")
    assert missing.status_code == 404


def test_worker_by_id_is_role_gated_and_returns_not_found_after_scope_check(api):
    worker_id = uuid.uuid4()
    row = worker_row(worker_id)
    assert (
        api(Pool(row=row), role="worker")[0].get(f"/api/v1/workers/{worker_id}").status_code == 403
    )
    result = api(Pool(row=row), role="admin")[0].get(f"/api/v1/workers/{worker_id}")
    assert result.status_code == 200 and result.json()["data"]["id"] == str(worker_id)
    assert api(Pool(), role="manager")[0].get(f"/api/v1/workers/{worker_id}").status_code == 404


@pytest.mark.parametrize("language", ["ru", "kk"])
def test_language_preference_update_accepts_supported_languages(api, language):
    client, pool, user = api(role="worker")
    response = client.patch("/api/v1/users/me/preferences", json={"language": language})
    assert response.status_code == 200 and response.json()["data"]["language"] == language
    assert pool.execute_calls[-1][1] == (user["id"], language)


@pytest.mark.parametrize("language", ["en", "de", "", None])
def test_language_preference_rejects_unsupported_languages(api, language):
    client, pool, _ = api(role="worker")
    response = client.patch("/api/v1/users/me/preferences", json={"language": language})
    assert response.status_code == 422
    assert pool.execute_calls == []

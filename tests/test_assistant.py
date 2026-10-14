"""Assistant chat endpoint tests with a deterministic async pool double."""

import uuid

import pytest
from fastapi.testclient import TestClient

from app import deps
from app.routers import assistant

URL = "/api/v1/assistant/chat"


class Pool:
    def __init__(self, rows=None, row=None):
        self.rows = rows or []
        self.row = row
        self.fetch_calls = []
        self.fetchrow_calls = []

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        return self.rows

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        return self.row


@pytest.fixture
def make_client(monkeypatch):
    clients = []

    def create(pool=None, role="manager", use_auth_override=True, **user_fields):
        pool = pool if pool is not None else Pool()

        async def get_pool():
            return pool

        monkeypatch.setattr(assistant, "get_pool", get_pool)
        from app.main import create_app

        app = create_app()
        app.include_router(assistant.router, prefix="/api/v1")
        if use_auth_override:
            user = {"id": str(uuid.uuid4()), "role": role, "areaIds": [], **user_fields}
            app.dependency_overrides[deps.get_current_user] = lambda: user
        client = TestClient(app)
        clients.append((client, pool))
        return client, pool

    yield create
    for client, _ in clients:
        client.close()


def _ids(n=2):
    return [uuid.uuid4() for _ in range(n)]


def test_overdue_ru_returns_top5_with_real_citations(make_client):
    ids = _ids(2)
    pool = Pool(rows=[{"id": ids[0], "number": 101}, {"id": ids[1], "number": 102}])
    client, _ = make_client(pool)
    response = client.post(URL, json={"question": "Какие наряды просрочены?"})
    assert response.status_code == 200
    body = response.json()
    assert body["confidence"] == 1.0
    assert body["citations"] == [{"work_order_id": str(ids[0])}, {"work_order_id": str(ids[1])}]
    assert "101" in body["answer_text"] and "102" in body["answer_text"]


def test_overdue_kk_matches_merzim(make_client):
    oid = uuid.uuid4()
    pool = Pool(rows=[{"id": oid, "number": 77}])
    client, _ = make_client(pool)
    response = client.post(
        URL, json={"question": "Мерзімі өткен нарядтар қайсы?"}, headers={"Accept-Language": "kk"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["confidence"] == 1.0
    assert body["citations"] == [{"work_order_id": str(oid)}]
    assert "77" in body["answer_text"]
    assert "Мерзімі" in body["answer_text"]


def test_overdue_empty_reports_no_overdue(make_client):
    client, _ = make_client(Pool(rows=[]))
    body = client.post(URL, json={"question": "что просрочено?"}).json()
    assert body["citations"] == [] and body["confidence"] == 1.0
    assert "нет" in body["answer_text"].lower()


def test_free_ru_lists_executors(make_client):
    pool = Pool(
        rows=[
            {"id": uuid.uuid4(), "full_name": "Иван Петров"},
            {"id": uuid.uuid4(), "full_name": "Айдар Нурланов"},
        ]
    )
    client, _ = make_client(pool)
    body = client.post(URL, json={"question": "Кто сейчас свободен?"}).json()
    assert body["confidence"] == 1.0 and body["citations"] == []
    assert "Иван Петров" in body["answer_text"]


def test_free_kk_matches_bos(make_client):
    pool = Pool(rows=[{"id": uuid.uuid4(), "full_name": "Ерлан Сериков"}])
    client, _ = make_client(pool)
    body = client.post(
        URL,
        json={"question": "Бос орындаушылар кімдер?"},
        headers={"Accept-Language": "kk"},
    ).json()
    assert body["confidence"] == 1.0
    assert "Ерлан Сериков" in body["answer_text"]
    assert "Бос" in body["answer_text"]


def test_shift_ru_reports_counters(make_client):
    pool = Pool(row={"issued": 5, "closed": 3, "overdue": 2})
    client, _ = make_client(pool)
    body = client.post(URL, json={"question": "Итоги смены?"}).json()
    assert body["confidence"] == 1.0 and body["citations"] == []
    assert "5" in body["answer_text"] and "3" in body["answer_text"]
    assert "2" in body["answer_text"] and "Смена" in body["answer_text"]


def test_shift_kk_matches_auysym(make_client):
    pool = Pool(row={"issued": 4, "closed": 4, "overdue": 0})
    client, _ = make_client(pool)
    body = client.post(
        URL, json={"question": "Ауысым қорытындысы қандай?"}, headers={"Accept-Language": "kk"}
    ).json()
    assert body["confidence"] == 1.0
    assert "Ауысым" in body["answer_text"]


def test_rating_ru_top3_by_closed(make_client):
    pool = Pool(
        rows=[
            {"id": uuid.uuid4(), "name": "Петров", "closed": 10},
            {"id": uuid.uuid4(), "name": "Сидоров", "closed": 7},
            {"id": uuid.uuid4(), "name": "Ким", "closed": 5},
        ]
    )
    client, _ = make_client(pool)
    body = client.post(URL, json={"question": "Покажи рейтинг исполнителей"}).json()
    assert body["confidence"] == 1.0 and body["citations"] == []
    assert "Петров" in body["answer_text"] and "10" in body["answer_text"]


def test_rating_kk(make_client):
    pool = Pool(rows=[{"id": uuid.uuid4(), "name": "Асанов", "closed": 9}])
    client, _ = make_client(pool)
    body = client.post(
        URL, json={"question": "Орындаушылар рейтингі"}, headers={"Accept-Language": "kk"}
    ).json()
    assert body["confidence"] == 1.0
    assert "Асанов" in body["answer_text"]


def test_anomalies_ru_headlines_and_order_citations(make_client):
    oid1, oid2 = _ids(2)
    pool = Pool(
        rows=[
            {"headline": "Повторные отказы насоса", "order_ids": [oid1, oid2]},
            {"headline": "Рост внеплановых", "order_ids": []},
        ]
    )
    client, _ = make_client(pool)
    body = client.post(URL, json={"question": "Какие аномалии найдены?"}).json()
    assert body["confidence"] == 1.0
    assert "Повторные отказы насоса" in body["answer_text"]
    assert body["citations"] == [{"work_order_id": str(oid1)}, {"work_order_id": str(oid2)}]


def test_anomalies_kk(make_client):
    pool = Pool(rows=[{"headline": "Сорғы ақаулары", "order_ids": []}])
    client, _ = make_client(pool)
    body = client.post(
        URL, json={"question": "Соңғы аномалиялар бар ма?"}, headers={"Accept-Language": "kk"}
    ).json()
    assert body["confidence"] == 1.0
    assert "Сорғы ақаулары" in body["answer_text"]


def test_fallback_ru_is_honest_with_zero_confidence(make_client):
    client, pool = make_client(Pool())
    body = client.post(URL, json={"question": "Сколько планет в системе?"}).json()
    assert body == {
        "answer_text": "Не понял вопрос. Спросите про сроки, смену, рейтинг или аномалии.",
        "citations": [],
        "confidence": 0.0,
    }
    assert pool.fetch_calls == [] and pool.fetchrow_calls == []


def test_fallback_kk(make_client):
    client, _ = make_client(Pool())
    body = client.post(
        URL, json={"question": "What is the weather?"}, headers={"Accept-Language": "kk-KZ"}
    ).json()
    assert body["confidence"] == 0.0 and body["citations"] == []
    assert "түсінбедім" in body["answer_text"]


def test_area_filter_is_bound_value(make_client):
    area_id = uuid.uuid4()
    oid = uuid.uuid4()
    pool = Pool(rows=[{"id": oid, "number": 5}])
    client, _ = make_client(pool, role="manager")
    response = client.post(
        URL, json={"question": "что просрочено?", "filters": {"area_id": str(area_id)}}
    )
    assert response.status_code == 200
    query, args = pool.fetch_calls[-1]
    assert str(area_id) not in query
    assert args == (str(area_id),)


def test_worker_is_forbidden(make_client):
    client, pool = make_client(Pool(), role="worker")
    response = client.post(URL, json={"question": "что просрочено?"})
    assert response.status_code == 403
    assert pool.fetch_calls == [] and pool.fetchrow_calls == []


def test_missing_token_is_unauthorized(make_client):
    client, _ = make_client(Pool(), use_auth_override=False)
    response = client.post(URL, json={"question": "что просрочено?"})
    assert response.status_code == 401

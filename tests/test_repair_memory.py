"""Similar-repairs memory: pure ranking + HTTP scope/role tests."""

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app import deps
from app.ai.worker_recommendation import explain_candidate
from app.routers import work_orders
from app.services.repair_memory import rank_similar_repairs


def _candidate(**overrides):
    base = {
        "id": str(uuid.uuid4()),
        "number": 10,
        "fault_code_id": None,
        "work_done_text": None,
        "materials": None,
        "return_count": 0,
        "closed_at": "2026-01-01T00:00:00+00:00",
    }
    base.update(overrides)
    return base


def test_rank_prefers_same_fault_and_prepends_its_reason():
    fault = uuid.uuid4()
    same = _candidate(fault_code_id=str(fault), work_done_text="заменили подшипник")
    other = _candidate(fault_code_id=None, work_done_text=None)
    ranked = rank_similar_repairs([other, same], fault_code_id=fault, limit=5)
    assert ranked[0]["id"] == same["id"]
    assert ranked[0]["match_reasons"][0] == "тот же шифр неисправности"
    assert ranked[0]["match_score"] > ranked[1]["match_score"]


def test_rank_parses_json_materials_and_never_invents_missing_fields():
    with_materials = _candidate(
        materials=json.dumps([{"name": "Сальник", "quantity": 2, "unit": "шт"}])
    )
    empty = _candidate(materials=None, return_count=1)
    ranked = rank_similar_repairs([with_materials, empty])
    assert ranked[0]["materials"] == [{"name": "Сальник", "quantity": 2, "unit": "шт"}]
    assert ranked[0]["match_score"] == 0.4
    assert "указаны израсходованные материалы" in ranked[0]["match_reasons"]
    assert ranked[1]["materials"] == []
    assert "закрыт без доработок" not in ranked[1]["match_reasons"]


def test_rank_localizes_reasons_and_respects_limit():
    cands = [_candidate() for _ in range(5)]
    ranked = rank_similar_repairs(cands, limit=2, lang="kk")
    assert len(ranked) == 2
    assert ranked[0]["match_reasons"][0] == "осы жабдықтың жөндеуі"
    assert rank_similar_repairs(cands, limit=0) == []


def test_explain_candidate_is_status_and_load_aware():
    assert explain_candidate("free", 0) == ["свободен сейчас", "низкая текущая загрузка"]
    assert explain_candidate("busy", 6) == [
        "занят текущим нарядом",
        "высокая загрузка: 6 в очереди",
    ]
    assert explain_candidate("unknown-status", 2, "kk") == ["ауысымда емес"]


class Pool:
    def __init__(self, *, equipment=None, rows=None):
        self.equipment = equipment
        self.rows = rows or []
        self.fetch_calls = []
        self.fetchrow_calls = []

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        return self.equipment

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        return self.rows


@pytest.fixture
def api(monkeypatch):
    clients = []

    def create(pool, role="master", **fields):
        async def get_pool():
            return pool

        monkeypatch.setattr(work_orders, "get_pool", get_pool)
        from app.main import create_app

        app = create_app()
        user = {"id": str(uuid.uuid4()), "role": role, "areaIds": [], "lang": "ru", **fields}
        app.dependency_overrides[deps.get_current_user] = lambda: user
        client = TestClient(app)
        clients.append(client)
        return client, user

    yield create
    for client in clients:
        client.close()


def test_similar_repairs_scopes_area_and_ranks(api):
    area, equipment = str(uuid.uuid4()), str(uuid.uuid4())
    fault = uuid.uuid4()
    pool = Pool(
        equipment={"id": equipment, "area_id": area},
        rows=[
            _candidate(fault_code_id=str(fault), work_done_text="работы", materials="[]"),
            _candidate(fault_code_id=None),
            _candidate(fault_code_id=str(fault)),
        ],
    )
    client, _ = api(pool, role="master", areaIds=[area])
    response = client.get(
        f"/api/v1/equipment/{equipment}/similar-repairs",
        params={"fault_code_id": str(fault), "limit": 2},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["equipment_id"] == equipment
    assert len(body["items"]) == 2
    assert all(item["match_reasons"][0] == "тот же шифр неисправности" for item in body["items"])
    query, args = pool.fetch_calls[-1]
    assert args[0] == equipment and "done" in args[1]


def test_similar_repairs_denies_worker_and_out_of_area_master(api):
    area, equipment = str(uuid.uuid4()), str(uuid.uuid4())
    pool = Pool(equipment={"id": equipment, "area_id": area}, rows=[_candidate()])
    assert (
        api(pool, role="worker")[0]
        .get(f"/api/v1/equipment/{equipment}/similar-repairs")
        .status_code
        == 403
    )
    out = api(
        Pool(equipment={"id": equipment, "area_id": area}),
        role="master",
        areaIds=[str(uuid.uuid4())],
    )[0]
    assert out.get(f"/api/v1/equipment/{equipment}/similar-repairs").status_code == 404
    missing = api(Pool(equipment=None), role="manager")[0]
    assert missing.get(f"/api/v1/equipment/{equipment}/similar-repairs").status_code == 404

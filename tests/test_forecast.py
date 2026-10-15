"""Failure-forecast scoring and endpoint tests with a deterministic pool double."""

import uuid

import pytest
from fastapi.testclient import TestClient

from app import deps
from app.routers import forecast
from app.routers.forecast import criticality_weight, forecast_growth, score_forecast


class Pool:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.fetch_calls = []

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        return self.rows


@pytest.fixture
def make_client(monkeypatch):
    clients = []

    def create(pool, role="manager", **user_fields):
        async def get_pool():
            return pool

        monkeypatch.setattr(forecast, "get_pool", get_pool)
        from app.main import create_app

        app = create_app()
        app.include_router(forecast.router, prefix="/api/v1")
        user = {"id": str(uuid.uuid4()), "role": role, "areaIds": [], **user_fields}
        app.dependency_overrides[deps.get_current_user] = lambda: user
        client = TestClient(app)
        clients.append(client)
        return client

    yield create
    for client in clients:
        client.close()


def row(name, recent, prev, crit="B"):
    return {
        "id": uuid.uuid4(),
        "name": name,
        "criticality": crit,
        "recent": recent,
        "prev": prev,
    }


def test_score_zero_history_matches_base_plus_c_weight():
    assert score_forecast(0, 0, "C") == {"probability": 0.19, "growth": 0.0}


def test_score_clamps_low_for_declining_unplanned():
    scored = score_forecast(0, 30, "C")
    assert scored["growth"] == pytest.approx(-0.968, abs=1e-3)
    assert scored["probability"] == 0.05


def test_score_clamps_high_for_surge_on_critical_equipment():
    scored = score_forecast(30, 0, "A")
    assert scored["growth"] == 1.0
    assert scored["probability"] == 0.95


def test_growth_is_capped_at_one():
    assert forecast_growth(50, 0) == 1.0
    assert forecast_growth(3, 0) == 1.0


def test_growth_is_negative_when_unplanned_decline():
    assert forecast_growth(0, 9) == pytest.approx(-0.9)
    assert forecast_growth(1, 3) == pytest.approx(-0.5)


def test_criticality_weights_are_ordered_a_above_b_above_c():
    assert criticality_weight("A") > criticality_weight("B") > criticality_weight("C")
    assert criticality_weight("A") == 3.0
    assert criticality_weight("B") == 2.0
    assert criticality_weight("C") == 1.0
    assert score_forecast(2, 2, "A")["probability"] > score_forecast(2, 2, "C")["probability"]


def test_unknown_criticality_defaults_to_c_weight():
    assert criticality_weight("Z") == 1.0
    assert score_forecast(2, 2, "Z") == score_forecast(2, 2, "C")


def test_endpoint_returns_sorted_forecast_matching_pure_score(make_client):
    pool = Pool(
        rows=[
            row("Конвейер К-3", 8, 1, "A"),
            row("Насос", 0, 0, "C"),
            row("Дробилка", 3, 3, "B"),
        ]
    )
    response = make_client(pool).get("/api/v1/analytics/failure-forecast")

    assert response.status_code == 200
    items = response.json()
    assert [i["equipment_name"] for i in items] == ["Конвейер К-3", "Дробилка", "Насос"]
    for item in items:
        assert set(item) == {
            "equipment_id",
            "equipment_name",
            "probability",
            "recent",
            "growth",
            "score_kind",
        }
    top = items[0]
    assert top["recent"] == 8
    assert top == {
        "equipment_id": str(pool.rows[0]["id"]),
        "equipment_name": "Конвейер К-3",
        "recent": 8,
        "score_kind": "heuristic_risk_index",
        **score_forecast(8, 1, "A"),
    }
    probs = [i["probability"] for i in items]
    assert probs == sorted(probs, reverse=True)


def test_endpoint_forbids_worker_role(make_client):
    response = make_client(Pool(), role="worker").get("/api/v1/analytics/failure-forecast")
    assert response.status_code == 403


def test_endpoint_returns_empty_list_without_equipment(make_client):
    response = make_client(Pool(rows=[])).get("/api/v1/analytics/failure-forecast")
    assert response.status_code == 200
    assert response.json() == []


def test_endpoint_query_aggregates_unplanned_windows(make_client):
    pool = Pool(rows=[row("Станок", 1, 0)])
    response = make_client(pool).get("/api/v1/analytics/failure-forecast")

    assert response.status_code == 200
    query, args = pool.fetch_calls[-1]
    assert "kind='unplanned'" in query
    assert "30 days" in query and "60 days" in query
    assert "equipment" in query
    assert args == ()


@pytest.mark.parametrize("areas", [[], [str(uuid.uuid4())]])
def test_master_forecast_is_restricted_to_assigned_areas(make_client, areas):
    pool = Pool()
    response = make_client(pool, role="master", areaIds=areas).get(
        "/api/v1/analytics/failure-forecast"
    )
    assert response.status_code == 200
    query, args = pool.fetch_calls[-1]
    assert "e.area_id = ANY($1::uuid[])" in query
    assert args == (areas,)

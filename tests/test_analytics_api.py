"""Analytics HTTP tests for scope, formulas, validation, and empty-data behavior."""

import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import deps
from app.routers import analytics


class AnalyticsPool:
    def __init__(self):
        self.fetch_calls = []
        self.fetchrow_calls = []
        self.fetchval_calls = []
        self.execute_calls = []
        self.rows = []
        self.row = None
        self.aggregate = {
            "total_orders": 4,
            "completed_orders": 3,
            "unplanned_orders": 1,
            "norm_hours": 6,
            "completed_without_norm": 0,
            "downtime_hours": 2,
        }
        self.materials = []

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        if "material_writeoffs" in query:
            return self.materials
        return self.rows

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        if "AS total_orders" in query:
            return self.aggregate
        if "AS reviews" in query:
            return self.row or {"reviews": 0, "passed": 0, "failed": 0, "avg_score": None}
        if "AS rework_rate" in query:
            return {"rework_rate": 0.25, "orders": 12}
        if "AS total FROM work_orders WHERE equipment_id" in query and "GROUP" not in query:
            return self.row
        if "AS total" in query:
            return self.row or {"total": 0}
        if "AS hours" in query:
            return self.row or {"hours": 0.0}
        return self.row

    async def fetchval(self, query, *args):
        self.fetchval_calls.append((query, args))
        return 100

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))


@pytest.fixture
def api(monkeypatch):
    clients = []

    def create(pool=None, role="manager", **fields):
        pool = pool or AnalyticsPool()

        async def get_pool():
            return pool

        monkeypatch.setattr(analytics, "get_pool", get_pool)
        from app.main import create_app

        app = create_app()
        user = {"id": str(uuid.uuid4()), "role": role, "areaIds": [], **fields}
        app.dependency_overrides[deps.get_current_user] = lambda: user
        client = TestClient(app)
        clients.append(client)
        return client, pool, user

    yield create
    for client in clients:
        client.close()


def test_dashboard_and_overview_scope_master_and_forward_date_filters(api):
    pool = AnalyticsPool()
    client, _, user = api(pool, role="master", areaIds=["area-a", "area-b"])
    dashboard = client.get(
        "/api/v1/analytics/dashboard", params={"from": "2026-01-01", "to": "2026-02-01"}
    )
    overview = client.get(
        "/api/v1/analytics/overview", params={"from": "2026-01-01", "to": "2026-02-01"}
    )

    assert dashboard.status_code == 200 and overview.status_code == 200
    for query, args in pool.fetchrow_calls:
        assert "area_id = ANY($3::uuid[])" in query
        assert args == ("2026-01-01", "2026-02-01", user["areaIds"])


def test_role_gated_analytics_reject_worker_but_worker_rating_endpoint_remains_readable(api):
    client, _, _ = api(role="worker")
    assert client.get("/api/v1/analytics/dashboard").status_code == 403
    assert client.get("/api/v1/analytics/ratings").status_code == 200


def test_basic_analytics_lists_serialize_database_ids_and_bound_trend_window(api):
    area_id, equipment_id = uuid.uuid4(), uuid.uuid4()
    pool = AnalyticsPool()
    pool.rows = [{"id": area_id, "name": "A", "orders": 3}]
    pool.row = {"total": 8}
    client, _, _ = api(pool)

    assert client.get("/api/v1/analytics/areas").json()["items"][0]["id"] == str(area_id)
    assert client.get(f"/api/v1/analytics/areas/{area_id}").json() == {
        "area_id": str(area_id),
        "total": 8,
    }
    pool.rows = [{"id": equipment_id, "name": "Pump", "unplanned": 2}]
    assert client.get("/api/v1/analytics/equipment/ranking").json()["items"][0]["id"] == str(
        equipment_id
    )
    pool.row = None
    assert client.get(f"/api/v1/analytics/equipment/{equipment_id}").status_code == 404
    pool.rows = [{"day": date(2026, 1, 1), "created": 2, "closed": 1}]
    trend = client.get("/api/v1/analytics/trends", params={"days": 999})
    assert trend.status_code == 200
    assert trend.json()["data"] == [{"day": "2026-01-01", "created": 2, "closed": 1}]
    assert pool.fetch_calls[-1][1] == (120,)


def test_daily_trends_are_limited_to_the_master_area_scope(api):
    pool = AnalyticsPool()
    client, _, user = api(pool, role="master", areaIds=[str(uuid.uuid4()), str(uuid.uuid4())])

    response = client.get("/api/v1/analytics/trends", params={"days": 14})

    assert response.status_code == 200
    query, args = pool.fetch_calls[-1]
    assert query.count("area_id = ANY($2::uuid[])") == 2
    assert args == (14, user["areaIds"])


def test_patterns_downtime_materials_and_work_order_status_endpoints(api):
    pool = AnalyticsPool()
    pool.row = {"hours": 1.75}
    pool.rows = [{"name": "Filter", "qty": 2.5}]
    pool.materials = [{"name": "Filter", "qty": 2.5}]
    client, _, _ = api(pool)
    assert client.get("/api/v1/analytics/patterns").json() == {"items": []}
    assert client.get("/api/v1/analytics/downtime").json()["downtime_hours"] == 1.75
    assert client.get("/api/v1/analytics/materials").json()["items"][0]["qty"] == 2.5
    assert client.get("/api/v1/analytics/work-orders").status_code == 200
    pool.rows = [{"id": uuid.uuid4(), "name": "Pump", "unplanned": 2}]
    assert client.get("/api/v1/analytics/equipment").status_code == 200


def test_insights_filters_serialize_uuid_and_status_patch_validates(api):
    insight_id = uuid.uuid4()
    pool = AnalyticsPool()
    pool.rows = [{"id": insight_id, "headline": "Review", "status": "new"}]
    client, _, _ = api(pool)
    response = client.get("/api/v1/analytics/insights", params={"status": "new"})
    assert response.json()["items"][0]["id"] == str(insight_id)
    assert pool.fetch_calls[-1][1] == ("new",)
    assert (
        client.patch(f"/api/v1/analytics/insights/{insight_id}", json={"status": "bad"}).status_code
        == 422
    )
    assert client.patch(
        f"/api/v1/analytics/insights/{insight_id}", json={"status": "seen"}
    ).json() == {
        "id": str(insight_id),
        "status": "seen",
    }


def test_ratings_validates_group_calculates_weighted_score_and_marks_low_sample(api):
    pool = AnalyticsPool()
    pool.rows = [
        {
            "gid": uuid.UUID(int=1),
            "closed": 2,
            "total": 3,
            "quality": 80,
            "on_time": 1,
            "rework": 0,
            "rejected": 0,
        },
        {
            "gid": uuid.UUID(int=2),
            "closed": 40,
            "total": 42,
            "quality": 100,
            "on_time": 1,
            "rework": 0,
            "rejected": 0,
        },
    ]
    client, _, _ = api(pool)
    assert client.get("/api/v1/analytics/ratings", params={"group_by": "squad"}).status_code == 422
    response = client.get("/api/v1/analytics/ratings", params={"group_by": "employee"})
    data = response.json()["data"]
    assert data[0]["id"] == str(uuid.UUID(int=2))
    assert data[0]["rating"] == 100 and data[0]["low_data"] is False
    assert data[1]["rating"] == 78.8 and data[1]["low_data"] is True
    assert data[1]["formula"].startswith("100*(quality*.35")
    assert client.get(f"/api/v1/analytics/ratings/{uuid.uuid4()}").status_code == 404


def test_worker_rating_is_limited_to_self_and_returns_formula_components(api):
    worker_id = uuid.uuid4()
    pool = AnalyticsPool()
    pool.rows = [
        {
            "gid": worker_id,
            "name": "Worker",
            "closed": 10,
            "total": 12,
            "quality": 80,
            "on_time": 0.9,
            "rework": 0.1,
            "rejected": 0.05,
        }
    ]
    client, _, user = api(pool, role="worker", id=str(worker_id))

    response = client.get("/api/v1/analytics/ratings/me")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["id"] == str(worker_id)
    assert data["name"] == "Worker"
    assert data["rating"] == 77.0
    assert data["volume"] == 0.25
    assert data["rejected"] == 0.05
    query, args = pool.fetch_calls[-1]
    assert "($3::uuid IS NULL OR w.assignee_id = $3::uuid)" in query
    assert args == (None, None, user["id"])

    assert client.get(f"/api/v1/analytics/ratings/{uuid.uuid4()}").status_code == 403
    assert client.get("/api/v1/analytics/ratings", params={"group_by": "crew"}).status_code == 403


def test_analytics_sql_identifiers_are_whitelisted_and_values_stay_bound(api):
    client, pool, _ = api()
    injection = "employee) UNION SELECT pg_sleep(10)--"
    response = client.get("/api/v1/analytics/ratings", params={"group_by": injection})
    assert response.status_code == 422
    assert pool.fetch_calls == []
    assert (
        client.get("/api/v1/analytics/insights", params={"status": "new' OR 1=1 --"}).status_code
        == 422
    )

    # User-provided filters are passed as bind arguments, never pasted into SQL.
    status = "new"
    client.get("/api/v1/analytics/insights", params={"status": status})
    query, args = pool.fetch_calls[-1]
    assert status not in query
    assert args == (status,)


ANALYTICS_FILTER_ATTACKS = [
    "employee' OR 1=1 --",
    "crew); DROP TABLE employees;--",
    "UNION SELECT pg_sleep(10)",
    "pg_catalog.pg_tables",
    "' OR 'x'='x",
    "new; UPDATE insights SET status='seen'",
    "in_plan/*x*/",
    "dismissed--",
    "NULL",
    "1=1",
    "false",
    "true",
    "${7*7}",
    "status::text",
    "new%27%20OR%201%3D1",
    "' UNION ALL SELECT NULL",
    "new; SELECT version()",
    "' AND 1=CAST(1 AS INT)--",
    "in_plan OR TRUE",
    """new\" OR \"1\"=\"1""",
    "insights#comment",
    "to_regclass('insights')",
    "0; COMMIT",
    "employee,crew",
    "new'/**/OR/**/'1'='1",
    "\x00",
    "🧪; DROP SCHEMA public CASCADE",
]


@pytest.mark.parametrize(
    "field,endpoint",
    [
        ("group_by", "/api/v1/analytics/ratings"),
        ("status", "/api/v1/analytics/insights"),
    ],
)
@pytest.mark.parametrize("payload", ANALYTICS_FILTER_ATTACKS)
def test_analytics_dynamic_filters_reject_untrusted_sql_tokens(api, field, endpoint, payload):
    client, pool, _ = api()
    response = client.get(endpoint, params={field: payload})
    assert response.status_code == 422
    assert pool.fetch_calls == []


def test_quality_handles_no_reviews_without_division_by_zero(api):
    client, _, _ = api(AnalyticsPool())
    data = client.get("/api/v1/analytics/quality").json()["data"]
    assert data == {
        "reviews": 0,
        "ai_pass_rate": None,
        "ai_fail_rate": None,
        "avg_score": 0.0,
        "rework_rate": 0.25,
        "orders": 12,
    }


def test_faults_and_insights_alias_forward_parameters(api):
    pool = AnalyticsPool()
    pool.rows = [{"code": "F-1", "name": "Valve", "n": 3}]
    client, _, _ = api(pool)
    faults = client.get(
        "/api/v1/analytics/faults", params={"from": "2026-02-01", "to": "2026-03-01", "limit": 5}
    )
    assert faults.json()["data"] == pool.rows
    assert pool.fetch_calls[-1][1] == ("2026-02-01", "2026-03-01", 5)
    pool.rows = [{"id": uuid.uuid4(), "status": "seen"}]
    assert client.get("/api/v1/analytics/ai-insights", params={"status": "seen"}).status_code == 200


def test_operational_economics_keeps_unknown_prices_explicit_and_scopes_master(api):
    material_id = uuid.uuid4()
    pool = AnalyticsPool()
    pool.materials = [{"id": material_id, "name": "Seal", "unit": "pcs", "quantity": 2.5}]
    client, _, user = api(pool, role="master", areaIds=["area-a"])
    body = {
        "from": "2026-01-01T00:00:00",
        "to": "2026-02-01T00:00:00",
        "labor_rate_per_hour": 1000,
        "downtime_cost_per_hour": 500,
        "material_unit_prices": {str(material_id): 75},
    }
    response = client.post("/api/v1/analytics/economics/operations", json=body)
    assert response.status_code == 200
    result = response.json()["data"]
    assert result["period_metrics"]["unplanned_share"] == 0.25
    assert result["estimated_costs"]["total"] == 7187.5
    assert result["materials"][0]["cost"] == 187.5
    assert result["period"]["from"].endswith("+00:00")
    assert all(
        call[1][-1] == user["areaIds"] for call in pool.fetchrow_calls[:1] + pool.fetch_calls[-1:]
    )


@pytest.mark.parametrize(
    "body",
    [
        {"currency": "kzt"},
        {"from": "2026-02-01T00:00:00Z", "to": "2026-01-01T00:00:00Z"},
        {"material_unit_prices": {"bad-id": 1}},
        {"material_unit_prices": {str(uuid.uuid4()): -1}},
    ],
)
def test_operational_economics_rejects_invalid_currency_period_and_prices(api, body):
    response = api()[0].post("/api/v1/analytics/economics/operations", json=body)
    assert response.status_code == 422


def test_feasibility_endpoint_reports_positive_and_nonpositive_cashflow_scenarios(api):
    client, _, user = api()
    positive = client.post(
        "/api/v1/analytics/economics/feasibility",
        json={
            "investment": 1200,
            "monthly_benefit": 400,
            "monthly_operating_cost": 100,
            "horizon_months": 12,
            "annual_discount_rate_pct": 10,
            "residual_value": 0,
        },
    )
    assert positive.status_code == 200
    assert positive.json()["data"]["simple_payback_months"] == 4
    assert positive.json()["data"]["annualized_irr_pct"] is not None

    no_return = client.post(
        "/api/v1/analytics/economics/feasibility",
        json={
            "investment": 1200,
            "monthly_benefit": 10,
            "monthly_operating_cost": 10,
            "horizon_months": 12,
        },
    )
    assert no_return.status_code == 200
    assert no_return.json()["data"]["simple_payback_months"] is None
    assert no_return.json()["data"]["annualized_irr_pct"] is None
    assert no_return.json()["data"]["payback_within_horizon"] is False


def test_plan_order_validates_exactly_one_assignee_and_creates_idempotent_order(api, monkeypatch):
    insight_id, equipment_id, area_id, order_id, assignee_id = (uuid.uuid4() for _ in range(5))

    class Conn:
        def __init__(self):
            self.calls = []

        async def fetchrow(self, query, *args):
            self.calls.append((query, args))
            if "FROM insights" in query:
                return {
                    "scope": {"equipment_id": str(equipment_id)},
                    "recommendation": "Inspect pump",
                    "headline": "Pump vibration",
                }
            if "FROM equipment" in query:
                return {"area_id": area_id}
            if "INSERT INTO work_orders" in query:
                return {"id": order_id}
            return None

        async def fetchval(self, query, *args):
            return 55

        async def execute(self, query, *args):
            self.calls.append((query, args))

    client, _, user = api()
    invalid = client.post(
        f"/api/v1/analytics/insights/{insight_id}/plan-order", json={"due_at": "2026-11-01"}
    )
    assert invalid.status_code == 422
    conn = Conn()

    async def run_idempotent(key, actor_id, payload_hash, action):
        result = await action(conn)
        return {"body": result["body"], "replayed": False}

    monkeypatch.setattr(analytics, "run_idempotent", run_idempotent)
    response = client.post(
        f"/api/v1/analytics/insights/{insight_id}/plan-order",
        headers={"Idempotency-Key": "plan-1"},
        json={"assignee_id": str(assignee_id), "due_at": "2026-11-01T09:00:00Z"},
    )
    assert response.status_code == 200
    assert response.json() == {"id": str(order_id), "replayed": False}
    insert = next((query, args) for query, args in conn.calls if "INSERT INTO work_orders" in query)
    assert insert[1][:7] == (
        55,
        "Inspect pump",
        str(area_id),
        str(equipment_id),
        str(assignee_id),
        None,
        user["id"],
    )
    assert any("UPDATE insights SET status='in_plan'" in query for query, _ in conn.calls)

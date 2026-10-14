"""Export API tests for role policy, ownership and queued job contracts."""

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app import deps
from app.routers import reports


class Conn:
    def __init__(self, job_id):
        self.job_id = job_id
        self.calls = []

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        if "INSERT INTO report_exports" in query:
            return {"id": self.job_id, "state": "queued"}
        return None

    async def execute(self, query, *args):
        self.calls.append((query, args))


class Pool:
    def __init__(self, *, order=None, export=None):
        self.order = order
        self.export = export
        self.fetchrow_calls = []

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        if "FROM work_orders" in query:
            return self.order
        return self.export


@pytest.fixture
def api(monkeypatch):
    clients = []

    def create(pool=None, role="manager", **fields):
        pool = pool or Pool()

        async def get_pool():
            return pool

        monkeypatch.setattr(reports, "get_pool", get_pool)
        monkeypatch.setattr(deps, "get_pool", get_pool)
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


def test_manager_export_queues_job_with_scope_filters_and_idempotency(api, monkeypatch):
    job_id = uuid.uuid4()
    order_id = uuid.uuid4()
    conn = Conn(job_id)
    client, _, user = api(
        Pool(order={"id": order_id}), role="manager", areaIds=["area-a"], crewId="crew-a"
    )

    async def run_idempotent(key, actor_id, request_hash, action):
        result = await action(conn)
        assert key == "report-key" and actor_id == user["id"]
        assert request_hash
        return {"body": result["body"], "replayed": False}

    monkeypatch.setattr(reports, "run_idempotent", run_idempotent)
    response = client.post(
        "/api/v1/reports/export",
        headers={"Idempotency-Key": "report-key"},
        json={
            "type": "orders",
            "format": "xlsx",
            "filters": {"status": "closed"},
            "work_order_id": str(order_id),
        },
    )
    assert response.status_code == 202
    assert response.json() == {"job_id": str(job_id), "status": "queued", "replayed": False}
    query, args = conn.calls[0]
    assert "INSERT INTO report_exports" in query
    assert args[0:3] == (user["id"], "orders", "xlsx")
    assert json.loads(args[3])["status"] == "closed"
    assert "work_order_id" in json.loads(args[3])
    assert json.loads(args[4]) == {
        "user_id": user["id"],
        "role": "manager",
        "area_ids": ["area-a"],
        "crew_id": "crew-a",
    }
    assert "report.export_requested" in conn.calls[1][0]


def test_worker_can_export_only_an_accessible_work_order(api, monkeypatch):
    order_id = uuid.uuid4()
    user_id = str(uuid.uuid4())
    pool = Pool(order={"id": order_id, "assignee_id": user_id, "crew_id": None})
    client, _, user = api(pool, role="worker", id=user_id)
    forbidden = client.post("/api/v1/reports/export", json={"type": "shift", "format": "pdf"})
    assert forbidden.status_code == 403

    conn = Conn(uuid.uuid4())

    async def run_idempotent(*args):
        result = await args[-1](conn)
        return {"body": result["body"], "replayed": True}

    monkeypatch.setattr(reports, "run_idempotent", run_idempotent)
    response = client.post(
        "/api/v1/reports/export",
        json={
            "type": "orders",
            "format": "pdf",
            "work_order_id": str(order_id),
        },
    )
    assert response.status_code == 202
    assert response.json()["replayed"] is True
    assert pool.fetchrow_calls[0][1] == (str(order_id),)

    inaccessible = api(Pool(), role="worker", id=user_id)[0].post(
        "/api/v1/reports/export",
        json={
            "type": "orders",
            "format": "pdf",
            "work_order_id": str(order_id),
        },
    )
    assert inaccessible.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"type": "unknown", "format": "pdf"},
        {"type": "orders", "format": "csv"},
        {"type": "orders", "format": "pdf", "work_order_id": "invalid-uuid"},
    ],
)
def test_export_contract_rejects_unknown_type_format_and_identifier(api, body):
    assert api()[0].post("/api/v1/reports/export", json=body).status_code == 422


def test_export_status_enforces_owner_and_admin_override_and_formats_each_state(api, monkeypatch):
    job_id = uuid.uuid4()
    owner_id = str(uuid.uuid4())
    pool = Pool(
        export={"id": job_id, "requested_by": owner_id, "state": "queued", "object_key": None}
    )
    client = api(pool, role="manager", id=owner_id)[0]
    assert client.get(f"/api/v1/reports/export/{job_id}").json() == {
        "job_id": str(job_id),
        "status": "queued",
        "state": "queued",
    }

    pool.export = {
        "id": job_id,
        "requested_by": owner_id,
        "state": "completed",
        "object_key": "report/file.xlsx",
    }
    monkeypatch.setattr(reports, "signed_url", lambda key, ttl: f"signed:{key}:{ttl}")
    completed = client.get(f"/api/v1/reports/export/{job_id}").json()
    assert completed["url"] == "signed:report/file.xlsx:900" and completed["expires_in"] == 900

    pool.export = {
        "id": job_id,
        "requested_by": owner_id,
        "state": "failed",
        "object_key": None,
        "error": "storage failed",
    }
    assert client.get(f"/api/v1/reports/export/{job_id}").json()["error"] == "storage failed"

    stranger = api(pool, role="manager")[0].get(f"/api/v1/reports/export/{job_id}")
    admin = api(pool, role="admin")[0].get(f"/api/v1/reports/export/{job_id}")
    assert stranger.status_code == 404
    assert admin.status_code == 200

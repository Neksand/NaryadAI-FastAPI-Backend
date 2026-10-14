"""HTTP-level state-machine tests using a controlled async SQL boundary."""

import json
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import deps
from app.routers import work_orders

ORDER_ID = str(uuid.uuid4())
AREA_ID = str(uuid.uuid4())
WORKER_ID = str(uuid.uuid4())
FAULT_ID = str(uuid.uuid4())
MATERIAL_ID = str(uuid.uuid4())


class Connection:
    def __init__(self, pool):
        self.pool = pool
        self.executions = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def transaction(self):
        return self

    async def fetchrow(self, query, *args):
        return await self.pool.fetchrow(query, *args)

    async def fetch(self, query, *args):
        return await self.pool.fetch(query, *args)

    async def fetchval(self, query, *args):
        if "MAX(queue_position)" in query:
            return 4
        if "author_id" in query and "FROM photos" in query:
            return self.pool.photo_count
        if "FROM photos" in query:
            return self.pool.after_photo_count
        return None

    async def execute(self, query, *args):
        self.executions.append((query, args))


class Pool:
    def __init__(self):
        self.order = {
            "id": ORDER_ID,
            "status": "issued",
            "area_id": AREA_ID,
            "assignee_id": WORKER_ID,
            "crew_id": None,
            "kind": "planned",
            "priority": "normal",
            "number": "WO-42",
        }
        self.details = {
            **self.order,
            "issued_at": datetime(2026, 1, 1, tzinfo=UTC),
            "equipment_id": uuid.uuid4(),
            "done_at": None,
        }
        self.rows = []
        self.photos = []
        self.materials = []
        self.events = []
        self.employees = []
        self.review = {"id": "review-1", "score": 90, "verdict": "accepted", "master_score": None}
        self.reason = {"code": "valid_reason"}
        self.fault = {"id": FAULT_ID}
        self.material = {"unit": "kg"}
        self.photo_count = 0
        self.after_photo_count = 1
        self.counters = {"issued": 3, "done": 2, "overdue": 1}
        self.fetch_calls = []
        self.fetchrow_calls = []
        self.conn = Connection(self)

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_):
        return False

    def acquire(self):
        return self

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        if "AS issued" in query:
            return self.counters
        if "JOIN equipment" in query and "FROM work_orders w" in query:
            return self.details
        if "FROM ai_reviews" in query:
            return self.review
        if "FROM reasons" in query:
            return self.reason
        if "FROM fault_codes" in query:
            return self.fault
        if "FROM materials" in query:
            return self.material
        if "FROM equipment" in query:
            return {"id": self.details["equipment_id"], "area_id": self.order["area_id"]}
        if "FROM work_orders" in query:
            return self.order
        return None

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        if "FROM photos" in query:
            return self.photos
        if "FROM material_writeoffs" in query:
            return self.materials
        if "FROM work_order_events" in query:
            return self.events
        if "FROM employees" in query:
            return self.employees
        return self.rows


@pytest.fixture
def api(monkeypatch):
    pool = Pool()

    async def get_pool():
        return pool

    async def no_read_check(*_args):
        return pool.order

    async def run_idempotent(_key, _actor, _request_hash, action):
        result = await action(pool.conn)
        return {"body": result["body"], "replayed": False}

    monkeypatch.setattr(work_orders, "get_pool", get_pool)
    monkeypatch.setattr(work_orders, "assert_can_read_order", no_read_check)
    monkeypatch.setattr(work_orders, "run_idempotent", run_idempotent)

    def client(role="master", **fields):
        from app.main import create_app

        app = create_app()
        user = {
            "id": WORKER_ID if role == "worker" else str(uuid.uuid4()),
            "role": role,
            "areaIds": [AREA_ID],
            "crewId": None,
            **fields,
        }
        app.dependency_overrides[deps.get_current_user] = lambda: user

        @contextmanager
        def no_lifespan_client():
            test_client = TestClient(app)
            try:
                yield test_client
            finally:
                test_client.close()

        return no_lifespan_client()

    yield pool, client


def test_work_order_listing_applies_filters_and_returns_stable_cursor(api):
    pool, client_factory = api
    issued = datetime(2026, 3, 2, tzinfo=UTC)
    pool.rows = [
        {
            "id": uuid.uuid4(),
            "issued_at": issued,
            "status": "issued",
            "area_id": AREA_ID,
            "assignee_id": WORKER_ID,
            "crew_id": None,
        },
        {
            "id": uuid.uuid4(),
            "issued_at": issued - timedelta(days=1),
            "status": "issued",
            "area_id": AREA_ID,
        },
    ]
    cursor_id = str(uuid.uuid4())
    cursor = work_orders.encode_cursor(cursor_id, issued.isoformat())
    with client_factory(role="manager") as client:
        response = client.get(
            "/api/v1/work-orders",
            params={
                "limit": 1,
                "area_id": AREA_ID,
                "equipment_id": str(uuid.uuid4()),
                "assignee_id": WORKER_ID,
                "crew_id": str(uuid.uuid4()),
                "kind": "unplanned",
                "priority": "high,critical",
                "status": "issued,queued",
                "overdue": "true",
                "from": "2026-01-01T00:00:00Z",
                "to": "2026-04-01T00:00:00Z",
                "cursor": cursor,
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1 and body["next_cursor"]
    query, args = pool.fetch_calls[-1]
    for fragment in (
        "w.area_id =",
        "w.equipment_id =",
        "w.assignee_id =",
        "w.crew_id =",
        "w.kind =",
        "work_order_priority[]",
        "work_order_status[]",
        "w.due_at < now()",
        "w.issued_at >=",
        "w.issued_at <",
        "w.issued_at, w.id",
    ):
        assert fragment in query
    assert args[-3:] == (issued.isoformat(), cursor_id, 2)
    decoded = work_orders.decode_cursor(body["next_cursor"])
    assert decoded["id"] == str(pool.rows[0]["id"])


@pytest.mark.parametrize(
    ("role", "areas", "expected"),
    [
        ("worker", [AREA_ID], "w.assignee_id = $1"),
        ("master", [AREA_ID], "w.area_id = ANY($1::uuid[])"),
        ("master", [], "WHERE false"),
        ("manager", [], "FROM work_orders w "),
    ],
)
def test_work_order_listing_scopes_rows_by_role(api, role, areas, expected):
    pool, client_factory = api
    with client_factory(role=role, areaIds=areas) as client:
        response = client.get("/api/v1/work-orders")
    assert response.status_code == 200
    assert expected in pool.fetch_calls[-1][0]


def test_work_order_listing_rejects_malformed_cursor_before_query(api):
    pool, client_factory = api
    with client_factory(role="manager") as client:
        response = client.get("/api/v1/work-orders?cursor=not-base64-json")
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["path"] == "cursor"
    assert pool.fetch_calls == []


def test_work_order_list_rejects_sql_like_enum_filters_before_query(api):
    pool, client_factory = api
    with client_factory(role="manager") as client:
        response = client.get("/api/v1/work-orders", params={"status": "issued) OR 1=1 --"})
        priority = client.get("/api/v1/work-orders", params={"priority": "critical,pg_sleep(10)"})
    assert response.status_code == 422 and priority.status_code == 422
    assert pool.fetch_calls == []


SQLI_FILTERS = [
    "issued' OR '1'='1",
    "issued); DROP TABLE work_orders;--",
    "issued UNION SELECT NULL",
    "pg_sleep(10)",
    "issued--",
    "issued/*comment*/",
    "issued,queued;--",
    "1=1",
    '" OR 1=1 --',
    "issued%27%20OR%201%3D1",
    "__import__('os')",
    "NULL",
    "issued\nOR 1=1",
    "issued||true",
    "issued; SELECT pg_catalog.version()",
    "critical),(SELECT 1)",
    "unlisted-enum",
    "issued%00",
    "issued#comment",
    "issued/**/OR/**/1=1",
    "../../work_orders",
    "false",
    "true",
    "$1::text",
    "::regclass",
    "to_regclass('work_orders')",
]


@pytest.mark.parametrize("field", ["status", "priority"])
@pytest.mark.parametrize("payload", SQLI_FILTERS)
def test_enum_filter_rejects_adversarial_sql_payloads_before_database_query(api, field, payload):
    pool, client_factory = api
    with client_factory(role="manager") as client:
        response = client.get("/api/v1/work-orders", params={field: payload})
    assert response.status_code == 422
    assert response.json()["error"]["details"]
    assert pool.fetch_calls == []


def test_order_detail_serializes_nested_rows_and_reports_latest_review(api):
    pool, client_factory = api
    photo_id = uuid.uuid4()
    material_id = uuid.uuid4()
    pool.photos = [{"id": photo_id, "kind": "after", "taken_at": datetime(2026, 3, 2, tzinfo=UTC)}]
    pool.materials = [{"id": uuid.uuid4(), "material_id": material_id, "quantity": 2.5}]
    with client_factory(role="master") as client:
        response = client.get(f"/api/v1/work-orders/{ORDER_ID}")
        events = client.get(f"/api/v1/work-orders/{ORDER_ID}/events")
        review = client.get(f"/api/v1/work-orders/{ORDER_ID}/ai-review")
        worker_report = client.get(f"/api/v1/work-orders/{ORDER_ID}/report?audience=worker")
        master_report = client.get(f"/api/v1/work-orders/{ORDER_ID}/report?audience=master")
    assert response.status_code == 200
    assert response.json()["photos"][0]["id"] == str(photo_id)
    assert response.json()["materials"][0]["id"] == str(pool.materials[0]["id"])
    assert response.json()["issued_at"].startswith("2026-01-01")
    assert events.json()["items"] == [] and review.json()["score"] == 90
    assert worker_report.json()["work_order_id"] == ORDER_ID
    assert master_report.json()["order"]["id"] == ORDER_ID


@pytest.mark.parametrize(
    ("status", "area", "payload", "expected"),
    [
        ("closed", AREA_ID, {"priority": "high"}, 409),
        ("issued", str(uuid.uuid4()), {"priority": "high"}, 404),
        ("issued", AREA_ID, {"due_at": "2000-01-01T00:00:00Z"}, 422),
    ],
)
def test_order_patch_rejects_terminal_out_of_scope_or_expired_updates(
    api, status, area, payload, expected
):
    pool, client_factory = api
    pool.order["status"] = status
    pool.order["area_id"] = area
    with client_factory(role="master") as client:
        response = client.patch(f"/api/v1/work-orders/{ORDER_ID}", json=payload)
    assert response.status_code == expected
    assert pool.conn.executions == []


def test_order_patch_writes_audit_event_for_valid_edit(api):
    pool, client_factory = api
    due = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    with client_factory(role="master") as client:
        response = client.patch(
            f"/api/v1/work-orders/{ORDER_ID}",
            json={"priority": "critical", "due_at": due, "comment": "  Проверено  "},
        )
    assert response.status_code == 200
    assert "UPDATE work_orders SET priority=COALESCE" in pool.conn.executions[0][0]
    assert pool.conn.executions[0][1][1] == "critical"
    assert "INSERT INTO work_order_events" in pool.conn.executions[1][0]
    assert json.loads(pool.conn.executions[1][1][-1])["comment"] == "Проверено"


@pytest.mark.parametrize(
    ("action", "status", "role", "extra", "target"),
    [
        ("accept", "issued", "worker", {}, "accepted"),
        ("queue", "issued", "worker", {}, "queued"),
        ("reject", "issued", "worker", {"reason_code": "valid_reason"}, "rejected"),
        ("start", "accepted", "worker", {}, "in_progress"),
        ("pause", "in_progress", "worker", {"reason_code": "valid_reason"}, "paused"),
        ("resume", "paused", "worker", {}, "in_progress"),
        ("cancel", "issued", "master", {"reason_code": "valid_reason"}, "cancelled"),
        ("return_to_rework", "ai_review", "master", {}, "rework"),
        ("reassign", "issued", "master", {"assignee_id": str(uuid.uuid4())}, "issued"),
        ("change_priority", "issued", "master", {"priority": "high"}, "issued"),
    ],
)
def test_transition_actions_apply_legal_state_change_and_emit_outbox(
    api, action, status, role, extra, target
):
    pool, client_factory = api
    pool.order["status"] = status
    payload = {"action": action, **extra}
    with client_factory(role=role) as client:
        response = client.post(f"/api/v1/work-orders/{ORDER_ID}/transitions", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["from"] == status and response.json()["to"] == target
    assert response.json()["replayed"] is False
    assert any("INSERT INTO work_order_events" in query for query, _ in pool.conn.executions)
    assert any("order.transition" in query for query, _ in pool.conn.executions)


@pytest.mark.parametrize(
    ("payload", "path"),
    [
        ({"action": "pause"}, "reason_code"),
        ({"action": "cancel"}, "reason_code"),
        ({"action": "reassign"}, "assignee_id"),
        ({"action": "change_priority"}, "priority"),
        ({"action": "close"}, "decision"),
        ({"action": "complete"}, "form"),
    ],
)
def test_transition_requires_complete_action_evidence_before_database_work(api, payload, path):
    pool, client_factory = api
    with client_factory(role="master") as client:
        response = client.post(f"/api/v1/work-orders/{ORDER_ID}/transitions", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["path"] == path
    assert pool.conn.executions == []


def test_transition_rejects_missing_orders_bad_reasons_and_wrong_roles(api):
    pool, client_factory = api
    pool.order = None
    with client_factory(role="worker") as client:
        missing = client.post(
            f"/api/v1/work-orders/{ORDER_ID}/transitions", json={"action": "accept"}
        )
    assert missing.status_code == 404

    pool.order = {
        "id": ORDER_ID,
        "status": "issued",
        "area_id": AREA_ID,
        "assignee_id": WORKER_ID,
        "kind": "planned",
    }
    pool.reason = None
    with client_factory(role="worker") as client:
        bad_reason = client.post(
            f"/api/v1/work-orders/{ORDER_ID}/transitions",
            json={"action": "reject", "reason_code": "missing"},
        )
    assert bad_reason.status_code == 422

    with client_factory(role="manager") as client:
        forbidden_action = client.post(
            f"/api/v1/work-orders/{ORDER_ID}/transitions", json={"action": "accept"}
        )
    assert forbidden_action.status_code == 403


def test_complete_requires_valid_fault_photo_and_material_and_enqueues_review(api):
    pool, client_factory = api
    pool.order.update(status="in_progress", kind="planned")
    pool.photo_count = 1
    photo_id = str(uuid.uuid4())
    payload = {
        "action": "complete",
        "photo_ids": [photo_id],
        "form": {
            "work_done_text": "Подшипник заменён",
            "fault_code_id": FAULT_ID,
            "materials": [{"material_id": MATERIAL_ID, "quantity": 1.5}],
        },
    }
    with client_factory(role="worker") as client:
        response = client.post(f"/api/v1/work-orders/{ORDER_ID}/transitions", json=payload)
    assert response.status_code == 200
    assert response.json()["to"] == "ai_review"
    assert any("UPDATE photos SET work_order_id" in query for query, _ in pool.conn.executions)
    assert any("INSERT INTO material_writeoffs" in query for query, _ in pool.conn.executions)
    assert any("ai.review_requested" in query for query, _ in pool.conn.executions)


@pytest.mark.parametrize(
    ("fault", "photo_count", "after_count", "material", "message"),
    [
        (None, 0, 1, True, "fault_code_id"),
        (FAULT_ID, 0, 1, True, "photo_ids"),
        (FAULT_ID, 1, 0, True, "photo_ids"),
        (FAULT_ID, 0, 1, False, "form.materials"),
    ],
)
def test_complete_fails_closed_when_evidence_or_reference_rows_are_missing(
    api, fault, photo_count, after_count, material, message
):
    pool, client_factory = api
    pool.order.update(status="in_progress", kind="unplanned")
    pool.fault = {"id": FAULT_ID} if fault else None
    pool.photo_count, pool.after_photo_count = photo_count, after_count
    pool.material = {"unit": "kg"} if material else None
    photo_ids = [str(uuid.uuid4())] if message == "photo_ids" else []
    payload = {
        "action": "complete",
        "photo_ids": photo_ids,
        "form": {
            "work_done_text": "Замена",
            "fault_code_id": fault or FAULT_ID,
            "materials": [{"material_id": MATERIAL_ID, "quantity": 1}] if not material else [],
        },
    }
    with client_factory(role="worker") as client:
        response = client.post(f"/api/v1/work-orders/{ORDER_ID}/transitions", json=payload)
    assert response.status_code == 422
    assert message in response.json()["error"]["details"][0]["path"]


def test_close_requires_ai_review_and_routes_rework_verdict_correctly(api):
    pool, client_factory = api
    pool.order["status"] = "ai_review"
    pool.review = None
    with client_factory(role="master") as client:
        missing = client.post(
            f"/api/v1/work-orders/{ORDER_ID}/transitions",
            json={"action": "close", "decision": "agree_ai"},
        )
    assert missing.status_code == 409

    pool.review = {"id": "review-2", "score": 70, "verdict": "needs_rework"}
    with client_factory(role="master") as client:
        response = client.post(
            f"/api/v1/work-orders/{ORDER_ID}/transitions",
            json={"action": "close", "decision": "agree_ai"},
        )
    assert response.status_code == 200 and response.json()["to"] == "rework"


def test_review_decision_validates_override_and_records_human_decision(api):
    pool, client_factory = api
    with client_factory(role="master") as client:
        invalid = client.post(
            f"/api/v1/work-orders/{ORDER_ID}/review/decision", json={"decision": "override"}
        )
    assert invalid.status_code == 422

    pool.review = {"id": "review-3", "score": 90, "verdict": "accepted"}
    with client_factory(role="master") as client:
        valid = client.post(
            f"/api/v1/work-orders/{ORDER_ID}/review/decision",
            json={
                "decision": "override",
                "score": 75,
                "comment": "Проверено мастером",
            },
        )
    assert valid.status_code == 200 and valid.json()["status"] == "closed"
    query, args = pool.conn.executions[0]
    assert "UPDATE ai_reviews" in query and args[2] == 75


def test_material_writeoff_requires_active_order_and_existing_material(api):
    pool, client_factory = api
    item = {"material_id": MATERIAL_ID, "quantity": 2}
    pool.order.update(status="issued")
    with client_factory(role="worker") as client:
        blocked = client.post(f"/api/v1/work-orders/{ORDER_ID}/materials", json=[item])
    assert blocked.status_code == 409

    pool.order.update(status="in_progress")
    pool.material = None
    with client_factory(role="worker") as client:
        missing = client.post(f"/api/v1/work-orders/{ORDER_ID}/materials", json=[item])
    assert missing.status_code == 422

    pool.material = {"unit": "kg"}
    with client_factory(role="worker") as client:
        success = client.post(f"/api/v1/work-orders/{ORDER_ID}/materials", json=[item])
    assert success.status_code == 201 and success.json()["added"] == 1
    assert any("INSERT INTO material_writeoffs" in query for query, _ in pool.conn.executions)


def test_shift_board_summary_and_equipment_history_respect_role_scope(api):
    pool, client_factory = api
    equipment_id = str(pool.details["equipment_id"])
    pool.employees = [{"id": uuid.uuid4(), "name": "Worker", "status": "free"}]
    pool.rows = [
        {
            "id": uuid.uuid4(),
            "number": "WO-42",
            "status": "closed",
            "description": "Течь масла",
            "work_done_text": "Заменили прокладку",
            "fault_code": "F-1",
            "return_count": 1,
        }
    ]
    with client_factory(role="manager") as manager:
        board = manager.get("/api/v1/shift/board")
        summary = manager.get("/api/v1/shift/summary")
        history = manager.get(f"/api/v1/equipment/{equipment_id}/history")
    assert board.json()["counters"] == pool.counters
    assert summary.json()["employees"] == 1
    assert history.json()["items"][0]["id"] == str(pool.rows[0]["id"])
    assert history.json()["items"][0]["work_done_text"] == "Заменили прокладку"
    history_query = next(query for query, _ in pool.fetch_calls if "LEFT JOIN fault_codes" in query)
    assert "ORDER BY w.issued_at DESC, w.id DESC LIMIT 200" in history_query

    with client_factory(role="worker") as worker:
        forbidden_history = worker.get(f"/api/v1/equipment/{equipment_id}/history")
    assert forbidden_history.status_code == 403

    with client_factory(role="master", areaIds=[str(uuid.uuid4())]) as other_master:
        hidden_history = other_master.get(f"/api/v1/equipment/{equipment_id}/history")
    assert hidden_history.status_code == 404

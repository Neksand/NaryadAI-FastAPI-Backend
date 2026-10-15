"""Outbox fan-out tests: recipient selection and graceful provider failures."""

import asyncio

from app.services import background


class Conn:
    def __init__(self, row=None, fail=False):
        self.row = row
        self.fail = fail

    async def fetchrow(self, *_):
        if self.fail:
            raise RuntimeError("temporary database failure")
        return self.row


def run(coro):
    return asyncio.run(coro)


def patch_fanout(monkeypatch, *, notifications=None, notify_error=False, broadcast_error=False):
    import app.notifications as notifications_module
    from app.routers import realtime

    sent = []
    broadcasts = []

    async def notify(conn, recipients, kind, data, entity_type, entity_id):
        sent.append((recipients, kind, data, entity_type, entity_id))
        if notify_error:
            raise RuntimeError("notification store offline")
        return notifications or [{"id": "notice-1"}]

    async def broadcast(channels, kind, data):
        broadcasts.append((channels, kind, data))
        if broadcast_error:
            raise RuntimeError("socket transport offline")

    monkeypatch.setattr(notifications_module, "notify", notify)
    monkeypatch.setattr(realtime, "broadcast", broadcast)
    return sent, broadcasts


def test_transition_targets_master_and_serializes_notification_ids(monkeypatch):
    sent, broadcasts = patch_fanout(monkeypatch, notifications=[{"id": "n-1"}, {"id": "n-2"}])
    order = {
        "id": "order-1",
        "number": 81,
        "master_id": "master-1",
        "assignee_id": "worker-1",
        "crew_id": None,
    }
    result = run(
        background._notify_for_event(
            Conn(order),
            "order.transition",
            {"id": "order-1", "action": "start"},
            ["user:worker-1", "shift:area-1"],
        )
    )

    assert result == ("WORK_ORDER_STARTED", [{"id": "n-1"}, {"id": "n-2"}])
    assert sent[0][0:2] == (["master-1"], "WORK_ORDER_STARTED")
    assert sent[0][2]["number"] == 81
    assert broadcasts[0][1] == "WORK_ORDER_STARTED"
    assert broadcasts[0][2]["notifications"] == ["n-1", "n-2"]
    assert broadcasts[1][1] == "NOTIFICATION_CREATED"


def test_close_and_rework_transitions_notify_both_master_and_assignee(monkeypatch):
    sent, _ = patch_fanout(monkeypatch)
    order = {
        "id": "order-2",
        "number": 82,
        "master_id": "master-2",
        "assignee_id": "worker-2",
        "crew_id": None,
    }

    closed = run(
        background._notify_for_event(
            Conn(order),
            "order.transition",
            {"id": "order-2", "action": "close"},
            ["user:watcher"],
        )
    )
    assert closed[0] == "WORK_ORDER_CLOSED"
    assert sent[0][0] == ["master-2", "worker-2"]

    sent.clear()
    returned = run(
        background._notify_for_event(
            Conn(order),
            "order.transition",
            {"id": "order-2", "action": "return_to_rework"},
            ["user:watcher"],
        )
    )
    assert returned[0] == "WORK_ORDER_REWORK"
    assert sent[0][0] == ["master-2", "worker-2"]


def test_event_mapping_covers_created_overdue_warning_and_worker_status(monkeypatch):
    sent, broadcasts = patch_fanout(monkeypatch)
    channels = ["user:worker-3", "shift:area-3"]

    created = run(background._notify_for_event(Conn(), "order.created", {"id": "o-3"}, channels))
    assert created[0] == "WORK_ORDER_CREATED"
    assert sent[0][0] == ["worker-3"]

    sent.clear()
    warning = run(
        background._notify_for_event(Conn(), "order.overdue_warning", {"id": "o-3"}, channels)
    )
    assert warning[0] == "DEADLINE_WARNING"
    assert sent[0][1] == "DEADLINE_WARNING"

    status = run(
        background._notify_for_event(
            Conn(), "employee.status_changed", {"employee_id": "worker-3"}, channels
        )
    )
    assert status == ("WORKER_STATUS_CHANGED", [])
    assert broadcasts[-1][1] == "WORKER_STATUS_CHANGED"


def test_unknown_action_and_database_notification_socket_failures_are_nonfatal(monkeypatch):
    sent, broadcasts = patch_fanout(monkeypatch, notify_error=True, broadcast_error=True)
    order = {
        "id": "order-4",
        "number": 84,
        "master_id": "master-4",
        "assignee_id": None,
        "crew_id": None,
    }
    fallback = run(
        background._notify_for_event(
            Conn(order),
            "order.transition",
            {"id": "order-4", "action": "future_action"},
            ["user:worker-4"],
        )
    )
    assert fallback == ("WORK_ORDER_UPDATED", [])
    assert sent == []  # Unknown transition actions have no persisted notification type.
    assert len(broadcasts) == 1

    no_order = run(
        background._notify_for_event(
            Conn(fail=True),
            "order.overdue",
            {"id": "order-5"},
            ["user:worker-5"],
        )
    )
    assert no_order == ("DEADLINE_EXCEEDED", [])


def test_reassignment_notifies_new_assignee_not_previous_subscriber(monkeypatch):
    sent, _ = patch_fanout(monkeypatch)
    order = {
        "id": "order",
        "number": 9,
        "master_id": "master",
        "assignee_id": "new-worker",
        "crew_id": None,
    }
    run(
        background._notify_for_event(
            Conn(order),
            "order.transition",
            {"id": "order", "action": "reassign"},
            ["user:old-worker", "user:new-worker"],
        )
    )
    assert sent[0][0] == ["new-worker"]


def test_ai_review_notifies_entire_assigned_crew(monkeypatch):
    sent, _ = patch_fanout(monkeypatch)
    order = {
        "id": "order",
        "number": 9,
        "master_id": "master",
        "assignee_id": None,
        "crew_id": "crew",
    }

    class CrewConn(Conn):
        async def fetch(self, query, *args):
            assert "enabled=true" in query
            return [{"id": "worker-1"}, {"id": "worker-2"}]

    run(
        background._notify_for_event(
            CrewConn(order), "order.ai_review_ready", {"id": "order"}, ["order:order"]
        )
    )
    assert sent[0][0] == ["master", "worker-1", "worker-2"]


def test_master_agreement_with_rework_emits_rework_not_closed(monkeypatch):
    sent, _ = patch_fanout(monkeypatch)
    order = {
        "id": "order",
        "number": 9,
        "master_id": "master",
        "assignee_id": "worker",
        "crew_id": None,
    }
    result = run(
        background._notify_for_event(
            Conn(order), "order.transition", {"id": "order", "action": "close", "to": "rework"}, []
        )
    )
    assert result[0] == "WORK_ORDER_REWORK"
    assert sent[0][1] == "WORK_ORDER_REWORK"

"""Deadline engine tests: due-30 / overdue-30 / long-120 / not-accepted 3-10 + free candidate."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

from app.services import background


class AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_):
        return False


class FakeConn:
    """Fake asyncpg connection routing by query text (mirrors test_background_schedulers)."""

    def __init__(self, *, rows=None, employees=None, recent=None, lock=True):
        self._rows = rows or []
        self._employees = employees or []
        self.recent = recent
        self.lock = lock
        self.executions = []
        self.fetch_queries = []
        self.fetchval_queries = []

    def transaction(self):
        return AsyncContext(None)

    async def fetchval(self, query, *args):
        self.fetchval_queries.append((query, args))
        q = query.lower()
        if "pg_try_advisory_lock" in q:
            return self.lock
        if "from notifications" in q:
            return self.recent
        return None

    async def fetch(self, query, *args):
        self.fetch_queries.append((query, args))
        if "from employees" in query.lower():
            return self._employees
        return self._rows

    async def execute(self, query, *args):
        self.executions.append((query, args))


def run(coro):
    return asyncio.run(coro)


def minutes_from_now(minutes):
    return datetime.now(UTC) + timedelta(minutes=minutes)


def minutes_ago(minutes):
    return datetime.now(UTC) - timedelta(minutes=minutes)


def due_order(**kw):
    base = {
        "id": "order-due-1",
        "number": 101,
        "area_id": "area-1",
        "assignee_id": "emp-1",
        "master_id": "master-1",
        "due_at": minutes_from_now(20),
    }
    base.update(kw)
    return base


# --- due-30 ---


def test_due_soon_sends_once_within_30_minutes():
    conn = FakeConn(rows=[due_order()])
    sent = run(background._due_soon_check(conn))
    assert sent == 1
    assert len(conn.executions) == 2
    insert_q, insert_args = conn.executions[0]
    assert "order.deadline_30min" in insert_q
    assert "user:emp-1" in insert_args[0]
    assert "user:master-1" in insert_args[0]
    payload = json.loads(insert_args[1])
    assert payload["minutes_left"] <= 30 and payload["minutes_left"] > 0
    assert "last_deadline_reminder_at" in conn.executions[1][0]


def test_due_soon_dedup_suppresses_repeat_within_hour():
    conn = FakeConn(rows=[due_order()], recent=1)
    assert run(background._due_soon_check(conn)) == 0
    assert conn.executions == []


def test_due_soon_outside_window_skipped():
    conn = FakeConn(rows=[due_order(due_at=minutes_from_now(45))])
    assert run(background._due_soon_check(conn)) == 0
    assert conn.executions == []


def test_due_soon_already_overdue_not_sent_here():
    conn = FakeConn(rows=[due_order(due_at=minutes_ago(5))])
    assert run(background._due_soon_check(conn)) == 0
    assert conn.executions == []


# --- overdue repeat ---


def test_overdue_repeats_every_30_minutes():
    conn = FakeConn(rows=[{"id": "o-1", "area_id": "a-1"}])
    n = run(background._overdue_check(conn))
    assert n == 1
    assert len(conn.executions) == 2
    assert "order.overdue" in conn.executions[0][0]
    assert "30 minutes" in conn.fetch_queries[0][0]
    assert "120 minutes" not in conn.fetch_queries[0][0]


# --- long overdue 120 ---


def test_long_overdue_goes_to_all_managers_only():
    order = {"id": "o-long", "number": 7, "area_id": "a-1", "due_at": minutes_ago(150)}
    employees = [
        {"id": "m-1", "role": "manager"},
        {"id": "m-2", "role": "manager"},
        {"id": "w-1", "role": "worker"},
    ]
    conn = FakeConn(rows=[order], employees=employees)
    assert run(background._long_overdue_check(conn)) == 1
    insert_q, insert_args = conn.executions[0]
    assert "order.long_overdue" in insert_q
    assert "user:m-1" in insert_args[0]
    assert "user:m-2" in insert_args[0]
    assert "user:w-1" not in insert_args[0]
    assert json.loads(insert_args[1])["minutes_overdue"] >= 120


def test_long_overdue_below_120_minutes_skipped():
    order = {"id": "o-60", "number": 8, "area_id": "a-1", "due_at": minutes_ago(60)}
    conn = FakeConn(rows=[order], employees=[{"id": "m-1", "role": "manager"}])
    assert run(background._long_overdue_check(conn)) == 0
    assert conn.executions == []


def test_long_overdue_without_managers_sends_nothing():
    order = {"id": "o-l", "number": 9, "area_id": "a-1", "due_at": minutes_ago(200)}
    conn = FakeConn(rows=[order], employees=[{"id": "w-1", "role": "worker"}])
    assert run(background._long_overdue_check(conn)) == 0
    assert conn.executions == []


# --- not accepted 3/10 ---


def unaccepted(**kw):
    base = {
        "id": "o-na",
        "number": 55,
        "area_id": "a-1",
        "master_id": "master-9",
        "assignee_id": "emp-9",
        "priority": "critical",
        "issued_at": minutes_ago(5),
        "created_at": minutes_ago(5),
    }
    base.update(kw)
    return base


def test_not_accepted_critical_threshold_is_3_minutes():
    assert background.not_accepted_threshold("critical") == 3
    assert background.should_send_not_accepted("critical", 5) is True
    assert background.should_send_not_accepted("critical", 2) is False


def test_not_accepted_usual_threshold_is_10_minutes():
    for prio in ("high", "normal", "planned", None):
        assert background.not_accepted_threshold(prio) == 10
    assert background.should_send_not_accepted("normal", 12) is True
    assert background.should_send_not_accepted("normal", 5) is False


def test_not_accepted_critical_alert_goes_to_master_with_candidate():
    conn = FakeConn(
        rows=[unaccepted()],
        employees=[{"id": "free-1", "full_name": "Иван", "current_status": "free"}],
    )
    assert run(background._not_accepted_check(conn)) == 1
    insert_q, insert_args = conn.executions[0]
    assert "order.not_accepted" in insert_q
    assert insert_args[0][0] == "user:master-9"
    payload = json.loads(insert_args[1])
    assert payload["candidate_id"] == "free-1"
    assert payload["candidate_name"] == "Иван"


def test_not_accepted_within_grace_period_skipped():
    conn = FakeConn(
        rows=[unaccepted(priority="critical", issued_at=minutes_ago(2), created_at=minutes_ago(2))],
        employees=[{"id": "free-1", "full_name": "Иван", "current_status": "free"}],
    )
    assert run(background._not_accepted_check(conn)) == 0
    conn2 = FakeConn(
        rows=[unaccepted(priority="normal", issued_at=minutes_ago(5), created_at=minutes_ago(5))],
        employees=[{"id": "free-1", "full_name": "Иван", "current_status": "free"}],
    )
    assert run(background._not_accepted_check(conn2)) == 0
    assert conn.executions == [] and conn2.executions == []


def test_candidate_picks_only_free_workers():
    pool = [
        {"id": "b-1", "full_name": "Busy", "current_status": "busy"},
        {"id": "o-1", "full_name": "Off", "current_status": "off"},
        {"id": "q-1", "full_name": "Queue", "current_status": "queue"},
        {"id": "f-1", "full_name": "Free One", "current_status": "free"},
        {"id": "f-2", "full_name": "Free Two", "current_status": "free"},
    ]
    picked = background.pick_free_candidate(pool)
    assert picked["id"] == "f-1"
    assert background.pick_free_candidate(pool[:3]) is None
    assert background.pick_free_candidate([]) is None


# --- templates + pure helpers ---


def test_new_templates_render_ru_and_kk():
    from app.notifications import TEMPLATES

    data = {
        "number": 42,
        "minutes_left": 25,
        "minutes_overdue": 150,
        "minutes_waiting": 12,
        "candidate_name": "Иван",
    }
    for key in ("DEADLINE_30MIN", "LONG_OVERDUE_120MIN", "NOT_ACCEPTED_WITH_CANDIDATE"):
        for lang in ("ru", "kk"):
            title, msg = TEMPLATES[key](data, lang)
            assert title and msg
            assert "42" in msg


def test_pure_trigger_boundaries():
    assert background.should_send_due_soon(30, False) is True
    assert background.should_send_due_soon(31, False) is False
    assert background.should_send_due_soon(0, False) is False
    assert background.should_send_due_soon(10, True) is False
    assert background.should_send_due_soon(None, False) is False
    assert background.should_escalate_long_overdue(120) is True
    assert background.should_escalate_long_overdue(119) is False
    assert background.should_escalate_long_overdue(None) is False

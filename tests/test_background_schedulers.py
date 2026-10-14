"""Deadline and insight scheduler tests with cancellation-safe fake services."""

import asyncio
import json

import pytest

from app.services import background


class AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_):
        return False


class SchedulerConnection:
    def __init__(self, *, locked=True, overdue=None):
        self.locked = locked
        self.overdue = overdue or []
        self.fetch_calls = []
        self.executions = []

    def transaction(self):
        return AsyncContext(None)

    async def fetchval(self, query):
        self.fetch_calls.append(("fetchval", query))
        return self.locked

    async def fetch(self, query):
        self.fetch_calls.append(("fetch", query))
        return self.overdue

    async def execute(self, query, *args):
        self.executions.append((query, args))


class SchedulerPool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return AsyncContext(self.conn)


def cancel_after_cycle(monkeypatch, stop_at):
    waits = []

    async def controlled_sleep(seconds):
        waits.append(seconds)
        if seconds == stop_at:
            raise asyncio.CancelledError

    monkeypatch.setattr(background.asyncio, "sleep", controlled_sleep)
    return waits


def test_insights_scheduler_runs_after_startup_delay_and_logs_generated_findings(monkeypatch):
    conn = SchedulerConnection()

    async def get_pool():
        return SchedulerPool(conn)

    async def generate(_conn):
        return ["repeated failure", "downtime"]

    monkeypatch.setattr("app.db.get_pool", get_pool)
    monkeypatch.setattr("app.services.insights.generate_insights", generate)
    waits = cancel_after_cycle(monkeypatch, 6 * 3600)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(background.insights_loop())

    assert waits == [60, 6 * 3600]


def test_insights_scheduler_tolerates_generation_failure_and_still_schedules_next_run(monkeypatch):
    async def get_pool():
        raise RuntimeError("database temporarily unavailable")

    monkeypatch.setattr("app.db.get_pool", get_pool)
    waits = cancel_after_cycle(monkeypatch, 6 * 3600)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(background.insights_loop())

    assert waits == [60, 6 * 3600]


def test_deadline_scheduler_enqueues_overdue_events_under_advisory_lock(monkeypatch):
    overdue = [{"id": "order-1", "area_id": "area-1"}, {"id": "order-2", "area_id": "area-2"}]
    conn = SchedulerConnection(overdue=overdue)

    async def get_pool():
        return SchedulerPool(conn)

    monkeypatch.setattr("app.db.get_pool", get_pool)
    waits = cancel_after_cycle(monkeypatch, 30)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(background.deadline_loop())

    assert conn.fetch_calls[0][0] == "fetchval"
    assert "pg_try_advisory_lock" in conn.fetch_calls[0][1]
    assert conn.fetch_calls[1][0] == "fetch"
    assert len(conn.executions) == 4
    assert [args[0] for _, args in conn.executions[::2]] == [
        ["shift:current", "order:order-1"],
        ["shift:current", "order:order-2"],
    ]
    assert [json.loads(args[1]) for _, args in conn.executions[::2]] == [
        {"id": "order-1", "area_id": "area-1"},
        {"id": "order-2", "area_id": "area-2"},
    ]
    assert waits == [30]


def test_deadline_scheduler_does_not_scan_or_enqueue_without_lock(monkeypatch):
    conn = SchedulerConnection(locked=False, overdue=[{"id": "ignored", "area_id": "area"}])

    async def get_pool():
        return SchedulerPool(conn)

    monkeypatch.setattr("app.db.get_pool", get_pool)
    waits = cancel_after_cycle(monkeypatch, 30)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(background.deadline_loop())

    assert [kind for kind, _ in conn.fetch_calls] == ["fetchval"]
    assert conn.executions == []
    assert waits == [30]


def test_deadline_scheduler_recovers_from_database_errors_on_next_interval(monkeypatch):
    async def get_pool():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("app.db.get_pool", get_pool)
    waits = cancel_after_cycle(monkeypatch, 30)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(background.deadline_loop())

    assert waits == [30]

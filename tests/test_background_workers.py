"""Outbox worker tests for delivery, internal jobs, retries, and idle polling."""

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


class FakeConnection:
    def __init__(self, row):
        self.row = row
        self.fetch_count = 0
        self.executions = []

    def transaction(self):
        return AsyncContext(None)

    async def fetchrow(self, *_):
        self.fetch_count += 1
        if self.fetch_count == 1:
            return self.row
        # Stop only after one whole transaction has completed.
        raise asyncio.CancelledError

    async def execute(self, query, *args):
        self.executions.append((query, args))


class FakeRedis:
    def __init__(self):
        self.published = []
        self.sequence_keys = []

    async def incr(self, key):
        self.sequence_keys.append(key)
        return 24

    async def publish(self, channel, payload):
        self.published.append((channel, payload))


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return AsyncContext(self.conn)


def install_worker_dependencies(monkeypatch, row):
    conn = FakeConnection(row)
    redis = FakeRedis()

    async def get_pool():
        return FakePool(conn)

    monkeypatch.setattr("app.db.get_pool", get_pool)
    monkeypatch.setattr(background.aioredis, "from_url", lambda *_args, **_kwargs: redis)

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(background.asyncio, "sleep", no_wait)
    return conn, redis


def test_outbox_publishes_regular_event_with_sequence_and_marks_it_published(monkeypatch):
    row = {
        "id": "event-1",
        "event": "order.created",
        "channels": ["shift:area-1", "user:worker-1"],
        "payload": {"id": "order-1", "number": "WO-1"},
    }
    conn, redis = install_worker_dependencies(monkeypatch, row)
    notifications = []

    async def fanout(_conn, event, payload, channels):
        notifications.append((event, payload, channels))

    monkeypatch.setattr(background, "_notify_for_event", fanout)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(background.outbox_loop())

    assert redis.sequence_keys == ["naryadai:events:seq"]
    assert redis.published[0][0] == "naryadai:events:v1"
    assert json.loads(redis.published[0][1]) == {
        "event": "order.created",
        "channels": row["channels"],
        "payload": row["payload"],
        "seq": 24,
    }
    assert notifications == [("order.created", row["payload"], row["channels"])]
    assert "published_at=now()" in conn.executions[-1][0]
    assert conn.executions[-1][1] == ("event-1",)


@pytest.mark.parametrize(
    ("event", "payload", "dependency", "expected_args"),
    [
        ("ai.review_requested", {"work_order_id": "order-2"}, "review", ("order-2",)),
        ("report.export_requested", {"export_id": "export-2"}, "export", ("export-2",)),
    ],
)
def test_outbox_routes_internal_jobs_without_publishing_to_redis(
    monkeypatch, event, payload, dependency, expected_args
):
    conn, redis = install_worker_dependencies(
        monkeypatch,
        {"id": "event-2", "event": event, "channels": [], "payload": payload},
    )
    calls = []

    async def handle(*args):
        calls.append(args)

    target = (
        "app.services.ai_review.run_ai_review"
        if dependency == "review"
        else "app.services.reports.process_export"
    )
    monkeypatch.setattr(target, handle)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(background.outbox_loop())

    assert calls == [(conn, *expected_args)]
    assert redis.published == []
    assert conn.executions[-1][0].startswith("UPDATE outbox_events SET published_at")


def test_outbox_releases_failed_event_and_records_bounded_error(monkeypatch):
    row = {"id": "event-3", "event": "order.created", "channels": [], "payload": {}}
    conn, redis = install_worker_dependencies(monkeypatch, row)

    async def fail_fanout(*_):
        raise RuntimeError("notification sink unavailable")

    monkeypatch.setattr(background, "_notify_for_event", fail_fanout)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(background.outbox_loop())

    failed_update = conn.executions[-1]
    assert "locked_until=NULL, last_error=$2" in failed_update[0]
    assert failed_update[1] == ("event-3", "notification sink unavailable")
    assert redis.published  # Publishing is at-least-once; DB failure leaves the row retryable.
    assert conn.executions[-1] == failed_update


def test_outbox_idle_poll_waits_before_trying_again(monkeypatch):
    conn, redis = install_worker_dependencies(monkeypatch, None)
    waits = []

    async def stop_after_idle_wait(seconds):
        waits.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(background.asyncio, "sleep", stop_after_idle_wait)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(background.outbox_loop())

    assert waits == [0.5]
    assert conn.executions == []
    assert redis.published == []

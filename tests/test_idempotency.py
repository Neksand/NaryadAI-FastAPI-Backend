"""Idempotency contract tests independent of database infrastructure."""

import asyncio
import json
import uuid

import pytest

from app import db, deps
from app.errors import AppError


class AsyncContext:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


class IdempotencyConn:
    def __init__(self):
        self.row = None
        self.business_effects = 0

    def transaction(self):
        return AsyncContext()

    async def execute(self, query, *args):
        if "INSERT INTO idempotency_keys" in query:
            if self.row is None:
                self.row = {
                    "id": "key-row-1",
                    "employee_id": args[0],
                    "key": args[1],
                    "request_hash": args[2],
                    "response_status": None,
                    "response_body": None,
                }
        elif "UPDATE idempotency_keys SET response_status" in query:
            self.row["response_status"] = args[1]
            self.row["response_body"] = json.loads(args[2])
        elif "INSERT INTO business_effect" in query:
            self.business_effects += 1
        else:
            raise AssertionError(f"Unexpected SQL: {query}")

    async def fetchrow(self, query, *_args):
        assert "SELECT * FROM idempotency_keys" in query
        return self.row


class IdempotencyPool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return AsyncContext(self.conn)


def test_same_key_and_same_payload_replays_stored_response_without_second_side_effect(monkeypatch):
    conn = IdempotencyConn()
    pool = IdempotencyPool(conn)

    async def get_pool():
        return pool

    async def action(tx):
        await tx.execute("INSERT INTO business_effect")
        return {"statusCode": 201, "body": {"id": "work-order-1"}}

    monkeypatch.setattr(db, "get_pool", get_pool)
    key = str(uuid.uuid4())
    first = asyncio.run(deps.run_idempotent(key, "employee-1", "hash-a", action))
    second = asyncio.run(deps.run_idempotent(key, "employee-1", "hash-a", action))

    assert first == {"status_code": 201, "body": {"id": "work-order-1"}, "replayed": False}
    assert second == {"status_code": 201, "body": {"id": "work-order-1"}, "replayed": True}
    assert conn.business_effects == 1


def test_same_key_with_different_payload_returns_conflict_without_running_action(monkeypatch):
    conn = IdempotencyConn()
    pool = IdempotencyPool(conn)

    async def get_pool():
        return pool

    async def first_action(tx):
        await tx.execute("INSERT INTO business_effect")
        return {"statusCode": 201, "body": {"id": "created-once"}}

    async def conflicting_action(_tx):
        pytest.fail("a conflicting payload must not run")

    monkeypatch.setattr(db, "get_pool", get_pool)
    key = str(uuid.uuid4())
    asyncio.run(deps.run_idempotent(key, "employee-1", "original-hash", first_action))

    with pytest.raises(AppError) as caught:
        asyncio.run(deps.run_idempotent(key, "employee-1", "changed-hash", conflicting_action))

    assert caught.value.status_code == 409
    assert caught.value.code == "idempotency_key_reused"
    assert conn.business_effects == 1


@pytest.mark.parametrize("key", [None, "not-a-uuid"])
def test_missing_or_malformed_key_is_rejected_before_database_access(monkeypatch, key):
    async def unexpected_pool_access():
        pytest.fail("invalid idempotency key must be rejected before database access")

    monkeypatch.setattr(db, "get_pool", unexpected_pool_access)

    with pytest.raises(AppError) as caught:
        asyncio.run(deps.run_idempotent(key, "employee-1", "hash-a", lambda _tx: None))

    if key is None:
        assert caught.value.status_code == 409
        assert caught.value.code == "idempotency_key_required"
    else:
        assert caught.value.status_code == 422
        assert caught.value.code == "validation_error"

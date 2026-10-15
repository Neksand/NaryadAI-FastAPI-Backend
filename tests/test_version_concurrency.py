"""Optimistic concurrency (expected_version) + replay access re-check.

DB-backed. Requires Postgres 127.0.0.1:15432 with migrations + seed applied.
Run against a scratch database via DATABASE_URL override (e.g. naryadai_ver) —
never naryadai / naryadai_stand::

    DATABASE_URL=postgresql://naryadai:naryadai@localhost:15432/naryadai_ver \
        .venv/bin/python -m pytest tests/test_version_concurrency.py -q
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def env(monkeypatch):
    import app.services.background as background
    from app.main import create_app

    async def idle_loop():
        await asyncio.Future()

    monkeypatch.setattr(background, "outbox_loop", idle_loop)
    monkeypatch.setattr(background, "deadline_loop", idle_loop)
    monkeypatch.setattr(background, "insights_loop", idle_loop)

    from app import db as _dbmod

    _dbmod._pool = None

    app = create_app()
    with TestClient(app) as client:
        master = _auth(client, "master1", "3333")
        worker = _auth(client, "worker01", "1001")
        yield {
            "app": app,
            "client": client,
            "mh": {"Authorization": f"Bearer {master['access_token']}"},
            "wh": {"Authorization": f"Bearer {worker['access_token']}"},
            "master": master,
            "worker": worker,
        }


def _auth(client: TestClient, login: str, pin: str) -> dict:
    r = client.post("/api/v1/auth/login", json={"login": login, "pin": pin})
    assert r.status_code == 200, r.text
    return r.json()


def _me(client: TestClient, headers: dict) -> dict:
    r = client.get("/api/v1/me", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _create_order(client: TestClient, mh: dict, assignee_id: str, area_ids: list) -> str:
    equip = client.get("/api/v1/dict/equipment", headers=mh).json()["items"]
    cand = [e for e in equip if e["area_id"] in area_ids]
    assert cand, "no equipment in worker area"
    due = (datetime.now(UTC) + timedelta(hours=4)).isoformat()
    r = client.post(
        "/api/v1/work-orders",
        headers={**mh, "Idempotency-Key": str(uuid.uuid4())},
        json={
            "kind": "planned",
            "description": "Наряд для проверки версий",
            "equipment_id": cand[0]["id"],
            "assignee_id": assignee_id,
            "priority": "normal",
            "due_at": due,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json().get("replayed") is False
    return r.json()["id"]


def _new_order(env) -> str:
    client, mh, wh = env["client"], env["mh"], env["wh"]
    me = _me(client, wh)
    return _create_order(client, mh, env["worker"]["user"]["id"], me["area_ids"])


def _tr(client: TestClient, headers: dict, oid: str, action: str, key=None, extra=None):
    body = {"action": action}
    body.update(extra or {})
    h = dict(headers)
    h["Idempotency-Key"] = key or str(uuid.uuid4())
    return client.post(f"/api/v1/work-orders/{oid}/transitions", headers=h, json=body)


def _db(client: TestClient, fn):
    return client.portal.call(fn)


def test_get_exposes_version_and_transition_increments_it(env):
    client, mh, wh = env["client"], env["mh"], env["wh"]
    oid = _new_order(env)

    detail = client.get(f"/api/v1/work-orders/{oid}", headers=wh)
    assert detail.status_code == 200, detail.text
    assert detail.json()["version"] == 1

    listing = client.get("/api/v1/work-orders", headers=mh, params={"limit": 100})
    assert listing.status_code == 200, listing.text
    card = next(i for i in listing.json()["items"] if i["id"] == oid)
    assert card["version"] == 1

    accept = _tr(client, wh, oid, "accept", extra={"expected_version": 1})
    assert accept.status_code == 200, accept.text
    assert accept.json()["version"] == 2
    assert accept.json()["replayed"] is False

    assert client.get(f"/api/v1/work-orders/{oid}", headers=wh).json()["version"] == 2

    stale = _tr(client, wh, oid, "start", extra={"expected_version": 1})
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "version_conflict"

    fresh = _tr(client, wh, oid, "start", extra={"expected_version": 2})
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["version"] == 3

    async def pause_reason():
        from app.db import get_pool

        pool = await get_pool()
        return await pool.fetchval("SELECT code FROM reasons WHERE kind='pause' LIMIT 1")

    reason = _db(client, pause_reason)
    no_version = _tr(client, wh, oid, "pause", extra={"reason_code": reason})
    # expected_version отсутствует → старое поведение (row lock решает).
    assert no_version.status_code == 200, no_version.text
    assert no_version.json()["version"] == 4


def test_concurrent_transitions_with_same_version_one_wins(env):
    import httpx

    client, app, wh = env["client"], env["app"], env["wh"]
    oid = _new_order(env)
    assert _tr(client, wh, oid, "accept", extra={"expected_version": 1}).status_code == 200

    async def race():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:

            async def one():
                return await ac.post(
                    f"/api/v1/work-orders/{oid}/transitions",
                    headers={**wh, "Idempotency-Key": str(uuid.uuid4())},
                    json={"action": "start", "expected_version": 2},
                )

            r1, r2 = await asyncio.gather(one(), one())
            return r1, r2

    r1, r2 = _db(client, race)
    assert sorted((r1.status_code, r2.status_code)) == [200, 409]
    loser = r1 if r1.status_code == 409 else r2
    assert loser.json()["error"]["code"] == "version_conflict"

    async def check():
        from app.db import get_pool

        pool = await get_pool()
        row = await pool.fetchrow("SELECT status, version FROM work_orders WHERE id=$1::uuid", oid)
        n = await pool.fetchval(
            "SELECT count(*) FROM work_order_events WHERE work_order_id=$1::uuid AND action='start'",
            oid,
        )
        return row["status"], int(row["version"]), int(n)

    status, version, n_events = _db(client, check)
    assert (status, version, n_events) == ("in_progress", 3, 1)


def test_replay_with_revoked_access_is_denied(env):
    client, mh, wh = env["client"], env["mh"], env["wh"]
    oid = _new_order(env)
    key = str(uuid.uuid4())
    first = _tr(client, wh, oid, "accept", key=key)
    assert first.status_code == 200, first.text
    assert first.json()["replayed"] is False

    async def other_worker():
        from app.db import get_pool

        pool = await get_pool()
        return await pool.fetchval(
            """SELECT e.id::text FROM employees e
               JOIN employee_areas ea ON ea.employee_id = e.id
               JOIN work_orders w ON w.area_id = ea.area_id
               WHERE w.id = $1::uuid AND e.role = 'worker' AND e.enabled = true
               AND e.deleted_at IS NULL AND e.id::text <> $2 LIMIT 1""",
            oid,
            env["worker"]["user"]["id"],
        )

    other = _db(client, other_worker)
    assert other, "seed must provide a second worker in the order area"

    moved = _tr(client, mh, oid, "reassign", extra={"assignee_id": other})
    assert moved.status_code == 200, moved.text

    replay = _tr(client, wh, oid, "accept", key=key)
    assert replay.status_code in (403, 404), replay.text
    assert replay.json().get("replayed") is not True

    async def check():
        from app.db import get_pool

        pool = await get_pool()
        return await pool.fetchval(
            "SELECT count(*) FROM work_order_events WHERE work_order_id=$1::uuid AND action='accept'",
            oid,
        )

    assert int(_db(client, check)) == 1


def test_replay_without_access_change_returns_same_without_duplicate(env):
    client, wh = env["client"], env["wh"]
    oid = _new_order(env)
    key = str(uuid.uuid4())
    first = _tr(client, wh, oid, "accept", key=key)
    assert first.status_code == 200, first.text
    assert first.json()["replayed"] is False

    second = _tr(client, wh, oid, "accept", key=key)
    assert second.status_code == 200, second.text
    assert second.json()["replayed"] is True
    for f in ("id", "from", "to", "action", "version"):
        assert second.json()[f] == first.json()[f]

    async def check():
        from app.db import get_pool

        pool = await get_pool()
        events = await pool.fetchval(
            "SELECT count(*) FROM work_order_events WHERE work_order_id=$1::uuid AND action='accept'",
            oid,
        )
        outbox = await pool.fetchval(
            "SELECT count(*) FROM outbox_events WHERE event='order.transition' AND payload->>'id'=$1",
            oid,
        )
        return int(events), int(outbox)

    assert _db(client, check) == (1, 1)

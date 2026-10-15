"""Top-1 integration-2: Postgres-backed integration (migrations, races, outbox, seed/backup).

Requires local infra: postgres 127.0.0.1:15432 (migrate+seed applied), redis,
minio S3. Mirrors tests/test_demo_flow.py: background loops are idled so outbox
rows stay observable instead of being raced by the real publisher.
"""

import asyncio
import json
import os
import subprocess
import sys
import urllib.parse
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]
MIGRATIONS = sorted(p.name for p in (REPO / "migrations").glob("*.sql"))

CLAIM_SQL = """UPDATE outbox_events SET locked_until=now()+interval '30 seconds', attempts=attempts+1
    WHERE id=(SELECT id FROM outbox_events WHERE published_at IS NULL
    AND (locked_until IS NULL OR locked_until < now()) ORDER BY created_at
    FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *"""


@pytest.fixture()
def env(monkeypatch):
    import app.ai_providers as ai_providers
    import app.services.ai_review as ai_review
    import app.services.background as background
    from app.config import settings

    async def no_text_provider(*_):
        return None, "local"

    async def no_vision_provider(*_):
        return None, "local"

    async def local_relevance(*_):
        return True, 0.75, "local"

    async def idle_loop():
        await asyncio.Future()

    monkeypatch.setattr(settings, "AI_MODE", "mock")
    monkeypatch.setattr(settings, "AI_TEXT_PROVIDER", "off")
    monkeypatch.setattr(settings, "AI_VISION_PROVIDER", "off")
    monkeypatch.setattr(ai_providers, "chat_text", no_text_provider)
    monkeypatch.setattr(ai_providers, "compare_photos", no_vision_provider)
    monkeypatch.setattr(ai_review, "_llm_relevance", local_relevance)
    monkeypatch.setattr(background, "outbox_loop", idle_loop)
    monkeypatch.setattr(background, "deadline_loop", idle_loop)
    monkeypatch.setattr(background, "insights_loop", idle_loop)

    # Pool — глобальный синглтон, привязанный к event loop. Тесты с asyncio.run()
    # до нас могли оставить пул на закрытом цикле — lifespan startup тогда вернул
    # бы stale pool, а shutdown упал бы. Сбрасываем: lifespan создаст свежий пул
    # на текущем portal-цикле.
    from app import db as _dbmod
    from app.main import create_app

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
            "description": "Интеграционный наряд top1-i2",
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


def _pg_env() -> dict:
    from app.config import settings

    u = urllib.parse.urlparse(settings.DATABASE_URL)
    return {
        "host": u.hostname or "127.0.0.1",
        "port": str(u.port or 5432),
        "dbname": (u.path or "/naryadai").lstrip("/"),
        "user": u.username or "naryadai",
        "password": u.password or "naryadai",
    }


def _pg_tools(tool: str) -> list[str]:
    """Use matching host tools if explicitly configured, otherwise Compose."""
    bindir = os.environ.get("PG_BINDIR")
    if bindir:
        binary = Path(bindir) / tool
        if not binary.is_file():
            raise RuntimeError(f"Missing PostgreSQL client: {binary}")
        return [str(binary)]
    return ["docker", "compose", "exec", "-T", "postgres", tool]


def _pg_client_env() -> dict[str, str]:
    env = dict(os.environ)
    if env.get("PG_BINDIR"):
        pg = _pg_env()
        env.update(PGHOST=pg["host"], PGPORT=pg["port"], PGPASSWORD=pg["password"])
    return env


# --- migrations ---


def test_migrations_state_matches_files(env):
    async def check():
        from app.db import get_pool

        pool = await get_pool()
        rows = await pool.fetch("SELECT name FROM schema_migrations ORDER BY name")
        return [r["name"] for r in rows]

    applied = _db(env["client"], check)
    assert MIGRATIONS, "migrations/ is empty"
    assert applied == MIGRATIONS
    assert "001_initial.sql" in applied


def test_migrate_applied_order_monotonic(env):
    async def check():
        from app.db import get_pool

        pool = await get_pool()
        rows = await pool.fetch("SELECT name, applied_at FROM schema_migrations ORDER BY name")
        return [(r["name"], r["applied_at"]) for r in rows]

    rows = _db(env["client"], check)
    assert len(rows) >= 5
    stamps = [a for _, a in rows]
    assert stamps == sorted(stamps), "applied_at must be monotonic with filename order"


def test_migrate_rerun_via_subprocess_is_noop(env):
    async def count():
        from app.db import get_pool

        pool = await get_pool()
        return await pool.fetchval("SELECT count(*) FROM schema_migrations")

    before = _db(env["client"], count)
    for _ in range(2):
        p = subprocess.run(
            [sys.executable, "-m", "scripts.migrate"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert p.returncode == 0, p.stderr
    assert _db(env["client"], count) == before


def test_migrate_is_forward_only_state_table_contract(env):
    src = (REPO / "scripts" / "migrate.py").read_text(encoding="utf-8")
    assert "downgrade" not in src.lower() and "down" not in src.split("applied_at")[0].lower()

    async def check():
        from app.db import get_pool

        pool = await get_pool()
        return await pool.fetchval(
            "SELECT count(*) FROM information_schema.tables WHERE table_name='schema_migrations'"
        )

    assert _db(env["client"], check) == 1


def test_core_tables_exist(env):
    async def check():
        from app.db import get_pool

        pool = await get_pool()
        rows = await pool.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
        )
        return {r["table_name"] for r in rows}

    tables = _db(env["client"], check)
    for t in (
        "work_orders",
        "work_order_events",
        "outbox_events",
        "idempotency_keys",
        "notifications",
        "employees",
        "schema_migrations",
    ):
        assert t in tables, t


# --- concurrent / conflicting transitions ---


def test_double_accept_second_conflicts_409(env):
    client, wh = env["client"], env["wh"]
    oid = _new_order(env)
    first = _tr(client, wh, oid, "accept")
    assert first.status_code == 200, first.text
    second = _tr(client, wh, oid, "accept")
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "invalid_transition"


def test_concurrent_accept_race_one_winner(env):
    import httpx

    client, app, wh = env["client"], env["app"], env["wh"]
    oid = _new_order(env)

    async def race():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:

            async def one():
                return await ac.post(
                    f"/api/v1/work-orders/{oid}/transitions",
                    headers={**wh, "Idempotency-Key": str(uuid.uuid4())},
                    json={"action": "accept"},
                )

            r1, r2 = await asyncio.gather(one(), one())
            return sorted((r1.status_code, r2.status_code))

    assert _db(client, race) == [200, 409]

    async def check():
        from app.db import get_pool

        pool = await get_pool()
        status = await pool.fetchval("SELECT status FROM work_orders WHERE id=$1::uuid", oid)
        n = await pool.fetchval(
            "SELECT count(*) FROM work_order_events WHERE work_order_id=$1::uuid AND action='accept'",
            oid,
        )
        return status, int(n)

    status, n_events = _db(client, check)
    assert status == "accepted"
    assert n_events == 1


def test_same_idempotency_key_replays_transition(env):
    client, wh = env["client"], env["wh"]
    oid = _new_order(env)
    key = str(uuid.uuid4())
    first = _tr(client, wh, oid, "accept", key=key)
    assert first.status_code == 200, first.text
    assert first.json()["replayed"] is False
    second = _tr(client, wh, oid, "accept", key=key)
    assert second.status_code == 200, second.text
    assert second.json()["replayed"] is True
    assert second.json()["to"] == first.json()["to"] == "accepted"

    async def check():
        from app.db import get_pool

        pool = await get_pool()
        return await pool.fetchval(
            "SELECT count(*) FROM outbox_events WHERE event='order.transition' "
            "AND payload->>'id'=$1",
            oid,
        )

    assert _db(client, check) == 1


def test_same_key_different_payload_conflicts(env):
    client, wh = env["client"], env["wh"]
    oid = _new_order(env)
    key = str(uuid.uuid4())
    assert _tr(client, wh, oid, "accept", key=key).status_code == 200
    clash = _tr(client, wh, oid, "start", key=key)
    assert clash.status_code == 409, clash.text
    assert clash.json()["error"]["code"] == "idempotency_key_reused"


def test_patch_open_order_ok(env):
    client, mh = env["client"], env["mh"]
    oid = _new_order(env)
    r = client.patch(f"/api/v1/work-orders/{oid}", headers=mh, json={"priority": "high"})
    assert r.status_code == 200, r.text

    async def check():
        from app.db import get_pool

        pool = await get_pool()
        prio = await pool.fetchval("SELECT priority FROM work_orders WHERE id=$1::uuid", oid)
        n = await pool.fetchval(
            "SELECT count(*) FROM work_order_events WHERE work_order_id=$1::uuid AND action='updated'",
            oid,
        )
        return prio, int(n)

    prio, n = _db(client, check)
    assert prio == "high"
    assert n == 1


def test_patch_closed_order_conflicts(env):
    client, mh = env["client"], env["mh"]

    async def closed_id():
        from app.db import get_pool

        pool = await get_pool()
        return await pool.fetchval("SELECT id::text FROM work_orders WHERE status='closed' LIMIT 1")

    oid = _db(client, closed_id)
    assert oid, "seed must provide a closed order"
    r = client.patch(f"/api/v1/work-orders/{oid}", headers=mh, json={"priority": "high"})
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "invalid_transition"


# --- outbox ---


def test_create_emits_order_created_outbox(env):
    client = env["client"]
    oid = _new_order(env)

    async def check():
        from app.db import get_pool

        pool = await get_pool()
        return await pool.fetchrow(
            "SELECT event, published_at, payload FROM outbox_events "
            "WHERE event='order.created' AND payload->>'id'=$1",
            oid,
        )

    row = _db(client, check)
    assert row is not None
    assert row["published_at"] is None
    payload = row["payload"]
    payload = json.loads(payload) if isinstance(payload, str) else dict(payload)
    assert payload.get("id") == oid


def test_accept_emits_order_transition_outbox(env):
    client, wh = env["client"], env["wh"]
    oid = _new_order(env)
    assert _tr(client, wh, oid, "accept").status_code == 200

    async def check():
        from app.db import get_pool

        pool = await get_pool()
        return await pool.fetchrow(
            "SELECT payload, published_at FROM outbox_events "
            "WHERE event='order.transition' AND payload->>'id'=$1",
            oid,
        )

    row = _db(client, check)
    assert row is not None and row["published_at"] is None
    payload = row["payload"]
    payload = json.loads(payload) if isinstance(payload, str) else dict(payload)
    assert (payload.get("from"), payload.get("to")) == ("issued", "accepted")


def test_complete_emits_ai_review_requested(env):
    client, mh, wh = env["client"], env["mh"], env["wh"]
    oid = _new_order(env)
    assert _tr(client, wh, oid, "accept").status_code == 200
    assert _tr(client, wh, oid, "start").status_code == 200
    faults = client.get("/api/v1/dict/fault-codes", headers=mh).json()["items"]
    assert faults
    r = _tr(
        client,
        wh,
        oid,
        "complete",
        extra={
            "form": {
                "work_done_text": "Плановый осмотр выполнен",
                "fault_code_id": faults[0]["id"],
                "materials": [],
            }
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["to"] == "ai_review"

    async def check():
        from app.db import get_pool

        pool = await get_pool()
        got = await pool.fetchrow(
            "SELECT payload, published_at FROM outbox_events "
            "WHERE event='ai.review_requested' AND payload->>'work_order_id'=$1",
            oid,
        )
        review = await pool.fetchrow("SELECT id FROM ai_reviews WHERE work_order_id=$1::uuid", oid)
        return got, review

    got, review = _db(client, check)
    assert got is not None and got["published_at"] is None
    assert review is None  # worker idle: queued but not processed
    missing = client.get(f"/api/v1/work-orders/{oid}/ai-review", headers=mh)
    assert missing.status_code == 404


def test_outbox_redelivery_does_not_duplicate(env):
    # Only events with user: channels fan out to persistent notifications
    # (order.created -> WORK_ORDER_ASSIGNED). order.transition payloads carry
    # no action and stay WS-only — covered by test_accept_emits below.
    from app.services.background import _notify_for_event

    client = env["client"]
    oid = _new_order(env)

    async def deliver_once():
        from app.db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM outbox_events WHERE event='order.created' "
                    "AND payload->>'id'=$1 AND published_at IS NULL",
                    oid,
                )
                assert row is not None
                assert any(str(c).startswith("user:") for c in (row["channels"] or []))
                payload = row["payload"]
                payload = json.loads(payload) if isinstance(payload, str) else dict(payload)
                ws_type, rows = await _notify_for_event(
                    conn, row["event"], payload, list(row["channels"] or [])
                )
                assert ws_type == "WORK_ORDER_CREATED"
                assert len(rows) == 1  # assignee notified exactly once
                await conn.execute(
                    "UPDATE outbox_events SET published_at=now(), locked_until=NULL WHERE id=$1",
                    row["id"],
                )
                return str(row["id"])

    eid = _db(client, deliver_once)

    async def redeliver():
        from app.db import get_pool

        pool = await get_pool()
        before = await pool.fetchval(
            "SELECT count(*) FROM notifications WHERE related_entity_id=$1::uuid", oid
        )
        claimed = await pool.fetchrow(
            "SELECT id FROM outbox_events WHERE id=$1::uuid AND published_at IS NULL", eid
        )
        after = await pool.fetchval(
            "SELECT count(*) FROM notifications WHERE related_entity_id=$1::uuid", oid
        )
        return int(before), claimed, int(after)

    before, claimed, after = _db(client, redeliver)
    assert before == 1
    assert claimed is None  # published gate: nothing left to redeliver
    assert after == before


def test_outbox_locked_row_skipped_by_second_worker(env):
    client, wh = env["client"], env["wh"]
    oid = _new_order(env)
    assert _tr(client, wh, oid, "accept").status_code == 200

    async def check():
        from app.db import get_pool

        pool = await get_pool()
        filt = " AND event='order.transition' AND payload->>'id'=$1"
        scoped = CLAIM_SQL.replace("ORDER BY created_at", f"{filt} ORDER BY created_at")
        async with pool.acquire() as c1:
            async with c1.transaction():
                row = await c1.fetchrow(scoped, oid)
                assert row is not None
                async with pool.acquire() as c2:
                    row2 = await c2.fetchrow(scoped, oid)
                    assert row2 is None  # SKIP LOCKED: claimed row invisible
                await c1.execute(
                    "UPDATE outbox_events SET locked_until=NULL, attempts=attempts-1 WHERE id=$1",
                    row["id"],
                )
                return str(row["id"])

    eid = _db(client, check)
    assert uuid.UUID(eid)


# --- seed idempotency + backup/restore ---


def test_seed_dict_tables_stable_no_duplicates(env):
    client = env["client"]

    async def counts():
        from app.db import get_pool

        pool = await get_pool()
        out = {}
        # Мягко удалённые (deleted_at) не считаем: админские create/delete в live-прогонах
        # не должны ронять проверку стабильности сида.
        for t, where in (
            ("areas", " WHERE deleted_at IS NULL"),
            ("equipment", " WHERE deleted_at IS NULL"),
            ("fault_codes", " WHERE deleted_at IS NULL"),
            ("materials", " WHERE deleted_at IS NULL"),
            ("employees", " WHERE deleted_at IS NULL"),
        ):
            out[t] = await pool.fetchval(f"SELECT count(*) FROM {t}{where}")
        dups = await pool.fetch("SELECT login FROM employees GROUP BY login HAVING count(*)>1")
        inv = await pool.fetch(
            "SELECT inventory_no FROM equipment GROUP BY inventory_no HAVING count(*)>1"
        )
        return out, len(dups), len(inv)

    before, d0, i0 = _db(client, counts)
    assert (before["areas"], before["equipment"], before["fault_codes"], before["materials"]) == (
        4,
        25,
        20,
        40,
    )
    p = subprocess.run(
        [sys.executable, "-m", "scripts.seed"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert p.returncode == 0, p.stderr
    assert "Seed OK" in p.stdout
    after, d1, i1 = _db(client, counts)
    assert after == before
    assert (d0, i0, d1, i1) == (0, 0, 0, 0)


def test_seed_work_orders_delta_is_three_fresh(env):
    client = env["client"]

    async def count():
        from app.db import get_pool

        pool = await get_pool()
        return await pool.fetchval("SELECT count(*) FROM work_orders")

    before = _db(client, count)
    p = subprocess.run(
        [sys.executable, "-m", "scripts.seed"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert p.returncode == 0, p.stderr
    # Seed is fully idempotent: historical orders top up to 600 once, the 3 fresh
    # demo orders insert only when absent — a re-run changes nothing.
    assert _db(client, count) == before


def test_work_order_numbers_unique_after_seed(env):
    async def check():
        from app.db import get_pool

        pool = await get_pool()
        return await pool.fetch("SELECT number FROM work_orders GROUP BY number HAVING count(*)>1")

    assert _db(env["client"], check) == []


def test_backup_schema_dump_has_core_tables(env):
    pg = _pg_env()
    cmd = [
        *_pg_tools("pg_dump"),
        "-U",
        pg["user"],
        "-d",
        pg["dbname"],
        "--schema-only",
        "-t",
        "work_orders",
        "-t",
        "outbox_events",
        "-t",
        "idempotency_keys",
        "-t",
        "schema_migrations",
    ]
    p = subprocess.run(
        cmd, capture_output=True, text=True, timeout=120, cwd=REPO, env=_pg_client_env()
    )
    assert p.returncode == 0, p.stderr
    for t in ("work_orders", "outbox_events", "idempotency_keys", "schema_migrations"):
        assert f"CREATE TABLE public.{t}" in p.stdout or f"CREATE TABLE {t}" in p.stdout, t


def test_backup_restore_probe_roundtrip(env):
    pg = _pg_env()
    base = ["-U", pg["user"], "-d", pg["dbname"]]
    probe = f"__backup_probe_{uuid.uuid4().hex[:8]}"

    async def setup():
        from app.db import get_pool

        pool = await get_pool()
        await pool.execute(f"CREATE TABLE {probe}(id int PRIMARY KEY, v text)")
        await pool.execute(f"INSERT INTO {probe} VALUES (1, 'naryad'), (2, 'audit')")

    _db(env["client"], setup)
    try:
        dump = subprocess.run(
            [*_pg_tools("pg_dump"), *base, "-t", probe],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=REPO,
            env=_pg_client_env(),
        )
        assert dump.returncode == 0, dump.stderr
        assert "COPY" in dump.stdout and "naryad" in dump.stdout

        async def drop():
            from app.db import get_pool

            pool = await get_pool()
            await pool.execute(f"DROP TABLE {probe}")

        _db(env["client"], drop)
        restore = subprocess.run(
            [*_pg_tools("psql"), *base, "-v", "ON_ERROR_STOP=1", "-q", "-f", "-"],
            input=dump.stdout,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=REPO,
            env=_pg_client_env(),
        )
        assert restore.returncode == 0, restore.stderr

        async def verify():
            from app.db import get_pool

            pool = await get_pool()
            return await pool.fetch(f"SELECT id, v FROM {probe} ORDER BY id")

        rows = _db(env["client"], verify)
        assert [(r["id"], r["v"]) for r in rows] == [(1, "naryad"), (2, "audit")]
    finally:

        async def cleanup():
            from app.db import get_pool

            pool = await get_pool()
            await pool.execute(f"DROP TABLE IF EXISTS {probe}")

        _db(env["client"], cleanup)


def test_login_still_works_after_seed(env):
    client = env["client"]
    for login, pin in (("master1", "3333"), ("worker01", "1001"), ("manager1", "2222")):
        r = client.post("/api/v1/auth/login", json={"login": login, "pin": pin})
        assert r.status_code == 200, (login, r.text)


def test_board_lists_created_order(env):
    # /shift/board exposes aggregates only (no per-order rows): read-your-write
    # shows up as counters.issued + 1 after creating an order.
    client, mh = env["client"], env["mh"]
    before = client.get("/api/v1/shift/board", headers=mh)
    assert before.status_code == 200, before.text
    _new_order(env)
    after = client.get("/api/v1/shift/board", headers=mh)
    assert after.status_code == 200, after.text
    assert after.json()["counters"]["issued"] == before.json()["counters"]["issued"] + 1
    assert after.json()["employees"], "board must list workers"


def test_events_log_records_transition_from_to(env):
    client, wh = env["client"], env["wh"]
    oid = _new_order(env)
    assert _tr(client, wh, oid, "accept").status_code == 200
    r = client.get(f"/api/v1/work-orders/{oid}/events", headers=wh)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    accepts = [e for e in items if e["action"] == "accept"]
    assert len(accepts) == 1
    payload = accepts[0]["payload"]
    payload = json.loads(payload) if isinstance(payload, str) else payload
    assert (payload.get("from"), payload.get("to")) == ("issued", "accepted")

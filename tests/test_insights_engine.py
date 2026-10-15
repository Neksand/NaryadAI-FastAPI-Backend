"""Insight evidence: order_ids + denominator + formula are really filled.

DB-backed. Requires scratch database naryadai_ins2 on 127.0.0.1:15432
with migrations + seed applied — never naryadai / naryadai_stand::

    DATABASE_URL=postgresql://naryadai:naryadai@localhost:15432/naryadai_ins2 \
        .venv/bin/python -m scripts/migrate.py
    DATABASE_URL=postgresql://naryadai:naryadai@localhost:15432/naryadai_ins2 \
        .venv/bin/python -m scripts/seed.py
    DATABASE_URL=postgresql://naryadai:naryadai@localhost:15432/naryadai_ins2 \
        .venv/bin/python -m pytest tests/test_insights_engine.py -q
"""

import asyncio
import os
import uuid

import pytest

SCRATCH_DB = "naryadai_ins2"

# Scratch DBs this file may run against. Production/dev databases are never
# allowed: exact main names (banco `naryadai`, `naryadai_stand`) are refused.
ALLOWED_SCRATCH_DBS = ("naryadai_ins2", "naryadai_final")


def _db_url() -> str:
    from app.config import settings

    url = os.environ.get("DATABASE_URL") or settings.DATABASE_URL
    assert any(f"/{name}" in url for name in ALLOWED_SCRATCH_DBS), (
        f"refusing to run outside scratch DBs {ALLOWED_SCRATCH_DBS}: {url}"
    )
    return url


def _run(coro):
    return asyncio.run(coro)


def test_insights_carry_real_order_ids_denominator_and_formula():
    async def body():
        import asyncpg

        conn = await asyncpg.connect(_db_url())
        try:
            rows = await conn.fetch(
                "SELECT headline, order_ids, denominator, formula FROM insights"
            )
            assert rows, "seed must generate at least one insight"
            for r in rows:
                assert r["denominator"] is not None and int(r["denominator"]) > 0, r["headline"]
                assert isinstance(r["formula"], str) and r["formula"].strip(), r["headline"]
                oids = [str(x) for x in (r["order_ids"] or [])]
                assert oids, r["headline"]
                for oid in oids:
                    uuid.UUID(oid)
                found = await conn.fetchval(
                    "SELECT count(*) FROM work_orders WHERE id = ANY($1::uuid[])", oids
                )
                assert int(found) == len(oids), (r["headline"], oids)
        finally:
            await conn.close()

    _run(body())


def test_insight_regeneration_is_idempotent():
    async def body():
        import asyncpg

        from app.services.insights import generate_insights

        conn = await asyncpg.connect(_db_url())
        try:
            before = await conn.fetchval("SELECT count(*) FROM insights")
            assert int(before) > 0, "seed must generate at least one insight"
            created = await generate_insights(conn)
            assert created == []
            after = await conn.fetchval("SELECT count(*) FROM insights")
            assert int(after) == int(before)
        finally:
            await conn.close()

    _run(body())


@pytest.fixture()
def api(monkeypatch):
    from fastapi.testclient import TestClient

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
        r = client.post("/api/v1/auth/login", json={"login": "manager1", "pin": "2222"})
        assert r.status_code == 200, r.text
        yield client, {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_analytics_insights_endpoint_exposes_evidence(api):
    client, headers = api
    r = client.get("/api/v1/analytics/insights", headers=headers)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items, "seed must generate at least one insight"
    for item in items:
        assert isinstance(item["order_ids"], list) and item["order_ids"], item["headline"]
        assert all(isinstance(x, str) for x in item["order_ids"]), item["headline"]
        assert isinstance(item["denominator"], int) and item["denominator"] > 0, item["headline"]
        assert isinstance(item["formula"], str) and item["formula"].strip(), item["headline"]
        assert isinstance(item["evidence"], list)

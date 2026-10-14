"""Live E2E stand preparation (dev/test only, never production).

Checks postgres/redis/s3 availability (short timeouts, clear errors),
applies migrations, applies the demo seed (idempotent), and prints::

    {"api": "http://127.0.0.1:8080", "accounts": {"master": "master1/3333", "worker": "worker01/1001"}}

The API server itself is NOT started here; it is assumed to be already
running (e.g. ``uvicorn app.main:app --port 8080``).

Usage (repo root as cwd / PYTHONPATH)::

    python -m scripts.e2e_stand
    python scripts/e2e_stand.py
"""

import asyncio
import json
import sys
from pathlib import Path

API_URL = "http://127.0.0.1:8080"
MASTER_CREDS = "master1/3333"
WORKER_CREDS = "worker01/1001"


def _bootstrap_sys_path() -> None:
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


_bootstrap_sys_path()


async def check_postgres() -> None:
    from app.config import settings

    import asyncpg

    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(settings.DATABASE_URL, timeout=3), timeout=5
        )
    except Exception as exc:
        raise RuntimeError(
            f"postgres unavailable at {settings.DATABASE_URL}: {exc}"
        ) from exc
    try:
        await asyncio.wait_for(conn.fetchval("SELECT 1"), timeout=5)
    except Exception as exc:
        raise RuntimeError(f"postgres check query failed: {exc}") from exc
    finally:
        await conn.close()


async def check_redis() -> None:
    from app.config import settings

    import redis.asyncio as aioredis

    try:
        client = aioredis.from_url(settings.REDIS_URL, socket_connect_timeout=2)
        try:
            await asyncio.wait_for(client.ping(), timeout=5)
        finally:
            await client.aclose()
    except Exception as exc:
        raise RuntimeError(
            f"redis unavailable at {settings.REDIS_URL}: {exc}"
        ) from exc


def check_s3() -> None:
    from app.config import settings
    from app.storage import get_s3

    try:
        s3 = get_s3()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(asyncio.wait_for(asyncio.to_thread(s3.list_buckets), timeout=8))
        finally:
            loop.close()
    except Exception as exc:
        raise RuntimeError(
            f"s3 unavailable at {settings.S3_ENDPOINT} "
            f"(bucket {settings.S3_BUCKET}): {exc}"
        ) from exc


async def main() -> None:
    from app.config import settings

    if settings.NODE_ENV == "production":
        raise RuntimeError("e2e_stand must not run with NODE_ENV=production")

    try:
        await check_postgres()
    except RuntimeError as exc:
        print(f"e2e_stand: {exc}", file=sys.stderr)
        print("e2e_stand: hint: start infra with `docker compose up -d postgres redis s3`",
              file=sys.stderr)
        sys.exit(2)
    try:
        await check_redis()
    except RuntimeError as exc:
        print(f"e2e_stand: {exc}", file=sys.stderr)
        print("e2e_stand: hint: start infra with `docker compose up -d postgres redis s3`",
              file=sys.stderr)
        sys.exit(2)
    try:
        await asyncio.to_thread(check_s3)
    except RuntimeError as exc:
        print(f"e2e_stand: {exc}", file=sys.stderr)
        print("e2e_stand: hint: start infra with `docker compose up -d postgres redis s3`",
              file=sys.stderr)
        sys.exit(2)

    from scripts.migrate import main as migrate_main
    from scripts.seed import main as seed_main

    await migrate_main()
    await seed_main()

    print(json.dumps(
        {"api": API_URL, "accounts": {"master": MASTER_CREDS, "worker": WORKER_CREDS}}
    ))


if __name__ == "__main__":
    asyncio.run(main())

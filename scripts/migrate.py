"""Приминение SQL-миграций, порт scripts/migrate.ts."""

import asyncio
from pathlib import Path

from app.db import get_pool


async def main() -> None:
    pool = await get_pool()
    from app.db import close_pool

    migration_dir = Path(__file__).resolve().parent.parent / "migrations"
    try:
        async with pool.acquire() as conn:
            # Serialize application starts without relying on process-local locks.
            await conn.execute("SELECT pg_advisory_lock(hashtext('naryadai_schema_migrations'))")
            try:
                await conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
                )
                for path in sorted(migration_dir.glob("*.sql")):
                    exists = await conn.fetchval(
                        "SELECT 1 FROM schema_migrations WHERE name=$1", path.name
                    )
                    if exists:
                        continue
                    async with conn.transaction():
                        await conn.execute(path.read_text(encoding="utf-8"))
                        await conn.execute(
                            "INSERT INTO schema_migrations(name) VALUES ($1)", path.name
                        )
                    print(f"Applied migration {path.name}")
            finally:
                await conn.execute(
                    "SELECT pg_advisory_unlock(hashtext('naryadai_schema_migrations'))"
                )
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

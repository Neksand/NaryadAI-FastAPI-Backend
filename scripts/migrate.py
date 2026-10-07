"""Приминение SQL-миграций, порт scripts/migrate.ts."""
import asyncio
from pathlib import Path

from app.db import get_pool


async def main() -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        files = sorted(p.name for p in (Path("migrations")).glob("*.sql"))
        for name in files:
            exists = await conn.fetchval("SELECT 1 FROM schema_migrations WHERE name=$1", name)
            if exists:
                continue
            sql = (Path("migrations") / name).read_text(encoding="utf-8")
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute("INSERT INTO schema_migrations(name) VALUES ($1)", name)
            print(f"Applied migration {name}")
    from app.db import close_pool
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

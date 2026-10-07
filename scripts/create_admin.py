"""Создание первого администратора, порт scripts/create-admin.ts."""
import asyncio

from app.config import settings
from app.db import close_pool, get_pool
from app.security import hash_pin


async def main() -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            exists = await conn.fetchrow("SELECT id FROM employees WHERE role='admin' AND deleted_at IS NULL LIMIT 1")
            if exists:
                raise RuntimeError("An administrator already exists")
            areas = await conn.fetch("SELECT id FROM areas WHERE deleted_at IS NULL")
            created = await conn.fetchrow(
                "INSERT INTO employees(login,pin_hash,full_name,role,shift,lang) VALUES ($1,$2,'Администратор системы','admin','1','ru') RETURNING id",
                settings.SEED_ADMIN_LOGIN, hash_pin(settings.SEED_ADMIN_PIN))
            for a in areas:
                await conn.execute("INSERT INTO employee_areas(employee_id,area_id) VALUES ($1,$2)", created["id"], a["id"])
            print(f"Created administrator login={settings.SEED_ADMIN_LOGIN}")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

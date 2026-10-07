"""Демо-данные, упрощённый порт scripts/seed.ts (идемпотентный, dev/test only)."""
import asyncio
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db import close_pool, get_pool
from app.security import hash_pin


async def ensure_user(pool, login: str, pin: str, full_name: str, role: str):
    row = await pool.fetchrow(
        """INSERT INTO employees(login,pin_hash,full_name,role,shift,current_status)
           VALUES ($1,$2,$3,$4,'1',CASE WHEN $4='worker' THEN 'free'::employee_status ELSE 'off'::employee_status END)
           ON CONFLICT (login) DO UPDATE SET full_name=EXCLUDED.full_name, role=EXCLUDED.role, enabled=true, deleted_at=NULL
           RETURNING id""", login, hash_pin(pin), full_name, role)
    return str(row["id"])


async def main() -> None:
    if settings.NODE_ENV == "production":
        raise RuntimeError("Demo seed data is disabled in production")
    pool = await get_pool()
    for spec in [("Дробление", "ДРБ"), ("Обогащение", "ОБГ"), ("РМЦ", "РМЦ"), ("Транспортировка", "ТРС")]:
        await pool.execute("INSERT INTO areas(name,code) VALUES ($1,$2) ON CONFLICT(code) DO NOTHING", spec[0], spec[1])
    admin = await ensure_user(pool, settings.SEED_ADMIN_LOGIN, settings.SEED_ADMIN_PIN, "Администратор системы", "admin")
    await ensure_user(pool, "manager1", "2222", "Сериков М.", "manager")
    await ensure_user(pool, "master1", "3333", "Ахметов Е.", "master")
    for i in range(5):
        await ensure_user(pool, f"worker{str(i+1).padStart(2,'0')}", str(1001 + i), f"Worker {i+1}", "worker")
    print(f"Seeded minimal demo data. Admin login: {settings.SEED_ADMIN_LOGIN} id={admin}")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

"""Real PostgreSQL regression: period filters, rating fairness and master scope.

Only temporary tables inside a rolled-back transaction are used. Production is
refused. This exercises the actual application SQL, not a fake SQL adapter.
"""

import asyncio
import json
import uuid

import asyncpg

from app.config import settings
from app.routers import analytics


async def main() -> None:
    if settings.NODE_ENV == "production":
        raise RuntimeError("Test harness is not for production")
    conn = await asyncpg.connect(settings.DATABASE_URL)
    tx = conn.transaction()
    await tx.start()
    original = analytics.get_pool
    try:
        await conn.execute("""
            CREATE TEMP TABLE employees(id uuid, full_name text, crew_id uuid);
            CREATE TEMP TABLE work_orders(id uuid, assignee_id uuid, crew_id uuid, status text,
              done_at timestamptz, due_at timestamptz, return_count int, issued_at timestamptz,
              area_id uuid, reject_reason_code text);
            CREATE TEMP TABLE ai_reviews(work_order_id uuid, attempt int, score float, master_score float);
            CREATE TEMP TABLE reasons(kind text, code text, disrespectful boolean);
            INSERT INTO reasons VALUES ('reject','no_materials',false),('reject','no_valid_reason',true);
        """)
        worker, area, closed = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        await conn.execute("INSERT INTO employees VALUES ($1,'SYNTHETIC regression',NULL)", worker)
        for i, (status, reason) in enumerate(
            [
                ("closed", None),
                ("issued", None),
                ("rejected", "no_materials"),
                ("rejected", "no_valid_reason"),
            ]
        ):
            await conn.execute(
                """INSERT INTO work_orders VALUES ($1,$2,NULL,$3,
                CASE WHEN $3='closed' THEN now() ELSE NULL END, now()+interval '1 hour',
                0, now(), $4, $5)""",
                closed if i == 0 else uuid.uuid4(),
                worker,
                status,
                area,
                reason,
            )
        await conn.execute("INSERT INTO ai_reviews VALUES ($1,1,80,NULL)", closed)

        async def test_connection():
            return conn

        analytics.get_pool = test_connection
        manager = {"role": "manager", "id": str(uuid.uuid4()), "areaIds": []}
        result = await analytics.ratings("employee", "2000-01-01", "2100-01-01", manager)
        rating = result["data"][0]
        assert rating["on_time"] == 1, rating
        assert rating["rejected"] == 0.25, rating
        assert rating["quality"] == 0.8, rating
        assert rating["rating"] == 77.1, rating
        restricted = await analytics.ratings("employee", None, None, {**manager, "role": "master"})
        assert restricted["data"] == [], restricted
        visible = await analytics.ratings(
            "employee", None, None, {**manager, "role": "master", "areaIds": [str(area)]}
        )
        assert len(visible["data"]) == 1
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "database": "PostgreSQL; temporary tables rolled back",
                    "checks": [
                        "ISO date filters bind without asyncpg type error",
                        "Pending work is not late",
                        "Excused rejection is not penalized",
                        "Master sees only assigned areas",
                    ],
                    "rating": rating["rating"],
                    "on_time": rating["on_time"],
                    "unjustified_rejection_share": rating["rejected"],
                },
                indent=2,
            )
        )
    finally:
        analytics.get_pool = original
        await tx.rollback()
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

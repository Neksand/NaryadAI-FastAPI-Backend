"""Regression checks against real SQL, isolated in rolled-back temporary tables."""

import asyncio
import json
import uuid

import asyncpg

from app.config import settings
from app.routers.analytics import _insight_scope
from app.services.insights import generate_insights


async def main():
    if settings.NODE_ENV == "production":
        raise RuntimeError("Verification requires an isolated demo database")
    conn = await asyncpg.connect(settings.DATABASE_URL)
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute("""
          CREATE TEMP TABLE equipment(id uuid, name text, name_kk text, area_id uuid);
          CREATE TEMP TABLE work_orders(id uuid, equipment_id uuid, kind text, status text,
            issued_at timestamptz, done_at timestamptz);
          CREATE TEMP TABLE employee_areas(employee_id uuid, area_id uuid);
          CREATE TEMP TABLE insights(scope jsonb);
        """)
        area, foreign, worker, pending, completed = (uuid.uuid4() for _ in range(5))
        for eq in (pending, completed):
            await conn.execute("INSERT INTO equipment VALUES ($1,$2,NULL,$3)", eq, str(eq), area)
            await conn.execute(
                "INSERT INTO work_orders VALUES ($1,$2,'unplanned','issued',now()-interval '1 day',NULL)",
                uuid.uuid4(),
                eq,
            )
        # Issuing a planned task does not mean maintenance happened.
        await conn.execute(
            "INSERT INTO work_orders VALUES ($1,$2,'planned','issued',now()-interval '3 days',NULL)",
            uuid.uuid4(),
            pending,
        )
        # An old task completed recently does count: use completion, not issue date.
        await conn.execute(
            "INSERT INTO work_orders VALUES ($1,$2,'planned','closed',now()-interval '60 days',now()-interval '2 days')",
            uuid.uuid4(),
            completed,
        )

        class DetectorConnection:
            def __init__(self):
                self.scopes = []

            async def fetch(self, query, *args):
                if "SELECT u.id" in query:
                    return await conn.fetch(query, *args)
                return []

            async def fetchval(self, *args):
                return False

            async def execute(self, query, *args):
                self.scopes.append(json.loads(args[2]))

        detector = DetectorConnection()
        await generate_insights(detector)
        assert detector.scopes == [{"equipment_id": str(completed)}], detector.scopes
        await conn.execute(
            "INSERT INTO insights VALUES ($1::jsonb)", json.dumps({"employee_id": str(worker)})
        )
        for a in (area, foreign):
            await conn.execute("INSERT INTO employee_areas VALUES ($1,$2)", worker, a)
        query = "SELECT count(*) FROM insights WHERE " + _insight_scope(1)
        assert await conn.fetchval(query, [area]) == 0
        assert await conn.fetchval(query, [area, foreign]) == 1
        assert await conn.fetchval(query, []) == 0
        assert await conn.fetchval(query, None) == 1
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "database": "PostgreSQL temporary tables, rolled back",
                    "checks": [
                        "Pending planned work cannot imply completed maintenance",
                        "Post-PPR window uses completion time",
                        "Master cannot see employee aggregate spanning another area",
                        "Master owning all employee areas can see aggregate",
                        "Empty master scope returns no employee aggregates",
                        "Manager global aggregate remains available",
                    ],
                },
                indent=2,
            )
        )
    finally:
        await tx.rollback()
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

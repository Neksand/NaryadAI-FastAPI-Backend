"""Verify synthetic ground truth against real PostgreSQL detector results.

Run after migrations + seed, with no production data: python -m scripts.verify_dataset.
The four target pairs are a controlled acceptance set, not a field ML benchmark.
"""

import asyncio
import json

from app.config import settings
from app.db import close_pool, get_pool
from app.services.insights import generate_insights


async def main() -> None:
    if settings.NODE_ENV == "production":
        raise RuntimeError("Synthetic verification is disabled in production")
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            marker = await conn.fetchval("SELECT value FROM settings WHERE key='demo_dataset_v2'")
            assert marker, "Expected seed-v2 synthetic database"
            counts = dict(
                await conn.fetchrow("""SELECT
                (SELECT count(*) FROM areas)::int areas,
                (SELECT count(*) FROM equipment)::int equipment,
                (SELECT count(*) FROM crews)::int crews,
                (SELECT count(*) FROM employees WHERE role='master')::int masters,
                (SELECT count(*) FROM employees WHERE role='worker')::int workers,
                (SELECT count(*) FROM fault_codes)::int faults,
                (SELECT count(*) FROM materials)::int materials,
                (SELECT count(*) FROM work_orders)::int orders,
                (SELECT extract(epoch FROM max(issued_at)-min(issued_at))/86400 FROM work_orders)::float history_days
                """)
            )
            assert all(
                counts[k] >= v
                for k, v in dict(
                    areas=4,
                    equipment=25,
                    crews=3,
                    masters=2,
                    workers=15,
                    faults=20,
                    materials=40,
                    orders=603,
                    history_days=90,
                ).items()
            ), counts
            invalid = await conn.fetchval("""SELECT count(*) FROM work_orders WHERE
                accepted_at < issued_at OR started_at < accepted_at OR done_at < started_at OR closed_at < done_at""")
            assert invalid == 0, "Lifecycle timestamps out of order"
            await generate_insights(conn)
            pairs = {
                (r["type"], r["inventory_no"])
                for r in await conn.fetch("""
                SELECT i.type, e.inventory_no FROM insights i
                JOIN equipment e ON e.id::text=i.scope->>'equipment_id'""")
            }
            expected = {
                ("repeat_failure", "EQ-0001"),
                ("post_ppr_failure", "EQ-0002"),
                ("material_anomaly", "EQ-0003"),
                ("failure_forecast", "EQ-0004"),
            }
            missing = expected - pairs
            assert not missing, f"Missing planted patterns: {sorted(missing)}"
            print(
                json.dumps(
                    {
                        "status": "PASS",
                        "synthetic": True,
                        "counts": counts,
                        "invalid_timestamp_rows": invalid,
                        "target_patterns_found": len(expected & pairs),
                        "target_patterns_total": len(expected),
                        "target_recall": len(expected & pairs) / len(expected),
                        "expected_pairs": sorted(expected),
                        "detected_pairs": sorted(pairs),
                        "metric_scope": "Four planted equipment/type pairs only; not overall detector precision or field accuracy.",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

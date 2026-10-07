"""Фоновые задачи: outbox publisher (500мс), deadline monitor (30с), insights (6ч)."""
import asyncio
import json

import redis.asyncio as aioredis

TELEGRAM_EVENTS = {"order.created", "order.overdue", "order.overdue_warning",
                   "order.escalated", "order.escalated_not_accepted", "order.ai_review_ready", "order.closed"}


async def outbox_loop() -> None:
    from app.config import settings
    from app.db import get_pool
    from app.services.ai_review import run_ai_review
    from app.services.reports import process_export

    redis = aioredis.from_url(settings.REDIS_URL)
    seq_key = "naryadai:events:seq"
    while True:
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        """UPDATE outbox_events SET locked_until=now()+interval '30 seconds', attempts=attempts+1
                           WHERE id=(SELECT id FROM outbox_events WHERE published_at IS NULL
                           AND (locked_until IS NULL OR locked_until < now()) ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
                           RETURNING *""")
                    if not row:
                        await asyncio.sleep(0.5)
                        continue
                    event = row["event"]
                    try:
                        if event == "ai.review_requested":
                            payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
                            await run_ai_review(conn, str(payload.get("work_order_id")))
                        elif event == "report.export_requested":
                            payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
                            await process_export(conn, str(payload.get("export_id")))
                        else:
                            seq = await redis.incr(seq_key)
                            channels = list(row["channels"] or [])
                            await redis.publish("naryadai:events:v1", json.dumps(
                                {"event": event, "channels": channels, "payload": row["payload"], "seq": seq}, default=str))
                            if event in TELEGRAM_EVENTS:
                                try:
                                    from app.notify import send_telegram
                                    p = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"] or "{}")
                                    await send_telegram(f"НарядAI · {event}: {json.dumps(p, default=str)[:400]}")
                                except Exception:
                                    pass
                        await conn.execute("UPDATE outbox_events SET published_at=now(), locked_until=NULL WHERE id=$1", row["id"])
                    except Exception as e:  # noqa: BLE001
                        await conn.execute("UPDATE outbox_events SET locked_until=NULL, last_error=$2 WHERE id=$1",
                                           row["id"], str(e)[:500])
                        await asyncio.sleep(0.2)
        except Exception:
            await asyncio.sleep(1.0)


async def insights_loop() -> None:
    """Regenerate rule-based insights every 6 hours."""
    await asyncio.sleep(60)
    while True:
        try:
            from app.db import get_pool
            from app.services.insights import generate_insights

            pool = await get_pool()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    created = await generate_insights(conn)
                    if created:
                        import logging
                        logging.getLogger("insights").info("generated %d insights", len(created))
        except Exception:
            pass
        await asyncio.sleep(6 * 3600)


async def deadline_loop() -> None:
    while True:
        try:
            from app.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                locked = await conn.fetchval("SELECT pg_try_advisory_lock(827410266)")
                if locked:
                    overdue = await conn.fetch(
                        """SELECT id, area_id FROM work_orders WHERE due_at < now()
                           AND status NOT IN ('done','ai_review','closed','rejected','cancelled')
                           AND (last_overdue_alert_at IS NULL OR last_overdue_alert_at < now() - interval '30 minutes')
                           LIMIT 100""")
                    for o in overdue:
                        await conn.execute(
                            "INSERT INTO outbox_events(event, channels, payload) VALUES ('order.overdue',$1,$2::jsonb)",
                            ["shift:current", f"order:{o['id']}"],
                            json.dumps({"id": str(o["id"]), "area_id": str(o["area_id"])}))
                        await conn.execute("UPDATE work_orders SET last_overdue_alert_at=now() WHERE id=$1", o["id"])
        except Exception:
            pass
        await asyncio.sleep(30)

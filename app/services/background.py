"""Фоновые задачи: outbox publisher (500мс), deadline monitor (30с), insights (6ч)."""
import asyncio
import json

import redis.asyncio as aioredis

TELEGRAM_EVENTS = {"order.created", "order.overdue", "order.overdue_warning",
                   "order.escalated", "order.escalated_not_accepted", "order.ai_review_ready", "order.closed"}

ACTION_NOTIFY = {
    "accept": "WORK_ORDER_ACCEPTED", "reject": "WORK_ORDER_REJECTED",
    "start": "WORK_ORDER_STARTED", "pause": "WORK_ORDER_PAUSED",
    "resume": "WORK_ORDER_STARTED", "complete": "WORK_ORDER_COMPLETED",
    "close": "WORK_ORDER_CLOSED", "return_to_rework": "WORK_ORDER_REWORK",
    "reassign": "WORK_ORDER_ASSIGNED", "cancel": "WORK_ORDER_REWORK",
}

ACTION_WS = {
    "accept": "WORK_ORDER_ACCEPTED", "queue": "WORK_ORDER_UPDATED",
    "reject": "WORK_ORDER_REJECTED", "start": "WORK_ORDER_STARTED",
    "pause": "WORK_ORDER_PAUSED", "resume": "WORK_ORDER_RESUMED",
    "complete": "WORK_ORDER_COMPLETED", "close": "WORK_ORDER_CLOSED",
    "return_to_rework": "WORK_ORDER_REWORK", "reassign": "WORK_ORDER_ASSIGNED",
    "cancel": "WORK_ORDER_UPDATED", "change_priority": "WORK_ORDER_UPDATED",
}


async def _notify_for_event(conn, event: str, payload: dict, channels: list[str]):
    """Central fan-out: persistent notification + typed WS event + optional Telegram.

    Returns (ws_type, notified_rows). Telegram failure never raises.
    """
    from app.notifications import notify, push_optional
    from app.routers.realtime import broadcast

    oid = payload.get("id") or payload.get("work_order_id")
    order = None
    if oid:
        try:
            order = await conn.fetchrow("SELECT id, number, master_id, assignee_id, crew_id FROM work_orders WHERE id=$1::uuid", str(oid))
        except Exception:
            order = None
    number = payload.get("number") or (order["number"] if order else "?")
    data = {**payload, "number": number}
    recipients = [c[5:] for c in channels if c.startswith("user:")]

    ntype, ws_type = None, event.upper().replace(".", "_")
    if event == "order.created":
        ntype, ws_type = "WORK_ORDER_ASSIGNED", "WORK_ORDER_CREATED"
        recipients = [r for r in recipients]
    elif event == "order.transition":
        action = payload.get("action", "")
        ntype, ws_type = ACTION_NOTIFY.get(action), ACTION_WS.get(action, "WORK_ORDER_UPDATED")
        if action in ("accept", "reject", "start", "pause", "complete") and order:
            recipients = [str(order["master_id"])]
        elif action in ("close", "return_to_rework") and order:
            recipients = [str(order["master_id"])]
            if order["assignee_id"]:
                recipients.append(str(order["assignee_id"]))
    elif event == "order.ai_review_ready":
        ntype, ws_type = "AI_INSPECTION_READY", "AI_INSPECTION_READY"
    elif event in ("order.overdue", "order.escalated"):
        ntype, ws_type = "DEADLINE_EXCEEDED", "DEADLINE_EXCEEDED"
    elif event == "order.overdue_warning":
        ntype, ws_type = "DEADLINE_WARNING", "DEADLINE_WARNING"
    elif event == "order.closed":
        ntype, ws_type = "WORK_ORDER_CLOSED", "WORK_ORDER_CLOSED"
    elif event == "order.rejected":
        ntype, ws_type = "WORK_ORDER_REJECTED", "WORK_ORDER_REJECTED"
    elif event == "order.reassigned":
        ntype, ws_type = "WORK_ORDER_ASSIGNED", "WORK_ORDER_ASSIGNED"
    elif event == "employee.status_changed":
        ws_type = "WORKER_STATUS_CHANGED"

    rows = []
    if ntype and recipients and oid:
        try:
            rows = await notify(conn, recipients, ntype, data, "work_order", str(oid))
        except Exception:
            rows = []
    try:
        await broadcast(channels, ws_type, {**data, "notifications": [r["id"] for r in rows]})
        if rows:
            await broadcast(channels, "NOTIFICATION_CREATED",
                            {"notifications": rows, "related_entity_type": "work_order",
                             "related_entity_id": str(oid) if oid else None})
    except Exception:
        pass
    try:
        await push_optional(rows)
    except Exception:
        pass
    return ws_type, rows


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
                            payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"] or "{}")
                            await redis.publish("naryadai:events:v1", json.dumps(
                                {"event": event, "channels": channels, "payload": row["payload"], "seq": seq}, default=str))
                            await _notify_for_event(conn, event, payload, channels)
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

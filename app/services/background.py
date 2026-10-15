"""Фоновые задачи: outbox publisher (500мс), deadline monitor (30с), insights (6ч)."""

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis

ACTION_NOTIFY = {
    "accept": "WORK_ORDER_ACCEPTED",
    "reject": "WORK_ORDER_REJECTED",
    "start": "WORK_ORDER_STARTED",
    "pause": "WORK_ORDER_PAUSED",
    "resume": "WORK_ORDER_STARTED",
    "complete": "WORK_ORDER_COMPLETED",
    "close": "WORK_ORDER_CLOSED",
    "return_to_rework": "WORK_ORDER_REWORK",
    "reassign": "WORK_ORDER_ASSIGNED",
    "cancel": "WORK_ORDER_REWORK",
}

ACTION_WS = {
    "accept": "WORK_ORDER_ACCEPTED",
    "queue": "WORK_ORDER_UPDATED",
    "reject": "WORK_ORDER_REJECTED",
    "start": "WORK_ORDER_STARTED",
    "pause": "WORK_ORDER_PAUSED",
    "resume": "WORK_ORDER_RESUMED",
    "complete": "WORK_ORDER_COMPLETED",
    "close": "WORK_ORDER_CLOSED",
    "return_to_rework": "WORK_ORDER_REWORK",
    "reassign": "WORK_ORDER_ASSIGNED",
    "cancel": "WORK_ORDER_UPDATED",
    "change_priority": "WORK_ORDER_UPDATED",
}


async def _notify_for_event(
    conn: Any, event: str, payload: dict[str, Any], channels: list[str]
) -> tuple[str, list[dict[str, Any]]]:
    """Central fan-out: persistent notification + typed WS event.

    Returns (ws_type, notified_rows).
    """
    from app.notifications import notify
    from app.routers.realtime import broadcast

    oid = payload.get("id") or payload.get("work_order_id")
    order = None
    if oid:
        try:
            order = await conn.fetchrow(
                "SELECT id, number, master_id, assignee_id, crew_id FROM work_orders WHERE id=$1::uuid",
                str(oid),
            )
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
        if action == "close" and payload.get("to") == "rework":
            action = "return_to_rework"
        ntype, ws_type = ACTION_NOTIFY.get(action), ACTION_WS.get(action, "WORK_ORDER_UPDATED")
        if action in ("accept", "reject", "start", "pause", "resume", "complete") and order:
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
    elif event == "order.deadline_30min":
        ntype, ws_type = "DEADLINE_30MIN", "DEADLINE_30MIN"
    elif event == "order.long_overdue":
        ntype, ws_type = "LONG_OVERDUE_120MIN", "LONG_OVERDUE_120MIN"
    elif event == "order.not_accepted":
        ntype, ws_type = "NOT_ACCEPTED_WITH_CANDIDATE", "NOT_ACCEPTED_WITH_CANDIDATE"
    elif event == "order.closed":
        ntype, ws_type = "WORK_ORDER_CLOSED", "WORK_ORDER_CLOSED"
    elif event == "order.rejected":
        ntype, ws_type = "WORK_ORDER_REJECTED", "WORK_ORDER_REJECTED"
    elif event == "order.reassigned":
        ntype, ws_type = "WORK_ORDER_ASSIGNED", "WORK_ORDER_ASSIGNED"
    elif event == "employee.status_changed":
        ws_type = "WORKER_STATUS_CHANGED"

    if order and (
        event
        in (
            "order.ai_review_ready",
            "order.overdue",
            "order.escalated",
            "order.overdue_warning",
            "order.deadline_30min",
            "order.long_overdue",
            "order.not_accepted",
        )
        or (
            event == "order.transition"
            and payload.get("action") in ("close", "return_to_rework", "reassign")
        )
    ):
        # A crew assignment has no assignee_id. Notify its active members too,
        # and on reassignment avoid treating the former worker as a new assignee.
        recipients = [] if payload.get("action") == "reassign" else [str(order["master_id"])]
        if order["assignee_id"]:
            recipients.append(str(order["assignee_id"]))
        elif order["crew_id"]:
            recipients.extend(
                str(member["id"])
                for member in await conn.fetch(
                    "SELECT id FROM employees WHERE crew_id=$1::uuid AND role='worker' AND enabled=true AND deleted_at IS NULL",
                    str(order["crew_id"]),
                )
            )
        channels = list(dict.fromkeys([*channels, *(f"user:{uid}" for uid in recipients)]))

    rows = []
    if ntype and recipients and oid:
        try:
            rows = await notify(conn, recipients, ntype, data, "work_order", str(oid))
        except Exception:
            rows = []
    try:
        await broadcast(channels, ws_type, {**data, "notifications": [r["id"] for r in rows]})
        if rows:
            await broadcast(
                channels,
                "NOTIFICATION_CREATED",
                {
                    "notifications": rows,
                    "related_entity_type": "work_order",
                    "related_entity_id": str(oid) if oid else None,
                },
            )
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
            async with pool.acquire() as conn, conn.transaction():
                row = await conn.fetchrow(
                    """UPDATE outbox_events SET locked_until=now()+interval '30 seconds', attempts=attempts+1
                       WHERE id=(SELECT id FROM outbox_events WHERE published_at IS NULL
                       AND (locked_until IS NULL OR locked_until < now()) ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
                       RETURNING *"""
                )
                if not row:
                    await asyncio.sleep(0.5)
                    continue
                event = row["event"]
                try:
                    if event == "ai.review_requested":
                        payload = (
                            row["payload"]
                            if isinstance(row["payload"], dict)
                            else json.loads(row["payload"])
                        )
                        await run_ai_review(conn, str(payload.get("work_order_id")))
                    elif event == "report.export_requested":
                        payload = (
                            row["payload"]
                            if isinstance(row["payload"], dict)
                            else json.loads(row["payload"])
                        )
                        await process_export(conn, str(payload.get("export_id")))
                    else:
                        seq = await redis.incr(seq_key)
                        channels = list(row["channels"] or [])
                        payload = (
                            row["payload"]
                            if isinstance(row["payload"], dict)
                            else json.loads(row["payload"] or "{}")
                        )
                        await redis.publish(
                            "naryadai:events:v1",
                            json.dumps(
                                {
                                    "event": event,
                                    "channels": channels,
                                    "payload": row["payload"],
                                    "seq": seq,
                                },
                                default=str,
                            ),
                        )
                        await _notify_for_event(conn, event, payload, channels)
                    await conn.execute(
                        "UPDATE outbox_events SET published_at=now(), locked_until=NULL WHERE id=$1",
                        row["id"],
                    )
                except Exception as e:
                    await conn.execute(
                        "UPDATE outbox_events SET locked_until=NULL, last_error=$2 WHERE id=$1",
                        row["id"],
                        str(e)[:500],
                    )
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
            async with pool.acquire() as conn, conn.transaction():
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
                    await _overdue_check(conn)
                    for check in (_due_soon_check, _long_overdue_check, _not_accepted_check):
                        with contextlib.suppress(Exception):
                            await check(conn)
        except Exception:
            pass
        await asyncio.sleep(30)


# --- Deadline engine ---
# 1. due-30мин: предупреждение за 30 мин до срока, один раз (дедуп по notifications за час).
# 2. overdue: повтор просрочки каждые 30 мин.
# 3. long-overdue: просрочка >=120 мин — всем руководителям (role='manager').
# 4. unaccepted: critical не принят >3 мин / прочие >10 мин — мастеру участка
#    с топ-1 свободным исполнителем (current_status='free').

DUE_SOON_MINUTES = 30
OVERDUE_REPEAT_MINUTES = 30
LONG_OVERDUE_MINUTES = 120
NOT_ACCEPTED_CRITICAL_MINUTES = 3
NOT_ACCEPTED_DEFAULT_MINUTES = 10
DEDUP_MINUTES = 60


def _field(row: Any, name: str, default: Any = None) -> Any:
    """Читать поле из dict / asyncpg.Record / объекта без KeyError."""
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        pass
    except Exception:
        return default
    try:
        return getattr(row, name)
    except AttributeError:
        return default


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None


def _minutes_until(due_at: Any) -> float | None:
    dt = _as_utc(due_at)
    if dt is None:
        if isinstance(due_at, (int, float)):
            return float(due_at)
        return None
    return (dt - datetime.now(UTC)).total_seconds() / 60


def _minutes_since(ts: Any) -> float | None:
    dt = _as_utc(ts)
    if dt is None:
        if isinstance(ts, (int, float)):
            return float(ts)
        return None
    return (datetime.now(UTC) - dt).total_seconds() / 60


def not_accepted_threshold(priority: str | None) -> int:
    """Аварийный — 3 мин, любой прочий — 10 мин."""
    return NOT_ACCEPTED_CRITICAL_MINUTES if priority == "critical" else NOT_ACCEPTED_DEFAULT_MINUTES


def should_send_due_soon(minutes_left: float | None, has_recent: bool) -> bool:
    return has_recent is False and minutes_left is not None and 0 < minutes_left <= DUE_SOON_MINUTES


def should_escalate_long_overdue(minutes_overdue: float | None) -> bool:
    return minutes_overdue is not None and minutes_overdue >= LONG_OVERDUE_MINUTES


def should_send_not_accepted(priority: str | None, waited_minutes: float | None) -> bool:
    return waited_minutes is not None and waited_minutes > not_accepted_threshold(priority)


def pick_free_candidate(candidates: Any) -> Any | None:
    """Топ-1 исполнитель со статусом 'free'; иначе None."""
    for cand in candidates or []:
        status = _field(cand, "current_status", _field(cand, "status"))
        if status == "free":
            return cand
    return None


async def _has_recent_notification(
    conn: Any, entity_id: Any, ntype: str, within_minutes: int = DEDUP_MINUTES
) -> bool:
    try:
        row = await conn.fetchval(
            """SELECT 1 FROM notifications WHERE related_entity_id=$1::uuid AND type=$2
               AND created_at > now() - ($3::int * interval '1 minute') LIMIT 1""",
            str(entity_id),
            ntype,
            int(within_minutes),
        )
        return bool(row)
    except Exception:
        return False


async def _overdue_check(conn: Any) -> int:
    """Повторы просрочки каждые 30 мин (троттлинг через last_overdue_alert_at)."""
    overdue = await conn.fetch(
        """SELECT id, area_id FROM work_orders WHERE due_at < now()
           AND status NOT IN ('done','ai_review','closed','rejected','cancelled')
           AND (last_overdue_alert_at IS NULL OR last_overdue_alert_at < now() - interval '30 minutes')
           LIMIT 100"""
    )
    for o in overdue:
        await conn.execute(
            "INSERT INTO outbox_events(event, channels, payload) VALUES ('order.overdue',$1,$2::jsonb)",
            ["shift:current", f"order:{_field(o, 'id')}"],
            json.dumps({"id": str(_field(o, "id")), "area_id": str(_field(o, "area_id"))}),
        )
        await conn.execute(
            "UPDATE work_orders SET last_overdue_alert_at=now() WHERE id=$1",
            _field(o, "id"),
        )
    return len(overdue)


async def _due_soon_check(conn: Any) -> int:
    """Предупреждение за 30 мин до срока — один раз (дедуп за последний час)."""
    rows = await conn.fetch(
        """SELECT id, number, area_id, assignee_id, master_id, due_at FROM work_orders
           WHERE due_at > now() AND due_at <= now() + interval '30 minutes'
           AND status NOT IN ('done','ai_review','closed','rejected','cancelled')
           AND (last_deadline_reminder_at IS NULL OR last_deadline_reminder_at < now() - interval '60 minutes')
           LIMIT 100"""
    )
    sent = 0
    for o in rows:
        oid = _field(o, "id")
        if oid is None:
            continue
        minutes_left = _minutes_until(_field(o, "due_at"))
        if minutes_left is None:
            continue
        if await _has_recent_notification(conn, oid, "DEADLINE_30MIN", DEDUP_MINUTES):
            continue
        if not should_send_due_soon(minutes_left, False):
            continue
        recipients = [
            str(r) for r in (_field(o, "assignee_id"), _field(o, "master_id")) if r is not None
        ]
        if not recipients:
            continue
        channels = [f"user:{r}" for r in recipients] + [f"order:{oid}"]
        await conn.execute(
            "INSERT INTO outbox_events(event, channels, payload) VALUES ('order.deadline_30min',$1,$2::jsonb)",
            channels,
            json.dumps(
                {
                    "id": str(oid),
                    "number": _field(o, "number"),
                    "area_id": str(_field(o, "area_id")),
                    "minutes_left": int(minutes_left),
                }
            ),
        )
        await conn.execute(
            "UPDATE work_orders SET last_deadline_reminder_at=now() WHERE id=$1",
            oid,
        )
        sent += 1
    return sent


async def _long_overdue_check(conn: Any) -> int:
    """Просрочка >=120 мин — всем руководителям (role='manager')."""
    rows = await conn.fetch(
        """SELECT id, number, area_id, due_at FROM work_orders
           WHERE due_at < now() - interval '120 minutes'
           AND status NOT IN ('done','ai_review','closed','rejected','cancelled')
           AND (escalated_at IS NULL OR escalated_at < now() - interval '60 minutes')
           LIMIT 100"""
    )
    targets = []
    for o in rows:
        if _field(o, "id") is None:
            continue
        minutes_overdue = _minutes_since(_field(o, "due_at"))
        if minutes_overdue is None:
            continue
        if should_escalate_long_overdue(minutes_overdue):
            targets.append((o, int(minutes_overdue)))
    if not targets:
        return 0
    managers = await conn.fetch(
        "SELECT id, role FROM employees WHERE role='manager' AND enabled AND deleted_at IS NULL LIMIT 50"
    )
    mids = [str(_field(m, "id")) for m in managers if _field(m, "role") == "manager"]
    if not mids:
        return 0
    for o, minutes_overdue in targets:
        oid = _field(o, "id")
        await conn.execute(
            "INSERT INTO outbox_events(event, channels, payload) VALUES ('order.long_overdue',$1,$2::jsonb)",
            [f"user:{m}" for m in mids] + [f"order:{oid}"],
            json.dumps(
                {
                    "id": str(oid),
                    "number": _field(o, "number"),
                    "area_id": str(_field(o, "area_id")),
                    "minutes_overdue": minutes_overdue,
                }
            ),
        )
        await conn.execute("UPDATE work_orders SET escalated_at=now() WHERE id=$1", oid)
    return len(targets)


async def _not_accepted_check(conn: Any) -> int:
    """Не принят: critical >3 мин / прочие >10 мин — мастеру + топ-1 free исполнитель."""
    rows = await conn.fetch(
        """SELECT id, number, area_id, master_id, assignee_id, priority, issued_at, created_at
           FROM work_orders WHERE status='issued'
           AND ((priority='critical' AND issued_at < now() - interval '3 minutes')
             OR (priority <> 'critical' AND issued_at < now() - interval '10 minutes'))
           AND (last_not_accepted_alert_at IS NULL OR last_not_accepted_alert_at < now() - interval '30 minutes')
           LIMIT 100"""
    )
    targets = []
    for o in rows:
        if _field(o, "id") is None:
            continue
        ts = _field(o, "issued_at", _field(o, "created_at"))
        waited = _minutes_since(ts)
        if waited is None or not should_send_not_accepted(_field(o, "priority"), waited):
            continue
        targets.append((o, int(waited)))
    if not targets:
        return 0
    free_rows = await conn.fetch(
        "SELECT id, full_name, current_status FROM employees "
        "WHERE current_status='free' AND enabled AND deleted_at IS NULL LIMIT 5"
    )
    candidate = pick_free_candidate(free_rows)
    sent = 0
    for o, waited in targets:
        oid = _field(o, "id")
        master_id = _field(o, "master_id")
        if master_id is None:
            continue
        await conn.execute(
            "INSERT INTO outbox_events(event, channels, payload) VALUES ('order.not_accepted',$1,$2::jsonb)",
            [f"user:{master_id}", f"order:{oid}"],
            json.dumps(
                {
                    "id": str(oid),
                    "number": _field(o, "number"),
                    "area_id": str(_field(o, "area_id")),
                    "priority": _field(o, "priority"),
                    "minutes_waiting": waited,
                    "candidate_id": str(_field(candidate, "id")) if candidate is not None else None,
                    "candidate_name": _field(candidate, "full_name")
                    if candidate is not None
                    else None,
                }
            ),
        )
        await conn.execute(
            "UPDATE work_orders SET last_not_accepted_alert_at=now() WHERE id=$1",
            oid,
        )
        sent += 1
    return sent

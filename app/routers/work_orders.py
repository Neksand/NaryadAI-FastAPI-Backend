import json
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.db import get_pool
from app.deps import (
    allowed_actions, assert_can_read_order, decode_cursor, encode_cursor, get_current_user,
    require_role, run_idempotent,
)
from app.errors import AppError, conflict, forbidden, not_found, validation_error
from app.security import sha256_hex

router = APIRouter()

TERMINAL_DONE = {"done", "ai_review", "closed", "cancelled"}

WORKER_TRANSITIONS = {
    "accept": ["issued", "queued"], "queue": ["issued"],
    "reject": ["issued", "queued", "accepted"], "start": ["accepted", "rework"],
    "pause": ["in_progress"], "resume": ["paused"], "complete": ["in_progress"],
}
MASTER_TRANSITIONS = {
    "close": ["ai_review", "rework"], "return_to_rework": ["ai_review"],
    "reassign": ["issued", "queued", "rejected", "accepted", "paused"],
    "cancel": ["issued", "accepted", "queued", "rejected", "in_progress", "paused", "rework", "ai_review"],
    "change_priority": ["issued", "accepted", "queued", "rejected", "in_progress", "paused", "rework", "ai_review"],
}


class CreateWO(BaseModel):
    kind: Literal["planned", "unplanned"]
    description: str = Field(min_length=3, max_length=4000)
    description_source: Literal["text", "voice"] = "text"
    equipment_id: uuid.UUID
    assignee_id: uuid.UUID | None = None
    crew_id: uuid.UUID | None = None
    priority: Literal["critical", "high", "normal", "planned"]
    due_at: datetime
    norm_minutes: int | None = Field(default=None, gt=0, le=100_000)
    equipment_stopped: bool = False
    comment: str | None = Field(default=None, max_length=4000)
    photo_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5)


class MaterialItem(BaseModel):
    material_id: uuid.UUID
    quantity: float = Field(gt=0, le=1_000_000)


class TransitionForm(BaseModel):
    work_done_text: str | None = Field(default=None, max_length=4000)
    fault_code_id: uuid.UUID | None = None
    materials: list[MaterialItem] = Field(default_factory=list, max_length=100)
    equipment_restored: bool | None = None
    comment: str | None = Field(default=None, max_length=4000)


class TransitionIn(BaseModel):
    action: Literal["accept", "queue", "reject", "start", "pause", "resume", "complete",
                    "close", "return_to_rework", "reassign", "cancel", "change_priority"]
    decision: Literal["agree_ai", "override"] | None = None
    score: int | None = Field(default=None, ge=0, le=100)
    reason_code: str | None = None
    comment: str | None = Field(default=None, max_length=4000)
    assignee_id: uuid.UUID | None = None
    crew_id: uuid.UUID | None = None
    priority: Literal["critical", "high", "normal", "planned"] | None = None
    client_at: datetime | None = None
    photo_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10)
    form: TransitionForm | None = None


class PatchWO(BaseModel):
    priority: Literal["critical", "high", "normal", "planned"] | None = None
    due_at: datetime | None = None
    comment: str | None = Field(default=None, max_length=4000)


class ReviewDecision(BaseModel):
    decision: Literal["agree_ai", "override"]
    score: int | None = Field(default=None, ge=0, le=100)
    comment: str | None = Field(default=None, max_length=4000)


def _row_to_dict(r) -> dict:
    d = dict(r)
    for k, v in list(d.items()):
        if isinstance(v, uuid.UUID):
            d[k] = str(v)
        elif isinstance(v, datetime):
            d[k] = v.isoformat()
    return d


async def _load_full(order_id: str) -> dict:
    pool = await get_pool()
    row = await pool.fetchrow(
        """SELECT w.*, jsonb_build_object('id',e.id,'name',e.name,'inventory_no',e.inventory_no,'type',e.type,'criticality',e.criticality) AS equipment,
                  jsonb_build_object('id',a.id,'name',a.name,'name_kk',a.name_kk,'code',a.code) AS area,
                  (w.due_at < now() AND w.status NOT IN ('done','ai_review','closed','rejected','cancelled')) AS is_overdue
           FROM work_orders w JOIN equipment e ON e.id=w.equipment_id JOIN areas a ON a.id=w.area_id WHERE w.id=$1""",
        order_id)
    if not row:
        raise not_found("Наряд не найден")
    d = _row_to_dict(row)
    photos = await pool.fetch("SELECT id, kind, object_key AS file_key, content_type, taken_at, author_id FROM photos WHERE work_order_id=$1 ORDER BY created_at", order_id)
    mats = await pool.fetch("SELECT mw.id, mw.material_id, m.name, mw.quantity, mw.unit FROM material_writeoffs mw JOIN materials m ON m.id=mw.material_id WHERE mw.work_order_id=$1 ORDER BY mw.created_at", order_id)
    review = await pool.fetchrow("SELECT * FROM ai_reviews WHERE work_order_id=$1 ORDER BY attempt DESC LIMIT 1", order_id)
    d["photos"] = [_row_to_dict(p) for p in photos]
    d["materials"] = [_row_to_dict(m) for m in mats]
    d["ai_review"] = _row_to_dict(review) if review else None
    return d


def _request_hash(method: str, url: str, body: dict) -> str:
    return sha256_hex(json.dumps({"method": method, "url": url, "body": body}, sort_keys=True, default=str))


async def _refresh_status(conn, employee_id: str):
    cur = await conn.fetchrow("SELECT current_status FROM employees WHERE id=$1 AND enabled=true", employee_id)
    if not cur or cur["current_status"] == "off":
        return
    active = await conn.fetch(
        """SELECT id, number, status FROM work_orders WHERE (assignee_id=$1 OR crew_id=(SELECT crew_id FROM employees WHERE id=$1))
           AND status IN ('issued','accepted','queued','in_progress','paused','rework') ORDER BY issued_at""", employee_id)
    running = next((r for r in active if r["status"] == "in_progress"), None)
    status = "busy" if running else ("queue" if active else "free")
    await conn.execute("UPDATE employees SET current_status=$2, updated_at=now() WHERE id=$1", employee_id, status)
    await conn.execute(
        "INSERT INTO outbox_events(event, channels, payload) VALUES ('employee.status_changed', $1, $2::jsonb)",
        ["shift:current"], json.dumps({"id": employee_id, "status": status,
            "current_order": {"id": str(running["id"]), "number": running["number"]} if running else None,
            "queue_count": len([r for r in active if r["status"] != "in_progress"])}))


@router.get("/work-orders")
async def list_orders(
    request: Request, limit: int = Query(50, ge=1, le=100), cursor: str | None = None,
    area_id: str | None = None, equipment_id: str | None = None, assignee_id: str | None = None,
    crew_id: str | None = None, priority: str | None = None, status: str | None = None,
    overdue: bool | None = None, from_: str | None = Query(default=None, alias="from"),
    to: str | None = None, shift: str | None = None, kind: str | None = None,
    user: dict = Depends(get_current_user),
):
    pool = await get_pool()
    params: list = []
    where: list[str] = []

    def add(clause_fn, value):
        params.append(value)
        where.append(clause_fn(f"${len(params)}"))

    if user["role"] == "worker":
        params.extend([user["id"], user["crewId"]])
        n = len(params)
        where.append(f"(w.assignee_id = ${n-1} OR (${n}::uuid IS NOT NULL AND w.crew_id = ${n}::uuid))")
    elif user["role"] == "master":
        if not user["areaIds"]:
            where.append("false")
        else:
            add(lambda p: f"w.area_id = ANY({p}::uuid[])", user["areaIds"])
    if area_id: add(lambda p: f"w.area_id = {p}::uuid", area_id)
    if equipment_id: add(lambda p: f"w.equipment_id = {p}::uuid", equipment_id)
    if assignee_id: add(lambda p: f"w.assignee_id = {p}::uuid", assignee_id)
    if crew_id: add(lambda p: f"w.crew_id = {p}::uuid", crew_id)
    if kind: add(lambda p: f"w.kind = {p}", kind)
    if priority: add(lambda p: f"w.priority = ANY({p}::work_order_priority[])", priority.split(","))
    if status: add(lambda p: f"w.status = ANY({p}::work_order_status[])", status.split(","))
    if overdue:
        where.append("w.due_at < now() AND w.status NOT IN ('done','ai_review','closed','rejected','cancelled')")
    if from_: add(lambda p: f"w.issued_at >= {p}::timestamptz", from_)
    if to: add(lambda p: f"w.issued_at < {p}::timestamptz", to)
    cur = decode_cursor(cursor)
    if cur:
        params.extend([cur["issued_at"], cur["id"]])
        where.append(f"(w.issued_at, w.id) < (${len(params)-1}::timestamptz, ${len(params)}::uuid)")
    params.append(limit + 1)
    sql = f"""SELECT w.id, w.number, w.issued_at, w.kind, w.description, w.area_id, w.equipment_id, w.assignee_id, w.crew_id,
              w.priority, w.due_at, w.status, w.norm_minutes, w.done_at, w.closed_at,
              (w.due_at < now() AND w.status NOT IN ('done','ai_review','closed','rejected','cancelled')) AS is_overdue
              FROM work_orders w {'WHERE ' + ' AND '.join(where) if where else ''}
              ORDER BY w.issued_at DESC, w.id DESC LIMIT ${len(params)}"""
    rows = await pool.fetch(sql, *params)
    has_more = len(rows) > limit
    items = [_row_to_dict(r) for r in rows[:limit]]
    for it in items:
        it["allowed_actions"] = allowed_actions(user, {**it, "assignee_id": it.get("assignee_id"), "crew_id": it.get("crew_id"), "area_id": str(it.get("area_id"))})
    nxt = None
    if has_more and len(rows) >= limit:
        last = rows[limit - 1]
        nxt = encode_cursor(str(last["id"]), last["issued_at"].isoformat() if isinstance(last["issued_at"], datetime) else str(last["issued_at"]))
    return {"items": items, "next_cursor": nxt}


@router.post("/work-orders", status_code=201)
async def create_order(body: CreateWO, request: Request, user: dict = Depends(get_current_user)):
    require_role(user, "master")
    if body.due_at.timestamp() * 1000 <= datetime.now(timezone.utc).timestamp() * 1000:
        raise validation_error([{"path": "due_at", "message": "Срок должен быть в будущем"}])
    if int(bool(body.assignee_id)) + int(bool(body.crew_id)) != 1:
        raise validation_error([{"path": "assignee_id", "message": "Укажите ровно одного исполнителя или одну бригаду"}])
    key = request.headers.get("idempotency-key") or f"auto-{uuid.uuid4()}"
    h = _request_hash("POST", "/work-orders", body.model_dump(mode="json"))

    async def action(conn):
        eq = await conn.fetchrow("SELECT id, area_id FROM equipment WHERE id=$1 AND deleted_at IS NULL", str(body.equipment_id))
        if not eq:
            raise not_found("Оборудование не найдено")
        area_id = str(eq["area_id"])
        if area_id not in user["areaIds"]:
            raise forbidden("Нет доступа к участку оборудования")
        if body.assignee_id:
            a = await conn.fetchrow(
                """SELECT e.id FROM employees e JOIN employee_areas ea ON ea.employee_id=e.id
                   WHERE e.id=$1 AND e.role='worker' AND e.enabled=true AND e.deleted_at IS NULL AND ea.area_id=$2::uuid""",
                str(body.assignee_id), area_id)
            if not a:
                raise validation_error([{"path": "assignee_id", "message": "Исполнитель недоступен на выбранном участке"}])
        if body.crew_id:
            c = await conn.fetchrow("SELECT id FROM crews WHERE id=$1 AND area_id=$2::uuid AND deleted_at IS NULL", str(body.crew_id), area_id)
            if not c:
                raise validation_error([{"path": "crew_id", "message": "Бригада не относится к участку оборудования"}])
        if body.photo_ids:
            n = await conn.fetchval("SELECT count(*) FROM photos WHERE id = ANY($1::uuid[]) AND author_id=$2 AND kind='before' AND work_order_id IS NULL",
                                    [str(p) for p in body.photo_ids], user["id"])
            if int(n) != len(body.photo_ids):
                raise validation_error([{"path": "photo_ids", "message": "Фото недоступно или уже связано с нарядом"}])
        number = await conn.fetchval("UPDATE work_order_counters SET value=value+1 WHERE key='number' RETURNING value")
        row = await conn.fetchrow(
            """INSERT INTO work_orders(number,kind,description,description_source,area_id,equipment_id,assignee_id,crew_id,master_id,priority,due_at,norm_minutes,comment,equipment_stopped_at)
               VALUES ($1,$2,$3,$4,$5::uuid,$6::uuid,$7::uuid,$8::uuid,$9::uuid,$10,$11,$12,$13,CASE WHEN $14 THEN now() ELSE NULL END)
               RETURNING id, number, status""",
            int(number), body.kind, body.description.strip(), body.description_source, area_id, str(body.equipment_id),
            str(body.assignee_id) if body.assignee_id else None, str(body.crew_id) if body.crew_id else None,
            user["id"], body.priority, body.due_at, body.norm_minutes, body.comment,
            body.equipment_stopped or body.priority == "critical")
        oid = str(row["id"])
        if body.photo_ids:
            await conn.execute("UPDATE photos SET work_order_id=$1::uuid WHERE id = ANY($2::uuid[])", oid, [str(p) for p in body.photo_ids])
        await conn.execute("INSERT INTO work_order_events(work_order_id, actor_id, action, comment, payload) VALUES ($1::uuid,$2::uuid,'issued',$3,$4::jsonb)",
                           oid, user["id"], body.comment, json.dumps({"status": "issued"}))
        uids = [str(body.assignee_id)] if body.assignee_id else [str(r["id"]) for r in await conn.fetch("SELECT id FROM employees WHERE crew_id=$1::uuid AND role='worker' AND enabled=true AND deleted_at IS NULL", str(body.crew_id))]
        for e in uids:
            await _refresh_status(conn, e)
        await conn.execute("INSERT INTO outbox_events(event, channels, payload) VALUES ('order.created',$1,$2::jsonb)",
                           ["shift:current", f"order:{oid}"] + [f"user:{u}" for u in uids],
                           json.dumps({"id": oid, "number": int(number), "area_id": area_id, "status": "issued"}))
        return {"statusCode": 201, "body": {"id": oid, "number": int(number), "status": row["status"]}}

    res = await run_idempotent(key, user["id"], h, action)
    return {**res["body"], "replayed": res["replayed"]}


@router.get("/work-orders/my", summary="My work orders (worker)")
async def my_orders(request: Request, limit: int = Query(50, ge=1, le=100), cursor: str | None = None,
                   user: dict = Depends(get_current_user)):
    require_role(user, "worker")
    return await list_orders(request, limit=limit, cursor=cursor, area_id=None, equipment_id=None,
                             assignee_id=user["id"], crew_id=None, priority=None, status=None,
                             overdue=None, from_=None, to=None, shift=None, kind=None, user=user)


@router.get("/work-orders/my/active", summary="My active work orders (worker)")
async def my_active(request: Request, user: dict = Depends(get_current_user)):
    require_role(user, "worker")
    return await list_orders(request, limit=100, cursor=None, area_id=None, equipment_id=None,
                             assignee_id=user["id"], crew_id=None, priority=None,
                             status="issued,queued,accepted,in_progress,paused,rework",
                             overdue=None, from_=None, to=None, shift=None, kind=None, user=user)


@router.get("/work-orders/my/history", summary="My work-order history (worker)")
async def my_history(request: Request, limit: int = Query(50, ge=1, le=100), user: dict = Depends(get_current_user)):
    require_role(user, "worker")
    return await list_orders(request, limit=limit, cursor=None, area_id=None, equipment_id=None,
                             assignee_id=user["id"], crew_id=None, priority=None,
                             status="done,ai_review,closed,cancelled,rejected",
                             overdue=None, from_=None, to=None, shift=None, kind=None, user=user)


@router.get("/work-orders/{order_id}")
async def get_order(order_id: uuid.UUID, user: dict = Depends(get_current_user)):
    await assert_can_read_order(user, str(order_id))
    o = await _load_full(str(order_id))
    return {**o, "allowed_actions": allowed_actions(user, {**o, "assignee_id": o.get("assignee_id"), "crew_id": o.get("crew_id"), "area_id": str(o.get("area_id"))})}


@router.patch("/work-orders/{order_id}")
async def patch_order(order_id: uuid.UUID, body: PatchWO, user: dict = Depends(get_current_user)):
    require_role(user, "master")
    await assert_can_read_order(user, str(order_id))
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            cur = await conn.fetchrow("SELECT * FROM work_orders WHERE id=$1::uuid FOR UPDATE", str(order_id))
            if not cur:
                raise not_found("Наряд не найден")
            if str(cur["area_id"]) not in user["areaIds"]:
                raise not_found("Наряд не найден")
            if cur["status"] in TERMINAL_DONE:
                raise conflict("invalid_transition", "Нельзя редактировать завершённый наряд")
            if body.due_at and body.due_at <= datetime.now(timezone.utc):
                raise validation_error([{"path": "due_at", "message": "Срок должен быть в будущем"}])
            await conn.execute("UPDATE work_orders SET priority=COALESCE($2,priority), due_at=COALESCE($3,due_at), comment=COALESCE($4,comment), updated_at=now() WHERE id=$1::uuid",
                               str(order_id), body.priority, body.due_at, body.comment)
            await conn.execute("INSERT INTO work_order_events(work_order_id, actor_id, action, comment, payload) VALUES ($1::uuid,$2::uuid,'updated',$3,$4::jsonb)",
                               str(order_id), user["id"], body.comment, json.dumps(body.model_dump(mode="json", exclude_none=True), default=str))
    return {"id": str(order_id)}


@router.post("/work-orders/{order_id}/transitions")
async def transition(order_id: uuid.UUID, body: TransitionIn, request: Request, user: dict = Depends(get_current_user)):
    await assert_can_read_order(user, str(order_id))
    if body.action in ("reject", "pause", "cancel") and not body.reason_code:
        raise validation_error([{"path": "reason_code", "message": "Укажите причину"}])
    if body.action == "reassign" and int(bool(body.assignee_id)) + int(bool(body.crew_id)) != 1:
        raise validation_error([{"path": "assignee_id", "message": "Укажите ровно одного исполнителя или одну бригаду"}])
    if body.action == "change_priority" and not body.priority:
        raise validation_error([{"path": "priority", "message": "Укажите приоритет"}])
    if body.action == "close" and not body.decision:
        raise validation_error([{"path": "decision", "message": "Укажите решение мастера"}])
    if body.action == "complete" and (not body.form or not body.form.work_done_text or not body.form.fault_code_id):
        raise validation_error([{"path": "form", "message": "Опишите работу и выберите шифр"}])
    key = request.headers.get("idempotency-key") or f"auto-{uuid.uuid4()}"
    h = _request_hash("POST", f"/work-orders/{order_id}/transitions", body.model_dump(mode="json"))

    async def action(conn):
        cur = await conn.fetchrow("SELECT * FROM work_orders WHERE id=$1::uuid FOR UPDATE", str(order_id))
        if not cur:
            raise not_found("Наряд не найден")
        o = dict(cur)
        states = MASTER_TRANSITIONS.get(body.action, []) if user["role"] == "master" else WORKER_TRANSITIONS.get(body.action, [])
        if user["role"] not in ("master", "worker") or o["status"] not in states:
            if user["role"] not in ("master", "worker"):
                raise forbidden()
            raise AppError(409, "invalid_transition", f"Действие «{body.action}» недоступно для статуса «{o['status']}»")
        if user["role"] == "master" and str(o["area_id"]) not in user["areaIds"]:
            raise not_found("Наряд не найден")
        if user["role"] == "worker" and str(o.get("assignee_id") or "") != user["id"] and not (user["crewId"] and str(o.get("crew_id") or "") == user["crewId"]):
            raise not_found("Наряд не найден")
        frm, to = o["status"], o["status"]
        reason = None
        if body.action == "accept":
            to = "accepted"
            await conn.execute("UPDATE work_orders SET status='accepted', accepted_at=COALESCE(accepted_at,now()), queue_position=NULL, updated_at=now() WHERE id=$1::uuid", str(order_id))
        elif body.action == "queue":
            nxt = await conn.fetchval("SELECT COALESCE(MAX(queue_position),0)+1 FROM work_orders WHERE status IN ('queued','accepted')")
            to = "queued"
            await conn.execute("UPDATE work_orders SET status='queued', queue_position=$2, updated_at=now() WHERE id=$1::uuid", str(order_id), int(nxt))
        elif body.action in ("reject", "pause", "cancel"):
            kind = {"reject": "reject", "pause": "pause", "cancel": "cancel"}[body.action]
            r = await conn.fetchrow("SELECT code FROM reasons WHERE kind=$1 AND code=$2", kind, body.reason_code)
            if not r:
                raise validation_error([{"path": "reason_code", "message": "Такой причины нет в справочнике"}])
            reason = r["code"]
            to = {"reject": "rejected", "pause": "paused", "cancel": "cancelled"}[body.action]
            await conn.execute("UPDATE work_orders SET status=$2, updated_at=now() WHERE id=$1::uuid", str(order_id), to)
        elif body.action == "start":
            to = "in_progress"
            await conn.execute("UPDATE work_orders SET status='in_progress', started_at=COALESCE(started_at,now()), queue_position=NULL, updated_at=now() WHERE id=$1::uuid", str(order_id))
        elif body.action == "resume":
            to = "in_progress"
            await conn.execute("UPDATE work_orders SET status='in_progress', paused_at=NULL, updated_at=now() WHERE id=$1::uuid", str(order_id))
        elif body.action == "complete":
            assert body.form and body.form.fault_code_id
            f = await conn.fetchrow("SELECT id FROM fault_codes WHERE id=$1::uuid AND deleted_at IS NULL", str(body.form.fault_code_id))
            if not f:
                raise validation_error([{"path": "form.fault_code_id", "message": "Шифр не найден"}])
            if body.photo_ids:
                n = await conn.fetchval("SELECT count(*) FROM photos WHERE id=ANY($1::uuid[]) AND author_id=$2::uuid AND kind='after'",
                                        [str(p) for p in body.photo_ids], user["id"])
                if int(n) != len(body.photo_ids):
                    raise validation_error([{"path": "photo_ids", "message": "Фото недоступно"}])
                await conn.execute("UPDATE photos SET work_order_id=$1::uuid WHERE id=ANY($2::uuid[]) AND work_order_id IS NULL",
                                   str(order_id), [str(p) for p in body.photo_ids])
            if o["kind"] == "unplanned":
                cnt = await conn.fetchval("SELECT count(*) FROM photos WHERE work_order_id=$1::uuid AND kind='after'", str(order_id))
                if int(cnt) == 0:
                    raise validation_error([{"path": "photo_ids", "message": "Для внепланового наряда добавьте фото «после»"}])
            for m in body.form.materials:
                u = await conn.fetchrow("SELECT unit FROM materials WHERE id=$1::uuid AND deleted_at IS NULL", str(m.material_id))
                if not u:
                    raise validation_error([{"path": "form.materials", "message": "Материал не найден"}])
                await conn.execute("INSERT INTO material_writeoffs(work_order_id, material_id, quantity, unit, created_by) VALUES ($1::uuid,$2::uuid,$3,$4,$5::uuid)",
                                   str(order_id), str(m.material_id), m.quantity, u["unit"], user["id"])
            to = "ai_review"
            await conn.execute("UPDATE work_orders SET status='ai_review', work_done_text=$2, fault_code_id=$3::uuid, done_at=now(), updated_at=now() WHERE id=$1::uuid",
                               str(order_id), body.form.work_done_text, str(body.form.fault_code_id))
            await conn.execute("INSERT INTO outbox_events(event, channels, payload) VALUES ('ai.review_requested','{}',$1::jsonb)",
                               json.dumps({"work_order_id": str(order_id)}))
        elif body.action in ("close", "return_to_rework"):
            if body.action == "return_to_rework":
                to = "rework"
                await conn.execute("UPDATE work_orders SET status='rework', return_count=return_count+1, updated_at=now() WHERE id=$1::uuid", str(order_id))
            else:
                rev = await conn.fetchrow("SELECT * FROM ai_reviews WHERE work_order_id=$1::uuid ORDER BY attempt DESC LIMIT 1", str(order_id))
                if not rev:
                    raise conflict("ai_review_missing", "Результат проверки ИИ ещё не готов")
                score = body.score if body.decision == "override" else (body.score if body.score is not None else rev["score"])
                if score is None:
                    raise validation_error([{"path": "score", "message": "Укажите итоговую оценку"}])
                await conn.execute("UPDATE ai_reviews SET master_decision=$2, master_score=$3, master_comment=$4, decided_by=$5::uuid, decided_at=now() WHERE id=$1",
                                   rev["id"], body.decision, int(score), body.comment, user["id"])
                if body.decision == "agree_ai" and rev["verdict"] == "needs_rework":
                    to = "rework"
                else:
                    to = "closed"
                    await conn.execute("UPDATE work_orders SET status='closed', closed_at=now(), updated_at=now() WHERE id=$1::uuid", str(order_id))
                    await conn.execute("INSERT INTO outbox_events(event, channels, payload) VALUES ('order.closed',$1,$2::jsonb)",
                                       ["shift:current", f"order:{order_id}"], json.dumps({"id": str(order_id)}))
        elif body.action == "reassign":
            to = "issued"
            await conn.execute("UPDATE work_orders SET status='issued', assignee_id=$2::uuid, crew_id=$3::uuid, accepted_at=NULL, started_at=NULL, updated_at=now() WHERE id=$1::uuid",
                               str(order_id), str(body.assignee_id) if body.assignee_id else None, str(body.crew_id) if body.crew_id else None)
        elif body.action == "change_priority":
            await conn.execute("UPDATE work_orders SET priority=$2, updated_at=now() WHERE id=$1::uuid", str(order_id), body.priority)
        await conn.execute(
            "INSERT INTO work_order_events(work_order_id, actor_id, action, reason_code, comment, payload) VALUES ($1::uuid,$2::uuid,$3,$4,$5,$6::jsonb)",
            str(order_id), user["id"], body.action, reason, body.comment, json.dumps({"from": frm, "to": to}))
        await conn.execute("INSERT INTO outbox_events(event, channels, payload) VALUES ('order.transition',$1,$2::jsonb)",
                           ["shift:current", f"order:{order_id}"],
                           json.dumps({"id": str(order_id), "from": frm, "to": to, "actor_id": user["id"]}))
        return {"statusCode": 200, "body": {"id": str(order_id), "from": frm, "to": to, "action": body.action}}

    res = await run_idempotent(key, user["id"], h, action)
    return {**res["body"], "replayed": res["replayed"]}


@router.get("/work-orders/{order_id}/events")
async def events(order_id: uuid.UUID, user: dict = Depends(get_current_user)):
    await assert_can_read_order(user, str(order_id))
    pool = await get_pool()
    rows = await pool.fetch("SELECT * FROM work_order_events WHERE work_order_id=$1::uuid ORDER BY at, id", str(order_id))
    return {"items": [_row_to_dict(r) for r in rows]}


@router.get("/work-orders/{order_id}/ai-review")
async def ai_review(order_id: uuid.UUID, user: dict = Depends(get_current_user)):
    await assert_can_read_order(user, str(order_id))
    pool = await get_pool()
    r = await pool.fetchrow("SELECT * FROM ai_reviews WHERE work_order_id=$1::uuid ORDER BY attempt DESC LIMIT 1", str(order_id))
    if not r:
        raise not_found("Проверка ИИ ещё не готова")
    return _row_to_dict(r)


@router.get("/work-orders/{order_id}/report")
async def report(order_id: uuid.UUID, audience: str = "worker", user: dict = Depends(get_current_user)):
    await assert_can_read_order(user, str(order_id))
    if audience == "master":
        require_role(user, "master", "manager", "admin")
    o = await _load_full(str(order_id))
    pool = await get_pool()
    rev = await pool.fetchrow("SELECT * FROM ai_reviews WHERE work_order_id=$1::uuid ORDER BY attempt DESC LIMIT 1", str(order_id))
    if audience == "worker":
        return {"work_order_id": str(order_id), "number": o.get("number"), "status": o.get("status"),
                "verdict": rev["verdict"] if rev else None, "score": rev["master_score"] if rev and rev["master_score"] is not None else (rev["score"] if rev else None)}
    ev = await pool.fetch("SELECT * FROM work_order_events WHERE work_order_id=$1::uuid ORDER BY at", str(order_id))
    return {"order": o, "ai_review": _row_to_dict(rev) if rev else None, "events": [_row_to_dict(e) for e in ev]}


@router.post("/work-orders/{order_id}/review/decision")
async def review_decision(order_id: uuid.UUID, body: ReviewDecision, request: Request, user: dict = Depends(get_current_user)):
    require_role(user, "master")
    await assert_can_read_order(user, str(order_id))
    if body.decision == "override" and (body.score is None or not body.comment):
        raise validation_error([{"path": "comment", "message": "Для изменения оценки обязательны балл и комментарий"}])
    key = request.headers.get("idempotency-key") or f"auto-{uuid.uuid4()}"
    h = _request_hash("POST", f"/work-orders/{order_id}/review/decision", body.model_dump(mode="json"))

    async def action(conn):
        rev = await conn.fetchrow("SELECT * FROM ai_reviews WHERE work_order_id=$1::uuid ORDER BY attempt DESC LIMIT 1 FOR UPDATE", str(order_id))
        if not rev:
            raise conflict("ai_review_missing", "Результат проверки ИИ ещё не готов")
        score = body.score if body.decision == "override" else (body.score if body.score is not None else rev["score"])
        if score is None:
            raise validation_error([{"path": "score", "message": "Укажите итоговую оценку"}])
        await conn.execute("UPDATE ai_reviews SET master_decision=$2, master_score=$3, master_comment=$4, decided_by=$5::uuid, decided_at=now() WHERE id=$1",
                           rev["id"], body.decision, int(score), body.comment, user["id"])
        to = "rework" if (body.decision == "agree_ai" and rev["verdict"] == "needs_rework") else "closed"
        if to == "closed":
            await conn.execute("UPDATE work_orders SET status='closed', closed_at=now(), updated_at=now() WHERE id=$1::uuid", str(order_id))
        return {"statusCode": 200, "body": {"id": str(order_id), "status": to, "decision": body.decision, "score": int(score)}}

    res = await run_idempotent(key, user["id"], h, action)
    return {**res["body"], "replayed": res["replayed"]}


@router.post("/work-orders/{order_id}/materials", status_code=201)
async def add_materials(order_id: uuid.UUID, body: list[MaterialItem], request: Request, user: dict = Depends(get_current_user)):
    require_role(user, "worker")
    await assert_can_read_order(user, str(order_id))
    key = request.headers.get("idempotency-key") or f"auto-{uuid.uuid4()}"
    h = _request_hash("POST", f"/work-orders/{order_id}/materials", [m.model_dump(mode="json") for m in body])

    async def action(conn):
        o = await conn.fetchrow("SELECT status, assignee_id, crew_id FROM work_orders WHERE id=$1::uuid FOR UPDATE", str(order_id))
        if not o or str(o["status"]) not in ("in_progress", "paused"):
            raise conflict("invalid_transition", "Материалы можно добавить только к наряду в работе")
        for m in body:
            u = await conn.fetchrow("SELECT unit FROM materials WHERE id=$1::uuid AND deleted_at IS NULL", str(m.material_id))
            if not u:
                raise validation_error([{"path": "materials", "message": "Материал не найден"}])
            await conn.execute("INSERT INTO material_writeoffs(work_order_id, material_id, quantity, unit, created_by) VALUES ($1::uuid,$2::uuid,$3,$4,$5::uuid)",
                               str(order_id), str(m.material_id), m.quantity, u["unit"], user["id"])
        return {"statusCode": 201, "body": {"work_order_id": str(order_id), "added": len(body)}}

    res = await run_idempotent(key, user["id"], h, action)
    return {**res["body"], "replayed": res["replayed"]}


class ActionIn(BaseModel):
    reason_code: str | None = None
    comment: str | None = Field(default=None, max_length=4000)
    assignee_id: uuid.UUID | None = None
    crew_id: uuid.UUID | None = None
    priority: Literal["critical", "high", "normal", "planned"] | None = None
    decision: Literal["agree_ai", "override"] | None = None
    score: int | None = Field(default=None, ge=0, le=100)
    client_at: datetime | None = None
    photo_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10)
    form: TransitionForm | None = None


async def _act(order_id: uuid.UUID, action: str, body: ActionIn, request: Request, user: dict):
    return await transition(order_id, TransitionIn(action=action, **body.model_dump()), request, user)


@router.post("/work-orders/{order_id}/assign", summary="Assign/reassign (master)")
async def assign(order_id: uuid.UUID, body: ActionIn, request: Request, user: dict = Depends(get_current_user)):
    """Master assigns the order (maps to `reassign`). Master decides, never AI."""
    return await _act(order_id, "reassign", body, request, user)


@router.post("/work-orders/{order_id}/accept", summary="Accept order (worker)")
async def accept(order_id: uuid.UUID, body: ActionIn, request: Request, user: dict = Depends(get_current_user)):
    return await _act(order_id, "accept", body, request, user)


@router.post("/work-orders/{order_id}/reject", summary="Reject order with reason (worker)")
async def reject(order_id: uuid.UUID, body: ActionIn, request: Request, user: dict = Depends(get_current_user)):
    return await _act(order_id, "reject", body, request, user)


@router.post("/work-orders/{order_id}/start", summary="Start execution (worker)")
async def start(order_id: uuid.UUID, body: ActionIn, request: Request, user: dict = Depends(get_current_user)):
    return await _act(order_id, "start", body, request, user)


@router.post("/work-orders/{order_id}/pause", summary="Pause with reason (worker)")
async def pause(order_id: uuid.UUID, body: ActionIn, request: Request, user: dict = Depends(get_current_user)):
    return await _act(order_id, "pause", body, request, user)


@router.post("/work-orders/{order_id}/resume", summary="Resume (worker)")
async def resume(order_id: uuid.UUID, body: ActionIn, request: Request, user: dict = Depends(get_current_user)):
    return await _act(order_id, "resume", body, request, user)


@router.post("/work-orders/{order_id}/complete", summary="Complete -> AI check (worker)")
async def complete(order_id: uuid.UUID, body: ActionIn, request: Request, user: dict = Depends(get_current_user)):
    return await _act(order_id, "complete", body, request, user)


@router.post("/work-orders/{order_id}/close", summary="Approve & close (master)")
async def close(order_id: uuid.UUID, body: ActionIn, request: Request, user: dict = Depends(get_current_user)):
    """Master approves the AI/master review and closes. Final word is human's."""
    return await _act(order_id, "close", body, request, user)


@router.post("/work-orders/{order_id}/rework", summary="Send back for rework (master)")
async def rework(order_id: uuid.UUID, body: ActionIn, request: Request, user: dict = Depends(get_current_user)):
    return await _act(order_id, "return_to_rework", body, request, user)


@router.get("/shift/board")
async def board(user: dict = Depends(get_current_user)):
    require_role(user, "master", "manager", "admin")
    pool = await get_pool()
    scope = user["areaIds"] if user["role"] == "master" else None
    counters = await pool.fetchrow(
        """SELECT count(*) FILTER (WHERE status IN ('issued','accepted','queued','rejected','in_progress','paused','rework','ai_review'))::int AS issued,
                  count(*) FILTER (WHERE status IN ('done','closed'))::int AS done,
                  count(*) FILTER (WHERE due_at < now() AND status NOT IN ('done','ai_review','closed','rejected','cancelled'))::int AS overdue
           FROM work_orders WHERE ($1::uuid[] IS NULL OR area_id = ANY($1::uuid[]))""", scope)
    emps = await pool.fetch(
        """SELECT e.id, e.full_name AS name, e.current_status AS status FROM employees e
           WHERE e.role='worker' AND e.enabled=true AND e.deleted_at IS NULL
           AND ($1::uuid[] IS NULL OR EXISTS (SELECT 1 FROM employee_areas ea WHERE ea.employee_id=e.id AND ea.area_id=ANY($1::uuid[])))
           ORDER BY e.full_name LIMIT 200""", scope)
    return {"counters": dict(counters), "employees": [_row_to_dict(e) for e in emps]}


@router.get("/shift/summary")
async def summary(user: dict = Depends(get_current_user)):
    b = await board(user)
    return {"period": "current_shift", "counters": b["counters"], "employees": len(b["employees"])}


@router.get("/equipment/{equipment_id}/history")
async def history(equipment_id: uuid.UUID, user: dict = Depends(get_current_user)):
    if user["role"] == "worker":
        raise forbidden()
    pool = await get_pool()
    eq = await pool.fetchrow("SELECT id, area_id FROM equipment WHERE id=$1::uuid AND deleted_at IS NULL", str(equipment_id))
    if not eq:
        raise not_found("Оборудование не найдено")
    if user["role"] == "master" and str(eq["area_id"]) not in user["areaIds"]:
        raise not_found("Оборудование не найдено")
    rows = await pool.fetch("SELECT id, number, kind, priority, status, issued_at FROM work_orders WHERE equipment_id=$1::uuid ORDER BY issued_at DESC LIMIT 200", str(equipment_id))
    return {"items": [_row_to_dict(r) for r in rows]}

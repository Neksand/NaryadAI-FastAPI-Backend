import json
import uuid
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.db import get_pool
from app.deps import get_current_user, require_role, run_idempotent
from app.errors import not_found, validation_error
from app.security import sha256_hex

router = APIRouter()


def _scope(user: dict, alias: str = "w") -> tuple[str, list]:
    if user["role"] == "master":
        return (f"AND {alias}.area_id = ANY(${{}}::uuid[])", user["areaIds"])
    return ("", [])


@router.get("/analytics/dashboard")
async def dashboard(from_: str | None = None, to: str | None = None, user: dict = Depends(get_current_user)):
    require_role(user, "master", "manager", "admin")
    pool = await get_pool()
    extra, params = "", []
    if user["role"] == "master":
        extra = "AND w.area_id = ANY($1::uuid[])"
        params.append(user["areaIds"])
    row = await pool.fetchrow(
        f"""SELECT count(*)::int AS total,
            count(*) FILTER (WHERE status IN ('accepted','in_progress','paused','rework'))::int AS in_progress,
            count(*) FILTER (WHERE due_at < now() AND status NOT IN ('done','ai_review','closed','rejected','cancelled'))::int AS overdue,
            count(*) FILTER (WHERE kind='unplanned')::int AS unplanned
            FROM work_orders w WHERE ($1::timestamptz IS NULL OR w.issued_at >= $1::timestamptz)
            AND ($2::timestamptz IS NULL OR w.issued_at < $2::timestamptz) {extra}""",
        from_, to, *params)
    return {"kpis": dict(row)}


@router.get("/analytics/areas")
async def areas(user: dict = Depends(get_current_user)):
    require_role(user, "master", "manager", "admin")
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT a.id, a.name, count(w.id)::int AS orders FROM areas a LEFT JOIN work_orders w ON w.area_id=a.id
           WHERE a.deleted_at IS NULL GROUP BY a.id, a.name ORDER BY a.name""")
    return {"items": [{"id": str(r["id"]), "name": r["name"], "orders": r["orders"]} for r in rows]}


@router.get("/analytics/areas/{area_id}")
async def area_detail(area_id: uuid.UUID, user: dict = Depends(get_current_user)):
    require_role(user, "master", "manager", "admin")
    pool = await get_pool()
    row = await pool.fetchrow("SELECT count(*)::int AS total FROM work_orders WHERE area_id=$1::uuid", str(area_id))
    return {"area_id": str(area_id), "total": row["total"]}


@router.get("/analytics/equipment/ranking")
async def ranking(user: dict = Depends(get_current_user)):
    require_role(user, "master", "manager", "admin")
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT e.id, e.name, count(w.id)::int AS unplanned FROM equipment e
           LEFT JOIN work_orders w ON w.equipment_id=e.id AND w.kind='unplanned'
           WHERE e.deleted_at IS NULL GROUP BY e.id ORDER BY unplanned DESC LIMIT 20""")
    return {"items": [{"id": str(r["id"]), "name": r["name"], "unplanned": r["unplanned"]} for r in rows]}


@router.get("/analytics/equipment/{eid}")
async def equipment_detail(eid: uuid.UUID, user: dict = Depends(get_current_user)):
    require_role(user, "master", "manager", "admin")
    pool = await get_pool()
    row = await pool.fetchrow("SELECT count(*)::int AS total FROM work_orders WHERE equipment_id=$1::uuid", str(eid))
    if not row:
        raise not_found("Оборудование не найдено")
    return {"equipment_id": str(eid), "total": row["total"]}


@router.get("/analytics/patterns")
async def patterns(user: dict = Depends(get_current_user)):
    require_role(user, "master", "manager", "admin")
    return {"items": []}


@router.get("/analytics/downtime")
async def downtime(user: dict = Depends(get_current_user)):
    require_role(user, "master", "manager", "admin")
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT COALESCE(sum(EXTRACT(EPOCH FROM (COALESCE(equipment_restored_at, now()) - equipment_stopped_at))/3600),0)::float AS hours FROM work_orders WHERE equipment_stopped_at IS NOT NULL")
    return {"downtime_hours": row["hours"]}


@router.get("/analytics/materials")
async def materials(user: dict = Depends(get_current_user)):
    require_role(user, "master", "manager", "admin")
    pool = await get_pool()
    rows = await pool.fetch("SELECT m.name, sum(mw.quantity)::float AS qty FROM material_writeoffs mw JOIN materials m ON m.id=mw.material_id GROUP BY m.name ORDER BY qty DESC LIMIT 50")
    return {"items": [dict(r) for r in rows]}


@router.get("/analytics/insights")
async def insights(status: str | None = None, user: dict = Depends(get_current_user)):
    require_role(user, "master", "manager", "admin")
    pool = await get_pool()
    rows = await pool.fetch("SELECT * FROM insights WHERE ($1::text IS NULL OR status=$1) ORDER BY created_at DESC LIMIT 100", status)
    out = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        out.append(d)
    return {"items": out}


@router.patch("/analytics/insights/{iid}")
async def patch_insight(iid: uuid.UUID, body: dict, user: dict = Depends(get_current_user)):
    require_role(user, "master", "manager", "admin")
    if body.get("status") not in ("seen", "in_plan", "dismissed"):
        raise validation_error([{"path": "status", "message": "Недопустимый статус"}])
    pool = await get_pool()
    await pool.execute("UPDATE insights SET status=$2, updated_at=now() WHERE id=$1::uuid", str(iid), body["status"])
    return {"id": str(iid), "status": body["status"]}


class PlanOrder(BaseModel):
    assignee_id: uuid.UUID | None = None
    crew_id: uuid.UUID | None = None
    due_at: str


@router.post("/analytics/insights/{iid}/plan-order")
async def plan_order(iid: uuid.UUID, body: PlanOrder, request: Request, user: dict = Depends(get_current_user)):
    require_role(user, "master", "manager", "admin")
    if int(bool(body.assignee_id)) + int(bool(body.crew_id)) != 1:
        raise validation_error([{"path": "assignee_id", "message": "Укажите исполнителя или бригаду"}])
    key = request.headers.get("idempotency-key")
    h = sha256_hex(json.dumps({"iid": str(iid), **body.model_dump(mode="json")}, sort_keys=True, default=str))

    async def action2(conn):
        ins = await conn.fetchrow("SELECT * FROM insights WHERE id=$1::uuid", str(iid))
        if not ins:
            raise not_found("Инсайт не найден")
        import json as j
        scope = ins["scope"] if isinstance(ins["scope"], dict) else j.loads(ins["scope"])
        eq_id = scope.get("equipment_id")
        if not eq_id:
            raise validation_error([{"path": "equipment", "message": "Нет оборудования в инсайте"}])
        number = await conn.fetchval("UPDATE work_order_counters SET value=value+1 WHERE key='number' RETURNING value")
        eq = await conn.fetchrow("SELECT area_id FROM equipment WHERE id=$1::uuid", eq_id)
        row = await conn.fetchrow(
            """INSERT INTO work_orders(number, kind, description, area_id, equipment_id, assignee_id, crew_id, master_id, priority, due_at)
               VALUES ($1,'planned',$2,$3::uuid,$4::uuid,$5::uuid,$6::uuid,$7::uuid,'planned',$8) RETURNING id""",
            int(number), ins["recommendation"] or ins["headline"], str(eq["area_id"]), eq_id,
            str(body.assignee_id) if body.assignee_id else None, str(body.crew_id) if body.crew_id else None,
            user["id"], body.due_at)
        await conn.execute("UPDATE insights SET status='in_plan' WHERE id=$1::uuid", str(iid))
        return {"statusCode": 201, "body": {"id": str(row["id"])}}

    res = await run_idempotent(key, user["id"], h, action2)
    return {**res["body"], "replayed": res["replayed"]}


@router.get("/analytics/ratings")
async def ratings(group_by: str = "employee", user: dict = Depends(get_current_user)):
    return {"items": [], "group_by": group_by}


@router.get("/analytics/ratings/{eid}")
async def rating_one(eid: uuid.UUID, user: dict = Depends(get_current_user)):
    pool = await get_pool()
    row = await pool.fetchrow("SELECT id, full_name FROM employees WHERE id=$1::uuid", str(eid))
    if not row:
        raise not_found("Сотрудник не найден")
    return {"employee_id": str(eid), "rating": None, "low_data": True}


@router.post("/analytics/query")
async def nl_query(body: dict, user: dict = Depends(get_current_user)):
    q = str(body.get("question", ""))
    kind = "dashboard"
    if "материал" in q.lower():
        kind = "materials"
    elif "простой" in q.lower():
        kind = "downtime"
    return {"answer_kind": kind, "confidence": 0.58}

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


@router.get("/analytics/overview", summary="Manager overview (alias of dashboard)")
async def overview(from_: str | None = None, to: str | None = None, user: dict = Depends(get_current_user)):
    return await dashboard(from_, to, user)


@router.get("/analytics/work-orders", summary="Work-order statistics by status")
async def wo_stats(from_: str | None = None, to: str | None = None, user: dict = Depends(get_current_user)):
    require_role(user, "master", "manager", "admin")
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT status, count(*)::int AS n FROM work_orders w
           WHERE ($1::timestamptz IS NULL OR w.issued_at >= $1::timestamptz)
           AND ($2::timestamptz IS NULL OR w.issued_at < $2::timestamptz)
           GROUP BY status ORDER BY n DESC""", from_, to)
    return {"data": [dict(r) for r in rows]}


@router.get("/analytics/workers", summary="Worker performance (alias of ratings)")
async def workers_stats(user: dict = Depends(get_current_user)):
    return await ratings("employee", None, None, user)


@router.get("/analytics/equipment", summary="Equipment analytics (alias of ranking)")
async def equipment_stats(user: dict = Depends(get_current_user)):
    return await ranking(user)


@router.get("/analytics/ai-insights", summary="AI insights (alias)")
async def ai_insights(status: str | None = None, user: dict = Depends(get_current_user)):
    return await insights(status, user)


@router.get("/analytics/dashboard")
async def dashboard(from_: str | None = None, to: str | None = None, user: dict = Depends(get_current_user)):
    require_role(user, "master", "manager", "admin")
    pool = await get_pool()
    extra, params = "", []
    if user["role"] == "master":
        extra = "AND w.area_id = ANY($3::uuid[])"
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
    rows = await pool.fetch("SELECT * FROM insights WHERE ($1::text IS NULL OR status=$1::insight_status) ORDER BY created_at DESC LIMIT 100", status)
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
    key = request.headers.get("idempotency-key") or f"auto-{uuid.uuid4()}"
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


@router.post("/analytics/insights/generate")
async def generate(user: dict = Depends(get_current_user)):
    require_role(user, "admin", "manager", "master")
    pool = await get_pool()
    from app.services.insights import generate_insights
    async with pool.acquire() as conn:
        async with conn.transaction():
            created = await generate_insights(conn)
    return {"generated": len(created), "headlines": created}


@router.get("/analytics/ratings", summary="Worker/crew ratings with formula explanation")
async def ratings(group_by: str = "employee", from_: str | None = None, to: str | None = None,
                  user: dict = Depends(get_current_user)):
    """rating = 100*(quality*.35 + on_time*.25 + (1-rework)*.2 + volume*.15 + (1-reject)*.05).
    low_data = closed < 5. Weights mirror settings.rating_weights."""
    pool = await get_pool()
    if group_by not in ("employee", "crew"):
        raise validation_error([{"path": "group_by", "message": "employee или crew"}])
    key = "assignee_id" if group_by == "employee" else "crew_id"
    gid_expr = "w.assignee_id" if group_by == "employee" else "COALESCE(w.crew_id, m.crew_id)"
    join = "" if group_by == "employee" else "LEFT JOIN employees m ON m.id=w.assignee_id"
    where_extra = "w.assignee_id IS NOT NULL" if group_by == "employee" else "(w.crew_id IS NOT NULL OR m.crew_id IS NOT NULL)"
    rows = await pool.fetch(
        f"""SELECT {gid_expr} AS gid,
              count(*) FILTER (WHERE w.status='closed')::int AS closed,
              count(*)::int AS total,
              AVG(COALESCE(ar.master_score, ar.score))::float AS quality,
              AVG(CASE WHEN w.done_at IS NOT NULL AND w.done_at <= w.due_at THEN 1.0 ELSE 0.0 END)::float AS on_time,
              AVG(CASE WHEN w.return_count > 0 THEN 1.0 ELSE 0.0 END)::float AS rework,
              AVG(CASE WHEN w.status='rejected' THEN 1.0 ELSE 0.0 END)::float AS rejected
           FROM work_orders w {join} LEFT JOIN LATERAL
             (SELECT * FROM ai_reviews ar WHERE ar.work_order_id=w.id ORDER BY attempt DESC LIMIT 1) ar ON true
           WHERE {where_extra}
             AND ($1::timestamptz IS NULL OR w.issued_at >= $1::timestamptz)
             AND ($2::timestamptz IS NULL OR w.issued_at < $2::timestamptz)
           GROUP BY {gid_expr} ORDER BY closed DESC LIMIT 200""", from_, to)
    items = []
    for r in rows:
        q = (r["quality"] or 0) / 100
        v = min((r["closed"] or 0) / 40, 1.0)
        rating = 100 * (q * 0.35 + (r["on_time"] or 0) * 0.25 + (1 - min(r["rework"] or 0, 1)) * 0.2
                        + v * 0.15 + (1 - min(r["rejected"] or 0, 1)) * 0.05)
        items.append({"id": str(r["gid"]), "closed": r["closed"], "total": r["total"],
                      "quality": round(q, 3), "on_time": round(r["on_time"] or 0, 3),
                      "rework_rate": round(r["rework"] or 0, 3), "rating": round(rating, 1),
                      "low_data": (r["closed"] or 0) < 5,
                      "formula": "100*(quality*.35+on_time*.25+(1-rework)*.2+volume*.15+(1-rejected)*.05)"})
    items.sort(key=lambda x: x["rating"], reverse=True)
    return {"data": items, "group_by": group_by}


@router.get("/analytics/ratings/{eid}", summary="One employee rating with explanation")
async def rating_one(eid: uuid.UUID, user: dict = Depends(get_current_user)):
    pool = await get_pool()
    row = await pool.fetchrow("SELECT id, full_name FROM employees WHERE id=$1::uuid", str(eid))
    if not row:
        raise not_found("Сотрудник не найден")
    return {"data": {"employee_id": str(eid), "hint": "Use /analytics/ratings?group_by=employee"}}


@router.get("/analytics/quality", summary="AI pass/fail + rework rates")
async def quality(from_: str | None = None, to: str | None = None, user: dict = Depends(get_current_user)):
    require_role(user, "master", "manager", "admin")
    pool = await get_pool()
    row = await pool.fetchrow(
        """SELECT count(*)::int AS reviews,
              count(*) FILTER (WHERE verdict IN ('accepted','accepted_with_remarks'))::int AS passed,
              count(*) FILTER (WHERE verdict='needs_rework')::int AS failed,
              AVG(score)::float AS avg_score
           FROM ai_reviews ar JOIN work_orders w ON w.id=ar.work_order_id
           WHERE ($1::timestamptz IS NULL OR ar.created_at >= $1::timestamptz)
           AND ($2::timestamptz IS NULL OR ar.created_at < $2::timestamptz)""", from_, to)
    rw = await pool.fetchrow(
        "SELECT AVG(CASE WHEN return_count>0 THEN 1.0 ELSE 0.0 END)::float AS rework_rate, count(*)::int AS orders FROM work_orders")
    total = row["reviews"] or 0
    return {"data": {"reviews": total,
                     "ai_pass_rate": round((row["passed"] or 0) / total, 3) if total else None,
                     "ai_fail_rate": round((row["failed"] or 0) / total, 3) if total else None,
                     "avg_score": round(row["avg_score"] or 0, 1),
                     "rework_rate": round(rw["rework_rate"] or 0, 3), "orders": rw["orders"]}}


@router.get("/analytics/faults", summary="Fault-code frequency")
async def faults(from_: str | None = None, to: str | None = None, limit: int = 20, user: dict = Depends(get_current_user)):
    require_role(user, "master", "manager", "admin")
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT f.code, f.name, count(*)::int AS n FROM work_orders w JOIN fault_codes f ON f.id=w.fault_code_id
           WHERE ($1::timestamptz IS NULL OR w.issued_at >= $1::timestamptz)
           AND ($2::timestamptz IS NULL OR w.issued_at < $2::timestamptz)
           GROUP BY f.code, f.name ORDER BY n DESC LIMIT $3""", from_, to, limit)
    return {"data": [dict(r) for r in rows]}


@router.get("/analytics/trends", summary="Daily buckets: created/closed/overdue")
async def trends(days: int = 30, user: dict = Depends(get_current_user)):
    require_role(user, "master", "manager", "admin")
    days = max(1, min(days, 120))
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT d::date AS day,
              (SELECT count(*)::int FROM work_orders WHERE issued_at::date=d) AS created,
              (SELECT count(*)::int FROM work_orders WHERE closed_at::date=d) AS closed
           FROM generate_series(current_date - ($1::int - 1), current_date, '1 day') d ORDER BY day""", days)
    return {"data": [{"day": str(r["day"]), "created": r["created"], "closed": r["closed"]} for r in rows]}


@router.post("/analytics/query")
async def nl_query(body: dict, user: dict = Depends(get_current_user)):
    q = str(body.get("question", ""))
    kind = "dashboard"
    if "материал" in q.lower():
        kind = "materials"
    elif "простой" in q.lower():
        kind = "downtime"
    return {"answer_kind": kind, "confidence": 0.58}

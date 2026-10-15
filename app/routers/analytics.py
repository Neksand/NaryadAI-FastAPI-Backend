import json
import math
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.db import get_pool
from app.deps import get_current_user, require_role, run_idempotent
from app.errors import forbidden, not_found, validation_error
from app.security import sha256_hex

router = APIRouter()


class OperationalEconomicsRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None
    currency: str = Field(default="KZT", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    labor_rate_per_hour: float | None = Field(default=None, ge=0, le=1e15, allow_inf_nan=False)
    downtime_cost_per_hour: float | None = Field(default=None, ge=0, le=1e15, allow_inf_nan=False)
    material_unit_prices: dict[str, float] = Field(default_factory=dict)

    @field_validator("from_", "to")
    @classmethod
    def assume_utc_for_naive_datetimes(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    @field_validator("material_unit_prices")
    @classmethod
    def validate_material_prices(cls, values: dict[str, float]) -> dict[str, float]:
        normalized = {}
        for material_id, price in values.items():
            try:
                normalized[str(uuid.UUID(material_id))] = float(price)
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError(f"Invalid material UUID: {material_id}") from exc
            if not math.isfinite(price) or price < 0 or price > 1e15:
                raise ValueError(
                    "Material unit prices must be finite, non-negative, and at most 1e15"
                )
        return normalized

    @model_validator(mode="after")
    def validate_period(self) -> "OperationalEconomicsRequest":
        if self.from_ is not None and self.to is not None and self.from_ >= self.to:
            raise ValueError("'from' must be earlier than 'to'")
        return self


class FeasibilityScenarioRequest(BaseModel):
    currency: str = Field(default="KZT", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    investment: float = Field(gt=0, le=1e15, allow_inf_nan=False)
    monthly_benefit: float = Field(ge=0, le=1e15, allow_inf_nan=False)
    monthly_operating_cost: float = Field(ge=0, le=1e15, allow_inf_nan=False)
    horizon_months: int = Field(ge=1, le=360)
    annual_discount_rate_pct: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
    residual_value: float = Field(default=0, ge=0, le=1e15, allow_inf_nan=False)


@router.post(
    "/analytics/economics/operations", summary="Estimated maintenance costs from work-order history"
)
async def operational_economics(
    body: OperationalEconomicsRequest, user: dict[str, Any] = Depends(get_current_user)
) -> dict[str, Any]:
    require_role(user, "master", "manager", "admin")
    pool = await get_pool()
    params: list[Any] = [body.from_, body.to]
    scope = ""
    if user["role"] == "master":
        scope = "AND w.area_id = ANY($3::uuid[])"
        params.append(user["areaIds"])

    aggregate = await pool.fetchrow(
        f"""SELECT count(*)::int AS total_orders,
              count(*) FILTER (WHERE w.status IN ('done','ai_review','closed'))::int AS completed_orders,
              count(*) FILTER (WHERE w.kind='unplanned')::int AS unplanned_orders,
              COALESCE(sum(w.norm_minutes) FILTER (WHERE w.status IN ('done','ai_review','closed')),0)::float / 60 AS norm_hours,
              count(*) FILTER (WHERE w.status IN ('done','ai_review','closed') AND w.norm_minutes IS NULL)::int AS completed_without_norm,
              COALESCE(sum(GREATEST(0, EXTRACT(EPOCH FROM
                (COALESCE(w.equipment_restored_at, now()) - w.equipment_stopped_at)) / 3600))
                FILTER (WHERE w.equipment_stopped_at IS NOT NULL),0)::float AS downtime_hours
           FROM work_orders w
           WHERE ($1::timestamptz IS NULL OR w.issued_at >= $1::timestamptz)
             AND ($2::timestamptz IS NULL OR w.issued_at < $2::timestamptz) {scope}""",
        *params,
    )
    material_rows = await pool.fetch(
        f"""SELECT m.id, m.name, mw.unit, sum(mw.quantity)::float AS quantity
           FROM material_writeoffs mw
           JOIN work_orders w ON w.id=mw.work_order_id
           JOIN materials m ON m.id=mw.material_id
           WHERE ($1::timestamptz IS NULL OR w.issued_at >= $1::timestamptz)
             AND ($2::timestamptz IS NULL OR w.issued_at < $2::timestamptz) {scope}
           GROUP BY m.id, m.name, mw.unit ORDER BY m.name, mw.unit""",
        *params,
    )
    from app.services.economics import operational_estimate

    result = operational_estimate(
        dict(aggregate),
        [dict(r) for r in material_rows],
        labor_rate_per_hour=body.labor_rate_per_hour,
        downtime_cost_per_hour=body.downtime_cost_per_hour,
        material_unit_prices=body.material_unit_prices,
        currency=body.currency,
    )
    result["period"] = {
        "from": body.from_.isoformat() if body.from_ else None,
        "to": body.to.isoformat() if body.to else None,
    }
    return {"data": result}


@router.post("/analytics/economics/feasibility", summary="ROI, payback, NPV and IRR scenario")
async def feasibility_economics(
    body: FeasibilityScenarioRequest, user: dict[str, Any] = Depends(get_current_user)
) -> dict[str, Any]:
    require_role(user, "master", "manager", "admin")
    from app.services.economics import feasibility_scenario

    return {"data": feasibility_scenario(**body.model_dump())}


def _scope(user: dict[str, Any], alias: str = "w") -> tuple[str, list[Any]]:
    if user["role"] == "master":
        return (f"AND {alias}.area_id = ANY(${{}}::uuid[])", user["areaIds"])
    return ("", [])


def _validate_period(from_: str | None, to: str | None) -> None:
    values = []
    for name, value in (("from", from_), ("to", to)):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
            if parsed is not None and parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            values.append(parsed)
        except ValueError as exc:
            raise validation_error([{"path": name, "message": "Ожидается дата ISO 8601"}]) from exc
    if values[0] is not None and values[1] is not None and values[0] >= values[1]:
        raise validation_error(
            [{"path": "to", "message": "Конец периода должен быть позже начала"}]
        )


def _areas(user: dict[str, Any]) -> list[str] | None:
    return user.get("areaIds", []) if user["role"] == "master" else None


def _insight_scope(parameter: int) -> str:
    # Employee findings aggregate all of that employee's areas. A master must
    # own every one of those areas before seeing the aggregate.
    return f"""(${parameter}::uuid[] IS NULL OR
       EXISTS (SELECT 1 FROM equipment e WHERE e.id::text=scope->>'equipment_id' AND e.area_id=ANY(${parameter}::uuid[])) OR
       scope->>'area_id'=ANY(SELECT unnest(${parameter}::uuid[])::text) OR
       (EXISTS (SELECT 1 FROM employee_areas ea WHERE ea.employee_id::text=scope->>'employee_id')
        AND NOT EXISTS (SELECT 1 FROM employee_areas ea WHERE ea.employee_id::text=scope->>'employee_id'
                        AND NOT (ea.area_id=ANY(${parameter}::uuid[])))))"""


@router.get("/analytics/overview", summary="Manager overview (alias of dashboard)")
async def overview(
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    return await dashboard(from_, to, user)


@router.get("/analytics/work-orders", summary="Work-order statistics by status")
async def wo_stats(
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    require_role(user, "master", "manager", "admin")
    _validate_period(from_, to)
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT status, count(*)::int AS n FROM work_orders w
           WHERE ($1::text::timestamptz IS NULL OR w.issued_at >= $1::text::timestamptz)
           AND ($2::text::timestamptz IS NULL OR w.issued_at < $2::text::timestamptz)
           AND ($3::uuid[] IS NULL OR w.area_id=ANY($3::uuid[]))
           GROUP BY status ORDER BY n DESC""",
        from_,
        to,
        _areas(user),
    )
    return {"data": [dict(r) for r in rows]}


@router.get("/analytics/workers", summary="Worker performance (alias of ratings)")
async def workers_stats(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    return await ratings("employee", None, None, user)


@router.get("/analytics/equipment", summary="Equipment analytics (alias of ranking)")
async def equipment_stats(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    return await ranking(user)


@router.get("/analytics/ai-insights", summary="AI insights (alias)")
async def ai_insights(
    status: str | None = None, user: dict[str, Any] = Depends(get_current_user)
) -> dict[str, Any]:
    return await insights(status, user)


@router.get("/analytics/dashboard")
async def dashboard(
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    require_role(user, "master", "manager", "admin")
    _validate_period(from_, to)
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
            FROM work_orders w WHERE ($1::text::timestamptz IS NULL OR w.issued_at >= $1::text::timestamptz)
            AND ($2::text::timestamptz IS NULL OR w.issued_at < $2::text::timestamptz) {extra}""",
        from_,
        to,
        *params,
    )
    return {"kpis": dict(row)}


@router.get("/analytics/areas")
async def areas(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    require_role(user, "master", "manager", "admin")
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT a.id, a.name, count(w.id)::int AS orders FROM areas a LEFT JOIN work_orders w ON w.area_id=a.id
           WHERE a.deleted_at IS NULL AND ($1::uuid[] IS NULL OR a.id=ANY($1::uuid[])) GROUP BY a.id, a.name ORDER BY a.name""",
        _areas(user),
    )
    return {"items": [{"id": str(r["id"]), "name": r["name"], "orders": r["orders"]} for r in rows]}


@router.get("/analytics/areas/{area_id}")
async def area_detail(
    area_id: uuid.UUID,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    require_role(user, "master", "manager", "admin")
    if user["role"] == "master" and str(area_id) not in user.get("areaIds", []):
        raise forbidden("Нет доступа к участку")
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT count(*)::int AS total FROM work_orders WHERE area_id=$1::uuid", str(area_id)
    )
    return {"area_id": str(area_id), "total": row["total"]}


@router.get("/analytics/equipment/ranking")
async def ranking(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    require_role(user, "master", "manager", "admin")
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT e.id, e.name, count(w.id)::int AS unplanned FROM equipment e
           LEFT JOIN work_orders w ON w.equipment_id=e.id AND w.kind='unplanned'
           WHERE e.deleted_at IS NULL AND ($1::uuid[] IS NULL OR e.area_id=ANY($1::uuid[])) GROUP BY e.id ORDER BY unplanned DESC LIMIT 20""",
        _areas(user),
    )
    return {
        "items": [
            {"id": str(r["id"]), "name": r["name"], "unplanned": r["unplanned"]} for r in rows
        ]
    }


@router.get("/analytics/equipment/{eid}")
async def equipment_detail(
    eid: uuid.UUID,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    require_role(user, "master", "manager", "admin")
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT count(*)::int AS total FROM work_orders WHERE equipment_id=$1::uuid AND ($2::uuid[] IS NULL OR area_id=ANY($2::uuid[]))",
        str(eid),
        _areas(user),
    )
    if not row:
        raise not_found("Оборудование не найдено")
    return {"equipment_id": str(eid), "total": row["total"]}


@router.get("/analytics/patterns")
async def patterns(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    require_role(user, "master", "manager", "admin")
    return await insights(None, user)


@router.get("/analytics/downtime")
async def downtime(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    require_role(user, "master", "manager", "admin")
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT COALESCE(sum(EXTRACT(EPOCH FROM (COALESCE(equipment_restored_at, now()) - equipment_stopped_at))/3600),0)::float AS hours FROM work_orders WHERE equipment_stopped_at IS NOT NULL AND ($1::uuid[] IS NULL OR area_id=ANY($1::uuid[]))",
        _areas(user),
    )
    return {"downtime_hours": row["hours"]}


@router.get("/analytics/materials")
async def materials(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    require_role(user, "master", "manager", "admin")
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT m.name, sum(mw.quantity)::float AS qty FROM material_writeoffs mw JOIN materials m ON m.id=mw.material_id JOIN work_orders w ON w.id=mw.work_order_id WHERE ($1::uuid[] IS NULL OR w.area_id=ANY($1::uuid[])) GROUP BY m.name ORDER BY qty DESC LIMIT 50",
        _areas(user),
    )
    return {"items": [dict(r) for r in rows]}


@router.get("/analytics/insights")
async def insights(
    status: str | None = None,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    require_role(user, "master", "manager", "admin")
    if status is not None and status not in ("new", "seen", "in_plan", "dismissed"):
        raise validation_error([{"path": "status", "message": "Недопустимый статус"}])
    pool = await get_pool()
    rows = await pool.fetch(
        f"SELECT * FROM insights WHERE ($1::text IS NULL OR status=$1::insight_status) AND {_insight_scope(2)} ORDER BY created_at DESC LIMIT 100",
        status,
        _areas(user),
    )
    out = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        for key in ("scope", "evidence"):
            if isinstance(d.get(key), str):
                d[key] = json.loads(d[key])
        out.append(d)
    return {"items": out}


@router.patch("/analytics/insights/{iid}")
async def patch_insight(
    iid: uuid.UUID,
    body: dict[str, Any],
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    require_role(user, "master", "manager", "admin")
    if body.get("status") not in ("seen", "in_plan", "dismissed"):
        raise validation_error([{"path": "status", "message": "Недопустимый статус"}])
    pool = await get_pool()
    await pool.execute(
        f"UPDATE insights SET status=$2, updated_at=now() WHERE id=$1::uuid AND {_insight_scope(3)}",
        str(iid),
        body["status"],
        _areas(user),
    )
    return {"id": str(iid), "status": body["status"]}


class PlanOrder(BaseModel):
    assignee_id: uuid.UUID | None = None
    crew_id: uuid.UUID | None = None
    due_at: AwareDatetime


@router.post("/analytics/insights/{iid}/plan-order")
async def plan_order(
    iid: uuid.UUID,
    body: PlanOrder,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    require_role(user, "master", "manager", "admin")
    if int(bool(body.assignee_id)) + int(bool(body.crew_id)) != 1:
        raise validation_error(
            [{"path": "assignee_id", "message": "Укажите исполнителя или бригаду"}]
        )
    key = request.headers.get("idempotency-key") or f"auto-{uuid.uuid4()}"
    h = sha256_hex(
        json.dumps({"iid": str(iid), **body.model_dump(mode="json")}, sort_keys=True, default=str)
    )

    async def action2(conn: Any) -> dict[str, Any]:
        ins = await conn.fetchrow("SELECT * FROM insights WHERE id=$1::uuid", str(iid))
        if not ins:
            raise not_found("Инсайт не найден")
        import json as j

        scope = ins["scope"] if isinstance(ins["scope"], dict) else j.loads(ins["scope"])
        eq_id = scope.get("equipment_id")
        if not eq_id:
            raise validation_error([{"path": "equipment", "message": "Нет оборудования в инсайте"}])
        number = await conn.fetchval(
            "UPDATE work_order_counters SET value=value+1 WHERE key='number' RETURNING value"
        )
        eq = await conn.fetchrow(
            "SELECT area_id FROM equipment WHERE id=$1::uuid AND deleted_at IS NULL", eq_id
        )
        if not eq:
            raise not_found("Оборудование не найдено")
        if user["role"] == "master" and str(eq["area_id"]) not in user.get("areaIds", []):
            raise forbidden("Нет доступа к участку оборудования")
        if body.due_at <= datetime.now(UTC):
            raise validation_error([{"path": "due_at", "message": "Срок должен быть в будущем"}])
        from app.routers.work_orders import _refresh_status, _validate_assignment

        await _validate_assignment(conn, str(eq["area_id"]), body.assignee_id, body.crew_id)
        row = await conn.fetchrow(
            """INSERT INTO work_orders(number, kind, description, area_id, equipment_id, assignee_id, crew_id, master_id, priority, due_at)
               VALUES ($1,'planned',$2,$3::uuid,$4::uuid,$5::uuid,$6::uuid,$7::uuid,'planned',$8) RETURNING id""",
            int(number),
            ins["recommendation"] or ins["headline"],
            str(eq["area_id"]),
            eq_id,
            str(body.assignee_id) if body.assignee_id else None,
            str(body.crew_id) if body.crew_id else None,
            user["id"],
            body.due_at,
        )
        await conn.execute(
            "INSERT INTO work_order_events(work_order_id,actor_id,action,payload) VALUES ($1,$2::uuid,'issued',$3::jsonb)",
            row["id"],
            user["id"],
            json.dumps({"insight_id": str(iid)}),
        )
        channels = ["shift:current", f"order:{row['id']}", f"user:{user['id']}"]
        if body.assignee_id:
            channels.append(f"user:{body.assignee_id}")
            await _refresh_status(conn, str(body.assignee_id))
        if body.crew_id:
            members = await conn.fetch(
                "SELECT id FROM employees WHERE crew_id=$1::uuid AND enabled=true AND deleted_at IS NULL",
                str(body.crew_id),
            )
            channels.extend(f"user:{member['id']}" for member in members)
            for member in members:
                await _refresh_status(conn, str(member["id"]))
        await conn.execute(
            "INSERT INTO outbox_events(event,payload,channels) VALUES ('order.created',$1::jsonb,$2::text[])",
            json.dumps({"id": str(row["id"]), "number": int(number)}),
            channels,
        )
        await conn.execute("UPDATE insights SET status='in_plan' WHERE id=$1::uuid", str(iid))
        return {"statusCode": 201, "body": {"id": str(row["id"])}}

    res = await run_idempotent(key, user["id"], h, action2)
    return {**res["body"], "replayed": res["replayed"]}


@router.post("/analytics/insights/generate")
async def generate(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    require_role(user, "admin", "manager", "master")
    pool = await get_pool()
    from app.services.insights import generate_insights

    async with pool.acquire() as conn, conn.transaction():
        created = await generate_insights(conn)
    if user["role"] == "master":
        visible = await insights(None, user)
        allowed = {item["headline"] for item in visible["items"]}
        created = [headline for headline in created if headline in allowed]
    return {"generated": len(created), "headlines": created}


@router.get("/analytics/ratings", summary="Worker/crew ratings with formula explanation")
async def ratings(
    group_by: str = "employee",
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """rating = 100*(quality*.35 + on_time*.25 + (1-rework)*.2 + volume*.15 + (1-reject)*.05).
    low_data = closed < 5. Weights mirror settings.rating_weights."""
    _validate_period(from_, to)
    pool = await get_pool()
    if group_by not in ("employee", "crew"):
        raise validation_error([{"path": "group_by", "message": "employee или crew"}])
    if user.get("role") == "worker" and group_by != "employee":
        raise forbidden("Исполнителю доступен только собственный рейтинг")
    if group_by == "employee":
        gid_expr = "w.assignee_id"
        join = "LEFT JOIN employees r_emp ON r_emp.id=w.assignee_id"
        name_expr = "MAX(r_emp.full_name)"
    else:
        gid_expr = "COALESCE(w.crew_id, m.crew_id)"
        join = "LEFT JOIN employees m ON m.id=w.assignee_id LEFT JOIN crews r_crew ON r_crew.id=COALESCE(w.crew_id, m.crew_id)"
        name_expr = "MAX(r_crew.name)"
    where_extra = (
        "w.assignee_id IS NOT NULL"
        if group_by == "employee"
        else "(w.crew_id IS NOT NULL OR m.crew_id IS NOT NULL)"
    )
    scope_id = user.get("id") if user.get("role") == "worker" else user.get("ratingEmployeeId")
    rows = await pool.fetch(
        f"""SELECT {gid_expr} AS gid, {name_expr} AS name,
              count(*) FILTER (WHERE w.status='closed')::int AS closed,
              count(*)::int AS total,
              AVG(COALESCE(ar.master_score, ar.score)) FILTER (WHERE w.status='closed')::float AS quality,
              AVG(CASE WHEN w.done_at <= w.due_at THEN 1.0 ELSE 0.0 END) FILTER (WHERE w.done_at IS NOT NULL)::float AS on_time,
              AVG(CASE WHEN w.return_count > 0 THEN 1.0 ELSE 0.0 END)::float AS rework,
              AVG(CASE WHEN w.status='rejected' AND EXISTS (SELECT 1 FROM reasons rr WHERE rr.kind='reject' AND rr.code=w.reject_reason_code AND rr.disrespectful) THEN 1.0 ELSE 0.0 END)::float AS rejected
           FROM work_orders w {join} LEFT JOIN LATERAL
             (SELECT * FROM ai_reviews ar WHERE ar.work_order_id=w.id ORDER BY attempt DESC LIMIT 1) ar ON true
           WHERE {where_extra}
             AND ($1::text::timestamptz IS NULL OR w.issued_at >= $1::text::timestamptz)
             AND ($2::text::timestamptz IS NULL OR w.issued_at < $2::text::timestamptz)
             AND ($3::uuid IS NULL OR {gid_expr} = $3::uuid)
             AND ($4::uuid[] IS NULL OR w.area_id = ANY($4::uuid[]))
           GROUP BY {gid_expr} ORDER BY closed DESC LIMIT 200""",
        from_,
        to,
        scope_id,
        _areas(user),
    )
    items = []
    for r in rows:
        q = (r["quality"] or 0) / 100
        v = min((r["closed"] or 0) / 40, 1.0)
        on_time = r["on_time"] or 0
        rework = min(r["rework"] or 0, 1)
        rejected = min(r["rejected"] or 0, 1)
        rating = 100 * (
            q * 0.35 + on_time * 0.25 + (1 - rework) * 0.2 + v * 0.15 + (1 - rejected) * 0.05
        )
        items.append(
            {
                "id": str(r["gid"]),
                "name": r.get("name"),
                "closed": r["closed"],
                "total": r["total"],
                "quality": round(q, 3),
                "on_time": round(r["on_time"] or 0, 3),
                "rework_rate": round(rework, 3),
                "volume": round(v, 3),
                "rejected": round(rejected, 3),
                "rating": round(rating, 1),
                "low_data": (r["closed"] or 0) < 5,
                "formula": "100*(quality*.35+on_time*.25+(1-rework)*.2+volume*.15+(1-rejected)*.05)",
                "assumptions": [
                    "Качество — оценки закрытых нарядов; незавершённые не считаются опоздавшими",
                    "Уважительные отказы исключены из штрафа; доля считается от назначенных нарядов",
                    "Срок календарный: причины ожидания и сложность требуют оценки мастера; рейтинг не доказывает вину",
                    "Низкая выборка при менее 5 закрытых нарядах; объём нормирован на 40 закрытых",
                ],
            }
        )
    items.sort(key=lambda x: x["rating"], reverse=True)
    return {"data": items, "group_by": group_by}


@router.get("/analytics/ratings/me", summary="Current worker rating with calculation breakdown")
async def rating_me(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    if user.get("role") != "worker":
        raise forbidden("Этот адрес предназначен для профиля исполнителя")
    result = await ratings("employee", None, None, user)
    return {"data": result["data"][0] if result["data"] else None}


@router.get("/analytics/ratings/{eid}", summary="One employee rating with explanation")
async def rating_one(
    eid: uuid.UUID,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    if user.get("role") == "worker":
        if str(eid) != str(user.get("id")):
            raise forbidden("Исполнителю доступен только собственный рейтинг")
        result = await ratings("employee", None, None, user)
        return {"data": result["data"][0] if result["data"] else None}
    pool = await get_pool()
    row = await pool.fetchrow("SELECT id, full_name FROM employees WHERE id=$1::uuid", str(eid))
    if not row:
        raise not_found("Сотрудник не найден")
    result = await ratings("employee", None, None, {**user, "ratingEmployeeId": str(eid)})
    return {"data": result["data"][0] if result["data"] else None}


@router.get("/analytics/quality", summary="AI pass/fail + rework rates")
async def quality(
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    require_role(user, "master", "manager", "admin")
    _validate_period(from_, to)
    pool = await get_pool()
    row = await pool.fetchrow(
        """SELECT count(*)::int AS reviews,
              count(*) FILTER (WHERE verdict IN ('accepted','accepted_with_remarks'))::int AS passed,
              count(*) FILTER (WHERE verdict='needs_rework')::int AS failed,
              AVG(score)::float AS avg_score
           FROM ai_reviews ar JOIN work_orders w ON w.id=ar.work_order_id
           WHERE ($1::text::timestamptz IS NULL OR ar.created_at >= $1::text::timestamptz)
           AND ($2::text::timestamptz IS NULL OR ar.created_at < $2::text::timestamptz)
           AND ($3::uuid[] IS NULL OR w.area_id=ANY($3::uuid[]))""",
        from_,
        to,
        _areas(user),
    )
    rw = await pool.fetchrow(
        "SELECT AVG(CASE WHEN return_count>0 THEN 1.0 ELSE 0.0 END)::float AS rework_rate, count(*)::int AS orders FROM work_orders w WHERE ($1::text::timestamptz IS NULL OR w.issued_at >= $1::text::timestamptz) AND ($2::text::timestamptz IS NULL OR w.issued_at < $2::text::timestamptz) AND ($3::uuid[] IS NULL OR w.area_id=ANY($3::uuid[]))",
        from_,
        to,
        _areas(user),
    )
    total = row["reviews"] or 0
    return {
        "data": {
            "reviews": total,
            "ai_pass_rate": round((row["passed"] or 0) / total, 3) if total else None,
            "ai_fail_rate": round((row["failed"] or 0) / total, 3) if total else None,
            "avg_score": round(row["avg_score"] or 0, 1),
            "rework_rate": round(rw["rework_rate"] or 0, 3),
            "orders": rw["orders"],
        }
    }


@router.get("/analytics/faults", summary="Fault-code frequency")
async def faults(
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    require_role(user, "master", "manager", "admin")
    _validate_period(from_, to)
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT f.code, f.name, count(*)::int AS n FROM work_orders w JOIN fault_codes f ON f.id=w.fault_code_id
           WHERE ($1::text::timestamptz IS NULL OR w.issued_at >= $1::text::timestamptz)
           AND ($2::text::timestamptz IS NULL OR w.issued_at < $2::text::timestamptz)
           AND ($4::uuid[] IS NULL OR w.area_id=ANY($4::uuid[]))
           GROUP BY f.code, f.name ORDER BY n DESC LIMIT $3""",
        from_,
        to,
        limit,
        _areas(user),
    )
    return {"data": [dict(r) for r in rows]}


@router.get("/analytics/trends", summary="Daily buckets: created/closed/overdue")
async def trends(
    days: int = 30,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    require_role(user, "master", "manager", "admin")
    days = max(1, min(days, 120))
    pool = await get_pool()
    scope_sql = ""
    params = [days]
    if user["role"] == "master":
        scope_sql = "AND area_id = ANY($2::uuid[])"
        params.append(user["areaIds"])
    rows = await pool.fetch(
        f"""SELECT d::date AS day,
              (SELECT count(*)::int FROM work_orders WHERE issued_at::date=d {scope_sql}) AS created,
              (SELECT count(*)::int FROM work_orders WHERE closed_at::date=d {scope_sql}) AS closed
           FROM generate_series(current_date - ($1::int - 1), current_date, '1 day') d ORDER BY day""",
        *params,
    )
    return {
        "data": [
            {"day": str(r["day"]), "created": r["created"], "closed": r["closed"]} for r in rows
        ]
    }


@router.post("/analytics/query")
async def nl_query(
    body: dict[str, Any],
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    q = str(body.get("question", ""))
    kind = "dashboard"
    if "материал" in q.lower():
        kind = "materials"
    elif "простой" in q.lower():
        kind = "downtime"
    return {"answer_kind": kind, "confidence": 0.58}

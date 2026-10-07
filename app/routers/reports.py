import hashlib
import json
import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from typing import Literal

from app.db import get_pool
from app.deps import assert_can_read_order, get_current_user, require_role, run_idempotent
from app.errors import forbidden, not_found, validation_error
from app.security import sha256_hex
from app.storage import signed_url

router = APIRouter()


class ExportIn(BaseModel):
    type: Literal["orders", "shift", "ratings", "materials", "downtime", "dashboard"]
    format: Literal["pdf", "xlsx"]
    filters: dict = Field(default_factory=dict)
    work_order_id: uuid.UUID | None = None


@router.post("/reports/export", status_code=202)
async def create_export(body: ExportIn, request: Request, user: dict = Depends(get_current_user)):
    if user["role"] == "worker" and not (body.type == "orders" and body.work_order_id):
        raise forbidden("Исполнителю доступен только отчёт по наряду")
    if body.type == "orders" and body.work_order_id:
        await assert_can_read_order(user, str(body.work_order_id))
    elif user["role"] not in ("master", "manager", "admin"):
        raise forbidden()
    key = request.headers.get("idempotency-key") or f"auto-{uuid.uuid4()}"
    h = sha256_hex(json.dumps({"body": body.model_dump(mode="json")}, sort_keys=True, default=str))

    async def action(conn):
        scope = {"user_id": user["id"], "role": user["role"], "area_ids": user["areaIds"], "crew_id": user["crewId"]}
        row = await conn.fetchrow(
            """INSERT INTO report_exports(requested_by, report_type, format, filters, scope)
               VALUES ($1::uuid,$2,$3,$4::jsonb,$5::jsonb) RETURNING id, state""",
            user["id"], body.type, body.format, json.dumps({**body.filters, **({"work_order_id": str(body.work_order_id)} if body.work_order_id else {})}), json.dumps(scope))
        await conn.execute("INSERT INTO outbox_events(event, channels, payload) VALUES ('report.export_requested',$1,$2::jsonb)",
                           [f"user:{user['id']}"], json.dumps({"export_id": str(row["id"])}))
        return {"statusCode": 202, "body": {"job_id": str(row["id"]), "status": row["state"]}}

    res = await run_idempotent(key, user["id"], h, action)
    return {**res["body"], "replayed": res["replayed"]}


@router.get("/reports/export/{job_id}")
async def get_export(job_id: uuid.UUID, user: dict = Depends(get_current_user)):
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM report_exports WHERE id=$1::uuid", str(job_id))
    if not row or (str(row["requested_by"]) != user["id"] and user["role"] != "admin"):
        raise not_found("Экспорт не найден")
    out = {"job_id": str(row["id"]), "status": row["state"], "state": row["state"]}
    if row["state"] == "completed" and row["object_key"]:
        out["url"] = signed_url(row["object_key"], 900)
        out["expires_in"] = 900
    if row["state"] == "failed":
        out["error"] = row["error"]
    return out

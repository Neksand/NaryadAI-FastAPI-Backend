import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from typing import Literal

from app.db import get_pool
from app.deps import get_current_user, require_role
from app.errors import not_found, validation_error

router = APIRouter()


def _pub(row: dict) -> dict:
    return {"id": str(row["id"]), "login": row.get("login"), "full_name": row.get("full_name"),
            "role": row.get("role"), "specialty": row.get("specialty"), "grade": row.get("grade"),
            "crew_id": str(row["crew_id"]) if row.get("crew_id") else None,
            "shift": row.get("shift"), "current_status": row.get("current_status"),
            "language": row.get("lang", "ru"), "lang": row.get("lang", "ru")}


@router.get("/workers", summary="List workers (role-scoped)")
async def list_workers(limit: int = Query(100, ge=1, le=500), user: dict = Depends(get_current_user)):
    pool = await get_pool()
    if user["role"] == "worker":
        rows = await pool.fetch("SELECT * FROM employees WHERE id=$1::uuid", user["id"])
    elif user["role"] == "master":
        rows = await pool.fetch(
            """SELECT DISTINCT e.* FROM employees e JOIN employee_areas ea ON ea.employee_id=e.id
               WHERE e.role='worker' AND e.enabled=true AND e.deleted_at IS NULL
               AND ea.area_id = ANY($1::uuid[]) ORDER BY e.full_name LIMIT $2""", user["areaIds"], limit)
    else:
        rows = await pool.fetch(
            "SELECT * FROM employees WHERE role='worker' AND enabled=true AND deleted_at IS NULL ORDER BY full_name LIMIT $1", limit)
    return {"data": [_pub(dict(r)) for r in rows]}


@router.get("/workers/me", summary="Current worker profile")
async def workers_me(user: dict = Depends(get_current_user)):
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM employees WHERE id=$1::uuid", user["id"])
    if not row:
        raise not_found("Сотрудник не найден")
    return {"data": _pub(dict(row))}


@router.get("/workers/{wid}", summary="Worker by id")
async def worker_by_id(wid: uuid.UUID, user: dict = Depends(get_current_user)):
    require_role(user, "master", "manager", "admin")
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM employees WHERE id=$1::uuid AND deleted_at IS NULL", str(wid))
    if not row:
        raise not_found("Сотрудник не найден")
    return {"data": _pub(dict(row))}


@router.get("/users/me", summary="Current user")
async def users_me(user: dict = Depends(get_current_user)):
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM employees WHERE id=$1::uuid", user["id"])
    return {"data": _pub(dict(row))}


class PrefsIn(BaseModel):
    language: Literal["ru", "kk", "en"] = Field(description="UI language: ru (primary), kk (secondary)")

    @classmethod
    def validate_lang(cls, v: str) -> str:
        if v not in ("ru", "kk"):
            raise validation_error([{"path": "language", "message": "Поддерживаются ru и kk"}])
        return v


@router.patch("/users/me/preferences", summary="Update own language preference")
async def update_prefs(body: dict, user: dict = Depends(get_current_user)):
    lang = str(body.get("language", ""))
    if lang not in ("ru", "kk"):
        raise validation_error([{"path": "language", "message": "Поддерживаются ru и kk"}])
    pool = await get_pool()
    await pool.execute("UPDATE employees SET lang=$2, updated_at=now() WHERE id=$1::uuid", user["id"], lang)
    return {"data": {"language": lang}}

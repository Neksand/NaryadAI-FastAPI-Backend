"""Catalog reads with product-contract names (sites/teams/equipment/materials/fault-codes).

DB keeps canonical tables (areas/crews/...); these are thin read aliases so the
frontend agent never guesses. Mutations stay under /dict/:type (admin).
"""
import uuid

from fastapi import APIRouter, Depends, Query

from app.db import get_pool
from app.deps import get_current_user, require_role
from app.errors import not_found

router = APIRouter()


def _s(r) -> dict:
    d = dict(r)
    for k, v in list(d.items()):
        if isinstance(v, uuid.UUID):
            d[k] = str(v)
    return d


@router.get("/sites", summary="Sites (= areas)")
async def sites(user: dict = Depends(get_current_user)):
    pool = await get_pool()
    if user["role"] == "master":
        rows = await pool.fetch("SELECT id, name, name_kk, code FROM areas WHERE deleted_at IS NULL AND id = ANY($1::uuid[]) ORDER BY name", user["areaIds"])
    else:
        rows = await pool.fetch("SELECT id, name, name_kk, code FROM areas WHERE deleted_at IS NULL ORDER BY name")
    return {"data": [_s(r) for r in rows]}


@router.get("/sites/{sid}", summary="Site by id")
async def site_one(sid: uuid.UUID, user: dict = Depends(get_current_user)):
    pool = await get_pool()
    row = await pool.fetchrow("SELECT id, name, name_kk, code FROM areas WHERE id=$1::uuid AND deleted_at IS NULL", str(sid))
    if not row:
        raise not_found("Участок не найден")
    return {"data": _s(row)}


@router.get("/teams", summary="Teams (= crews, with member counts)")
async def teams(area_id: str | None = None, user: dict = Depends(get_current_user)):
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT c.id, c.name, c.area_id, a.name AS area_name,
                  (SELECT count(*)::int FROM employees e WHERE e.crew_id=c.id AND e.enabled=true AND e.deleted_at IS NULL) AS members
           FROM crews c JOIN areas a ON a.id=c.area_id
           WHERE c.deleted_at IS NULL AND ($1::uuid IS NULL OR c.area_id=$1::uuid)
           ORDER BY c.name""", area_id)
    return {"data": [_s(r) for r in rows]}


@router.get("/teams/{tid}", summary="Team by id")
async def team_one(tid: uuid.UUID, user: dict = Depends(get_current_user)):
    pool = await get_pool()
    row = await pool.fetchrow("SELECT id, name, area_id FROM crews WHERE id=$1::uuid AND deleted_at IS NULL", str(tid))
    if not row:
        raise not_found("Бригада не найдена")
    members = await pool.fetch("SELECT id, full_name, specialty, current_status FROM employees WHERE crew_id=$1::uuid AND enabled=true AND deleted_at IS NULL ORDER BY full_name", str(tid))
    d = _s(row)
    d["members"] = [_s(m) for m in members]
    return {"data": d}


@router.get("/equipment", summary="Equipment list")
async def equipment(area_id: str | None = None, q: str | None = None,
                    limit: int = Query(200, ge=1, le=500), user: dict = Depends(get_current_user)):
    pool = await get_pool()
    if user["role"] == "master" and not area_id:
        rows = await pool.fetch(
            """SELECT * FROM equipment WHERE deleted_at IS NULL AND area_id = ANY($1::uuid[])
               AND ($2::text IS NULL OR name ILIKE '%'||$2||'%') ORDER BY name LIMIT $3""",
            user["areaIds"], q, limit)
    else:
        rows = await pool.fetch(
            """SELECT * FROM equipment WHERE deleted_at IS NULL
               AND ($1::uuid IS NULL OR area_id=$1::uuid)
               AND ($2::text IS NULL OR name ILIKE '%'||$2||'%') ORDER BY name LIMIT $3""",
            area_id, q, limit)
    return {"data": [_s(r) for r in rows]}


@router.get("/equipment/{eid}", summary="Equipment by id")
async def equipment_one(eid: uuid.UUID, user: dict = Depends(get_current_user)):
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM equipment WHERE id=$1::uuid AND deleted_at IS NULL", str(eid))
    if not row:
        raise not_found("Оборудование не найдено")
    return {"data": _s(row)}


@router.get("/materials", summary="Materials reference")
async def materials(q: str | None = None, limit: int = Query(200, ge=1, le=500), user: dict = Depends(get_current_user)):
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT id, name, name_kk, unit, typical_usage_per_order FROM materials WHERE deleted_at IS NULL
           AND ($1::text IS NULL OR name ILIKE '%'||$1||'%') ORDER BY name LIMIT $2""", q, limit)
    return {"data": [_s(r) for r in rows]}


@router.get("/fault-codes", summary="Fault codes reference")
async def fault_codes(q: str | None = None, user: dict = Depends(get_current_user)):
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT id, code, fault_group, name, name_kk, default_norm_minutes FROM fault_codes
           WHERE deleted_at IS NULL AND ($1::text IS NULL OR (code ILIKE '%'||$1||'%' OR name ILIKE '%'||$1||'%'))
           ORDER BY code""", q)
    return {"data": [_s(r) for r in rows]}

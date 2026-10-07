import json
import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.db import get_pool
from app.deps import get_current_user, require_role
from app.errors import forbidden, not_found, validation_error
from app.security import hash_pin

router = APIRouter()

CATALOGUES = {
    "areas": {"table": "areas", "cols": ["name", "name_kk", "code"], "search": ["name", "code"]},
    "equipment": {"table": "equipment", "cols": ["name", "inventory_no", "area_id", "type", "criticality", "qr_token"], "search": ["name", "inventory_no"]},
    "crews": {"table": "crews", "cols": ["name", "area_id"], "search": ["name"]},
    "employees": {"table": "employees", "cols": ["login", "full_name", "role", "crew_id", "shift"], "search": ["login", "full_name"]},
    "fault-codes": {"table": "fault_codes", "cols": ["code", "fault_group", "name", "default_norm_minutes"], "search": ["code", "name"]},
    "materials": {"table": "materials", "cols": ["name", "unit", "typical_usage_per_order"], "search": ["name"]},
    "norms": {"table": "time_norms", "cols": ["fault_code_id", "equipment_type", "minutes"], "search": ["equipment_type"]},
    "reasons": {"table": "reasons", "cols": ["kind", "code", "name", "disrespectful"], "search": ["code", "name"]},
}


@router.get("/dict/{ctype}")
async def list_dict(ctype: str, q: str | None = None, area_id: str | None = None, kind: str | None = None,
                    limit: int = Query(500, ge=1, le=500), user: dict = Depends(get_current_user)):
    if ctype not in CATALOGUES:
        raise not_found("Справочник не найден")
    pool = await get_pool()
    meta = CATALOGUES[ctype]
    where, params = ["deleted_at IS NULL"], []
    if q:
        params.append(f"%{q}%")
        where.append(f"({' OR '.join([f'{c} ILIKE ${len(params)}' for c in meta['search']] )})")
    if area_id and "area_id" in meta["cols"]:
        params.append(area_id)
        where.append(f"area_id = ${len(params)}::uuid")
    if kind and "kind" in meta["cols"]:
        params.append(kind)
        where.append(f"kind = ${len(params)}")
    # area scoping mirrors TS: equipment filtered for master/worker
    if ctype == "equipment" and user["role"] in ("master", "worker") and not area_id:
        params.append(user["areaIds"])
        where.append(f"area_id = ANY(${len(params)}::uuid[])")
    params.append(limit)
    rows = await pool.fetch(f"SELECT * FROM {meta['table']} WHERE {' AND '.join(where)} LIMIT ${len(params)}", *params)
    out = []
    for r in rows:
        d = dict(r)
        for k, v in list(d.items()):
            if isinstance(v, uuid.UUID):
                d[k] = str(v)
        out.append(d)
    return {"items": out}


@router.post("/dict/{ctype}", status_code=201)
async def create_dict(ctype: str, body: dict, user: dict = Depends(get_current_user)):
    require_role(user, "admin")
    if ctype not in CATALOGUES:
        raise not_found("Справочник не найден")
    pool = await get_pool()
    if ctype == "employees":
        pin = str(body.get("pin", ""))
        import re
        if not re.fullmatch(r"\d{4,8}", pin):
            raise validation_error([{"path": "pin", "message": "ПИН 4-8 цифр"}])
        row = await pool.fetchrow(
            "INSERT INTO employees(login, pin_hash, full_name, role, crew_id, shift) VALUES ($1,$2,$3,$4,$5::uuid,$6) RETURNING id",
            body["login"], hash_pin(pin), body.get("full_name", body["login"]), body.get("role", "worker"),
            body.get("crew_id"), body.get("shift", "1"))
        for a in body.get("area_ids", []):
            await pool.execute("INSERT INTO employee_areas(employee_id, area_id) VALUES ($1::uuid,$2::uuid) ON CONFLICT DO NOTHING", str(row["id"]), a)
        return {"id": str(row["id"])}
    meta = CATALOGUES[ctype]
    cols = [c for c in meta["cols"] if c in body]
    if not cols:
        raise validation_error([{"path": "body", "message": "Нет полей"}])
    placeholders = ", ".join([f"${i+1}" for i in range(len(cols))])
    row = await pool.fetchrow(f"INSERT INTO {meta['table']}({','.join(cols)}) VALUES ({placeholders}) RETURNING id",
                              *[body[c] for c in cols])
    return {"id": str(row["id"])}


@router.patch("/dict/{ctype}/{item_id}")
async def patch_dict(ctype: str, item_id: uuid.UUID, body: dict, user: dict = Depends(get_current_user)):
    require_role(user, "admin")
    if ctype not in CATALOGUES or (ctype == "employees" and "pin" in body):
        raise not_found("Справочник не найден") if ctype not in CATALOGUES else validation_error([{"path": "pin", "message": "ПИН меняется через reset-pin"}])
    pool = await get_pool()
    sets, vals = [], []
    for k, v in body.items():
        if k in CATALOGUES[ctype]["cols"]:
            vals.append(v)
            sets.append(f"{k} = ${len(vals)}")
    if not sets:
        raise validation_error([{"path": "body", "message": "Нет полей"}])
    vals.append(str(item_id))
    await pool.execute(f"UPDATE {CATALOGUES[ctype]['table']} SET {', '.join(sets)}, updated_at=now() WHERE id=${len(vals)}::uuid" if ctype in ("areas", "equipment", "crews", "employees") else f"UPDATE {CATALOGUES[ctype]['table']} SET {', '.join(sets)} WHERE id=${len(vals)}::uuid", *vals)
    return {"id": str(item_id)}


@router.delete("/dict/{ctype}/{item_id}")
async def delete_dict(ctype: str, item_id: uuid.UUID, user: dict = Depends(get_current_user)):
    require_role(user, "admin")
    if ctype not in CATALOGUES:
        raise not_found("Справочник не найден")
    pool = await get_pool()
    await pool.execute(f"UPDATE {CATALOGUES[ctype]['table']} SET deleted_at=now() WHERE id=$1::uuid", str(item_id))
    return {"id": str(item_id)}


@router.get("/equipment/by-qr/{token}")
async def by_qr(token: str, user: dict = Depends(get_current_user)):
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM equipment WHERE qr_token=$1 AND deleted_at IS NULL", token)
    if not row:
        raise not_found("Оборудование не найдено")
    if user["role"] == "master" and str(row["area_id"]) not in user["areaIds"]:
        raise not_found("Оборудование не найдено")
    d = dict(row)
    for k, v in list(d.items()):
        if isinstance(v, uuid.UUID):
            d[k] = str(v)
    return d


DEFAULT_AI = {"deadline_before_minutes": 30, "deadline_repeat_minutes": 30, "not_accepted_minutes": 10}


@router.get("/settings/ai")
async def get_ai_settings(user: dict = Depends(get_current_user)):
    require_role(user, "admin", "manager")
    pool = await get_pool()
    row = await pool.fetchrow("SELECT value FROM settings WHERE key='ai'")
    val = dict(DEFAULT_AI)
    if row:
        import json as j
        v = row["value"]
        val.update(j.loads(v) if isinstance(v, str) else v)
    return val


@router.put("/settings/ai")
async def put_ai_settings(body: dict, user: dict = Depends(get_current_user)):
    require_role(user, "admin")
    pool = await get_pool()
    await pool.execute("INSERT INTO settings(key, value, updated_by) VALUES ('ai',$1::jsonb,$2::uuid) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                       json.dumps(body), user["id"])
    return body


@router.get("/audit")
async def audit(limit: int = Query(50, ge=1, le=200), before: str | None = None, user: dict = Depends(get_current_user)):
    require_role(user, "admin")
    pool = await get_pool()
    rows = await pool.fetch("SELECT * FROM audit_log WHERE ($1::timestamptz IS NULL OR created_at < $1::timestamptz) ORDER BY created_at DESC LIMIT $2", before, limit)
    return {"items": [dict(r) for r in rows]}


class PinIn(BaseModel):
    pin: str


@router.post("/users/{uid}/reset-pin")
async def reset_pin(uid: uuid.UUID, body: PinIn, user: dict = Depends(get_current_user)):
    require_role(user, "admin")
    import re
    if not re.fullmatch(r"\d{4,8}", body.pin):
        raise validation_error([{"path": "pin", "message": "ПИН 4-8 цифр"}])
    pool = await get_pool()
    await pool.execute("UPDATE employees SET pin_hash=$2, failed_pin_attempts=0, locked_at=NULL, auth_version=auth_version+1 WHERE id=$1::uuid",
                       str(uid), hash_pin(body.pin))
    await pool.execute("UPDATE refresh_sessions SET revoked_at=now() WHERE employee_id=$1::uuid", str(uid))
    return {"id": str(uid)}


@router.post("/users/{uid}/unblock")
async def unblock(uid: uuid.UUID, user: dict = Depends(get_current_user)):
    require_role(user, "admin")
    pool = await get_pool()
    await pool.execute("UPDATE employees SET failed_pin_attempts=0, locked_at=NULL, auth_version=auth_version+1 WHERE id=$1::uuid", str(uid))
    return {"id": str(uid)}


@router.get("/alerts")
async def alerts(limit: int = Query(50, ge=1, le=200), user: dict = Depends(get_current_user)):
    pool = await get_pool()
    if user["role"] == "worker":
        rows = await pool.fetch("SELECT id, number, priority, due_at, status FROM work_orders WHERE assignee_id=$1::uuid AND due_at < now() AND status NOT IN ('done','ai_review','closed','rejected','cancelled') ORDER BY due_at LIMIT $2", user["id"], limit)
    elif user["role"] == "master":
        rows = await pool.fetch("SELECT id, number, priority, due_at, status FROM work_orders WHERE area_id = ANY($1::uuid[]) AND due_at < now() AND status NOT IN ('done','ai_review','closed','rejected','cancelled') ORDER BY due_at LIMIT $2", user["areaIds"], limit)
    else:
        rows = await pool.fetch("SELECT id, number, priority, due_at, status FROM work_orders WHERE due_at < now() AND status NOT IN ('done','ai_review','closed','rejected','cancelled') ORDER BY due_at LIMIT $1", limit)
    return {"items": [dict(r) for r in rows]}

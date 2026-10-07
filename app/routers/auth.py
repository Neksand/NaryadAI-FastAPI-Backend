from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, Request

from app.db import get_pool
from app.deps import get_current_user
from app.errors import AppError, unauthorized
from app.security import (
    create_access_token, create_refresh_token, hash_pin, hash_refresh_token, verify_pin,
)
from app.config import settings

router = APIRouter()


class LoginIn(BaseModel):
    login: str
    pin: str = Field(pattern=r"^\d{4,8}$")


class RefreshIn(BaseModel):
    refresh_token: str


class LogoutIn(BaseModel):
    refresh_token: str | None = None


class DeviceIn(BaseModel):
    platform: str = Field(pattern="^(android|ios|web|desktop|tauri)$")
    fcm_token: str = Field(min_length=1, max_length=500)


def _public(user: dict) -> dict:
    return {"id": user["id"], "login": user.get("login"), "full_name": user.get("fullName"),
            "role": user["role"], "area_ids": user.get("areaIds", []),
            "crew_id": user.get("crewId"), "lang": user.get("lang", "ru")}


async def _load_user(employee_id: str) -> dict:
    pool = await get_pool()
    row = await pool.fetchrow(
        """SELECT e.id, e.login, e.role, e.crew_id, e.full_name, e.auth_version, e.lang,
                  COALESCE(array_agg(ea.area_id) FILTER (WHERE ea.area_id IS NOT NULL), '{}') AS area_ids
           FROM employees e LEFT JOIN employee_areas ea ON ea.employee_id = e.id
           WHERE e.id=$1 AND e.enabled=true AND e.deleted_at IS NULL GROUP BY e.id""",
        employee_id,
    )
    if not row:
        raise unauthorized()
    return {"id": str(row["id"]), "login": row["login"], "role": row["role"],
            "areaIds": [str(a) for a in (row["area_ids"] or [])],
            "crewId": str(row["crew_id"]) if row["crew_id"] else None,
            "fullName": row["full_name"], "authVersion": int(row["auth_version"]), "lang": row["lang"]}


async def _issue_session(user: dict) -> dict:
    pool = await get_pool()
    refresh = create_refresh_token()
    sess = await pool.fetchrow(
        "INSERT INTO refresh_sessions(employee_id, token_hash, expires_at) VALUES ($1,$2, now() + ($3::text || ' days')::interval) RETURNING id",
        user["id"], hash_refresh_token(refresh), str(settings.REFRESH_TOKEN_TTL_DAYS),
    )
    access = create_access_token(user, str(sess["id"]))
    pub = _public(user)
    pub["language"] = user.get("lang", "ru")
    return {"access_token": access, "token_type": "bearer", "refresh_token": refresh,
            "expires_in": settings.ACCESS_TOKEN_TTL_SECONDS, "user": pub}


@router.post("/auth/login")
async def login(body: LoginIn):
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT id, pin_hash, failed_pin_attempts, locked_at FROM employees WHERE login=$1 AND enabled=true AND deleted_at IS NULL",
        body.login,
    )
    if not row:
        verify_pin(hash_pin("0000"), body.pin)
        raise unauthorized("Неверный логин или ПИН")
    if row["locked_at"] is not None:
        raise AppError(423, "account_locked", "Учётная запись заблокирована", {"locked_until": None})
    if not verify_pin(row["pin_hash"], body.pin):
        upd = await pool.fetchrow(
            """UPDATE employees SET failed_pin_attempts = failed_pin_attempts + 1,
               locked_at = CASE WHEN failed_pin_attempts + 1 >= 5 THEN now() ELSE NULL END, updated_at = now()
               WHERE id=$1 RETURNING failed_pin_attempts, locked_at""", row["id"])
        if upd["locked_at"] is not None:
            raise AppError(423, "account_locked", "Учётная запись заблокирована", {"locked_until": None})
        raise AppError(401, "unauthorized", "Неверный логин или ПИН",
                       {"attempts_remaining": max(0, 5 - int(upd["failed_pin_attempts"]))})
    await pool.execute("UPDATE employees SET failed_pin_attempts=0, locked_at=NULL, last_login_at=now(), updated_at=now() WHERE id=$1", row["id"])
    return await _issue_session(await _load_user(str(row["id"])))


@router.post("/auth/refresh")
async def refresh(body: RefreshIn):
    pool = await get_pool()
    replacement = create_refresh_token()
    async with pool.acquire() as conn:
        async with conn.transaction():
            cur = await conn.fetchrow(
                "SELECT id, employee_id FROM refresh_sessions WHERE token_hash=$1 AND revoked_at IS NULL AND expires_at > now() FOR UPDATE",
                hash_refresh_token(body.refresh_token))
            if not cur:
                raise unauthorized("Refresh-сессия истекла или отозвана")
            emp = await conn.fetchrow(
                """SELECT e.id, e.role, e.crew_id, e.full_name, e.auth_version,
                          COALESCE(array_agg(ea.area_id) FILTER (WHERE ea.area_id IS NOT NULL), '{}') AS area_ids
                   FROM employees e LEFT JOIN employee_areas ea ON ea.employee_id=e.id
                   WHERE e.id=$1 AND e.enabled=true AND e.deleted_at IS NULL GROUP BY e.id""",
                cur["employee_id"])
            if not emp:
                raise unauthorized()
            await conn.execute("UPDATE refresh_sessions SET revoked_at=now(), last_used_at=now() WHERE id=$1", cur["id"])
            await conn.execute(
                "INSERT INTO refresh_sessions(employee_id, token_hash, expires_at) VALUES ($1,$2, now() + ($3::text || ' days')::interval)",
                cur["employee_id"], hash_refresh_token(replacement), str(settings.REFRESH_TOKEN_TTL_DAYS))
            user = {"id": str(emp["id"]), "role": emp["role"],
                    "areaIds": [str(a) for a in (emp["area_ids"] or [])],
                    "crewId": str(emp["crew_id"]) if emp["crew_id"] else None,
                    "fullName": emp["full_name"], "authVersion": int(emp["auth_version"])}
            # fetch session id of replacement
            sess = await conn.fetchrow("SELECT id FROM refresh_sessions WHERE token_hash=$1", hash_refresh_token(replacement))
    return {"access_token": create_access_token(user, str(sess["id"])),
            "refresh_token": replacement, "expires_in": settings.ACCESS_TOKEN_TTL_SECONDS}


@router.post("/auth/logout", status_code=204)
async def logout(body: LogoutIn, user: dict = Depends(get_current_user)):
    pool = await get_pool()
    if body.refresh_token:
        await pool.execute(
            "UPDATE refresh_sessions SET revoked_at=now() WHERE employee_id=$1 AND (id=$2 OR token_hash=$3) AND revoked_at IS NULL",
            user["id"], user["sessionId"], hash_refresh_token(body.refresh_token))
    else:
        await pool.execute(
            "UPDATE refresh_sessions SET revoked_at=now() WHERE employee_id=$1 AND id=$2 AND revoked_at IS NULL",
            user["id"], user["sessionId"])
    return None


@router.get("/me", summary="Current user profile")
async def me(user: dict = Depends(get_current_user)):
    pool = await get_pool()
    extra = await pool.fetchrow(
        "SELECT e.specialty, e.grade, e.shift, e.current_status, e.lang, c.name AS crew_name FROM employees e LEFT JOIN crews c ON c.id=e.crew_id WHERE e.id=$1",
        user["id"])
    out = {**_public({**user, "login": user.get("login"), "lang": "ru"}), **dict(extra)}
    out["language"] = out.get("lang", "ru")
    return out


@router.get("/auth/me", summary="Current user (contract alias)")
async def auth_me(user: dict = Depends(get_current_user)):
    return await me(user)


@router.post("/devices", status_code=204)
async def devices(body: DeviceIn, user: dict = Depends(get_current_user)):
    pool = await get_pool()
    await pool.execute(
        """INSERT INTO device_tokens(employee_id, platform, token) VALUES ($1,$2,$3)
           ON CONFLICT (token) DO UPDATE SET employee_id=EXCLUDED.employee_id, platform=EXCLUDED.platform, last_seen_at=now()""",
        user["id"], body.platform, body.fcm_token)
    return None

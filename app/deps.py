import base64
import json
import uuid

from fastapi import Header, Request

from app.db import get_pool
from app.errors import AppError, conflict, forbidden, not_found, unauthorized, validation_error
from app.security import decode_access_token, sha256_hex

ROLES = ("worker", "master", "manager", "admin")


async def get_current_user(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    token = None
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token and request.query_params.get("token"):
        token = request.query_params["token"]
    if not token:
        raise unauthorized()
    try:
        claims = decode_access_token(token)
    except Exception:
        raise unauthorized()
    sub, sid = claims.get("sub"), claims.get("sid")
    if not sub or not isinstance(sid, str):
        raise unauthorized()
    try:
        uuid.UUID(str(sub))
        uuid.UUID(str(sid))
    except ValueError:
        raise unauthorized()
    if claims.get("role") not in ROLES:
        raise unauthorized()
    pool = await get_pool()
    row = await pool.fetchrow(
        """SELECT e.id, e.role, e.crew_id, e.full_name, e.auth_version, e.enabled,
                  COALESCE(array_agg(ea.area_id) FILTER (WHERE ea.area_id IS NOT NULL), '{}') AS area_ids
           FROM employees e LEFT JOIN employee_areas ea ON ea.employee_id = e.id
           WHERE e.id = $1 AND e.enabled = true AND e.deleted_at IS NULL GROUP BY e.id""",
        str(sub),
    )
    if not row:
        raise unauthorized()
    sess = await pool.fetchrow(
        "SELECT id FROM refresh_sessions WHERE id = $1 AND employee_id = $2 AND revoked_at IS NULL AND expires_at > now()",
        str(sid), str(sub),
    )
    if not sess:
        raise unauthorized()
    if int(row["auth_version"]) != int(claims.get("authv", -1)):
        raise unauthorized()
    return {
        "id": str(row["id"]),
        "role": row["role"],
        "areaIds": [str(a) for a in (row["area_ids"] or [])],
        "crewId": str(row["crew_id"]) if row["crew_id"] else None,
        "fullName": row["full_name"],
        "authVersion": int(row["auth_version"]),
        "sessionId": str(sess["id"]),
    }


def require_role(user: dict, *roles: str) -> None:
    if user["role"] not in roles:
        raise forbidden()


async def assert_can_read_order(user: dict, order_id: str) -> dict:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM work_orders WHERE id = $1", order_id)
    if not row:
        raise not_found("Наряд не найден")
    o = dict(row)
    role = user["role"]
    if role in ("admin", "manager"):
        return o
    if role == "master":
        if str(o["area_id"]) not in user["areaIds"]:
            raise not_found("Наряд не найден")
        return o
    if role == "worker":
        if str(o.get("assignee_id") or "") == user["id"]:
            return o
        if user["crewId"] and str(o.get("crew_id") or "") == user["crewId"]:
            return o
        raise not_found("Наряд не найден")
    raise forbidden()


def allowed_actions(user: dict, order: dict) -> list[str]:
    status = order.get("status")
    if user["role"] == "worker" and (
        str(order.get("assignee_id") or "") == user["id"]
        or (user["crewId"] and str(order.get("crew_id") or "") == user["crewId"])
    ):
        return {
            "issued": ["accept", "queue", "reject"],
            "queued": ["accept", "reject"],
            "accepted": ["start", "reject"],
            "in_progress": ["pause", "complete"],
            "paused": ["resume"],
            "rework": ["start"],
        }.get(status, [])
    if user["role"] == "master" and str(order.get("area_id")) in user["areaIds"]:
        if status == "ai_review":
            return ["close", "return_to_rework"]
        if status == "rework":
            return ["close"]
        if status in ("issued", "queued", "rejected", "accepted", "paused"):
            return ["reassign", "cancel", "change_priority"]
    return []


def decode_cursor(cursor: str | None) -> dict | None:
    if not cursor:
        return None
    try:
        val = json.loads(base64.urlsafe_b64decode(cursor + "==").decode())
        uuid.UUID(str(val["id"]))
        return {"id": str(val["id"]), "issued_at": str(val["issued_at"])}
    except Exception:
        raise validation_error([{"path": "cursor", "message": "Некорректный курсор"}])


def encode_cursor(id_: str, issued_at: str) -> str:
    return base64.urlsafe_b64encode(json.dumps({"id": id_, "issued_at": issued_at}).encode()).decode().rstrip("=")


async def run_idempotent(key: str | None, user_id: str, request_hash: str, action):
    """Port of src/common/idempotency.ts. Requires UUID key; replays stored response."""
    from app.db import get_pool

    if not key:
        raise conflict("idempotency_key_required", "Требуется заголовок Idempotency-Key")
    try:
        uuid.UUID(key)
    except ValueError:
        raise validation_error([{"path": "Idempotency-Key", "message": "Укажите корректный Idempotency-Key в формате UUID"}])
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO idempotency_keys(employee_id, key, request_hash) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
                user_id, key, request_hash,
            )
            row = await conn.fetchrow(
                "SELECT * FROM idempotency_keys WHERE employee_id=$1 AND key=$2 FOR UPDATE",
                user_id, key,
            )
            assert row is not None
            if row["request_hash"] != request_hash:
                raise conflict("idempotency_key_reused", "Этот Idempotency-Key уже использован для другого запроса")
            if row["response_status"] is not None:
                import json as _json

                body = _json.loads(row["response_body"]) if isinstance(row["response_body"], str) else row["response_body"]
                return {"status_code": int(row["response_status"]), "body": body, "replayed": True}
            # run action inside same txn: give it the connection
            result = await action(conn)
            import json as _json

            await conn.execute(
                "UPDATE idempotency_keys SET response_status=$2, response_body=$3::jsonb WHERE id=$1",
                row["id"], int(result["statusCode"]), _json.dumps(result["body"]),
            )
            return {"status_code": int(result["statusCode"]), "body": result["body"], "replayed": False}


def idempotency_key_from_header(idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> str | None:
    return idempotency_key

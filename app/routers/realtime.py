import json
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.deps import assert_can_read_order
from app.security import decode_access_token
from app.db import get_pool

router = APIRouter()


async def _user_from_token(token: str) -> dict | None:
    try:
        claims = decode_access_token(token)
    except Exception:
        return None
    try:
        pool = await get_pool()
        row = await pool.fetchrow(
            """SELECT e.id, e.role, e.crew_id, e.full_name, e.auth_version,
                      COALESCE(array_agg(ea.area_id) FILTER (WHERE ea.area_id IS NOT NULL), '{}') AS area_ids
               FROM employees e LEFT JOIN employee_areas ea ON ea.employee_id=e.id
               WHERE e.id=$1 AND e.enabled=true AND e.deleted_at IS NULL GROUP BY e.id""",
            str(claims.get("sub")))
        if not row or int(row["auth_version"]) != int(claims.get("authv", -1)):
            return None
        sess = await pool.fetchrow("SELECT id FROM refresh_sessions WHERE id=$1 AND revoked_at IS NULL AND expires_at > now()", str(claims.get("sid")))
        if not sess:
            return None
        return {"id": str(row["id"]), "role": row["role"],
                "areaIds": [str(a) for a in (row["area_ids"] or [])],
                "crewId": str(row["crew_id"]) if row["crew_id"] else None}
    except Exception:
        return None


@router.websocket("/ws")
async def ws(ws: WebSocket):
    await ws.accept()
    token = ws.query_params.get("token", "")
    user = await _user_from_token(token)
    if not user:
        await ws.close(code=4401)
        return
    subscribed: set[str] = set()
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("op") == "subscribe":
                channels = msg.get("channels", [])[:50]
                ok, rejected = [], []
                for ch in channels:
                    if ch in (f"user:{user['id']}", "user:me"):
                        ok.append(ch)
                    elif ch.startswith("order:"):
                        try:
                            await assert_can_read_order(user, ch[6:])
                            ok.append(ch)
                        except Exception:
                            rejected.append(ch)
                    elif ch in ("shift:current", "shift"):
                        if user["role"] in ("master", "manager", "admin"):
                            ok.append(ch)
                        else:
                            rejected.append(ch)
                    elif ch == "manager":
                        if user["role"] in ("manager", "admin"):
                            ok.append(ch)
                        else:
                            rejected.append(ch)
                    else:
                        rejected.append(ch)
                subscribed = set(ok)
                await ws.send_json({"event": "subscribed", "channels": ok, "rejected": rejected})
            elif msg.get("op") == "ping":
                await ws.send_json({"event": "pong"})
    except WebSocketDisconnect:
        return

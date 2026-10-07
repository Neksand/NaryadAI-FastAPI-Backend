import json
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.deps import assert_can_read_order
from app.security import decode_access_token
from app.db import get_pool

router = APIRouter()

# In-process live socket registry: user_id -> list of (websocket, channels).
# Single-process demo/brew deployment; multi-instance fan-out goes via Redis pub.
_connections: dict[str, list] = {}


def register(user_id: str, ws: WebSocket, channels: set[str]) -> None:
    unregister(ws)
    _connections.setdefault(user_id, []).append((ws, set(channels)))


def unregister(ws: WebSocket) -> None:
    for uid, lst in list(_connections.items()):
        remaining = [(s, ch) for (s, ch) in lst if s is not ws]
        if remaining:
            _connections[uid] = remaining
        else:
            _connections.pop(uid, None)


def channels_for(user_id: str, ws: WebSocket) -> set[str]:
    for s, ch in _connections.get(user_id, []):
        if s is ws:
            return ch
    return set()


async def broadcast(channels: list[str], type_: str, payload: dict) -> int:
    """Deliver a typed event to subscribed sockets. Returns socket count."""
    from app.deps import assert_can_read_order as _guard  # noqa: F401  (order guard applied at subscribe time)
    targets = set(channels)
    envelope = {"type": type_, "timestamp": datetime.now(timezone.utc).isoformat(), "payload": payload}
    sent = 0
    for uid, lst in list(_connections.items()):
        for ws, subscribed in list(lst):
            if not (subscribed & targets):
                continue
            try:
                if ws.client_state.name != "CONNECTED":
                    continue
                await ws.send_json(envelope)
                sent += 1
            except Exception:
                unregister(ws)
    return sent


async def _user_from_token(token: str) -> dict | None:
    try:
        claims = decode_access_token(token)
    except Exception:
        return None
    try:
        pool = await get_pool()
        row = await pool.fetchrow(
            """SELECT e.id, e.role, e.crew_id, e.full_name, e.auth_version, e.lang,
                      COALESCE(array_agg(ea.area_id) FILTER (WHERE ea.area_id IS NOT NULL), '{}') AS area_ids
               FROM employees e LEFT JOIN employee_areas ea ON ea.employee_id=e.id
               WHERE e.id=$1 AND e.enabled=true AND e.deleted_at IS NULL GROUP BY e.id""",
            str(claims.get("sub")))
        if not row or int(row["auth_version"]) != int(claims.get("authv", -1)):
            return None
        sess = await pool.fetchrow("SELECT id FROM refresh_sessions WHERE id=$1 AND revoked_at IS NULL AND expires_at > now()", str(claims.get("sid")))
        if not sess:
            return None
        return {"id": str(row["id"]), "role": row["role"], "lang": row["lang"],
                "areaIds": [str(a) for a in (row["area_ids"] or [])],
                "crewId": str(row["crew_id"]) if row["crew_id"] else None}
    except Exception:
        return None


async def _handle(ws: WebSocket):
    await ws.accept()
    token = ws.query_params.get("token", "")
    user = await _user_from_token(token)
    if not user:
        await ws.close(code=4401)
        return
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
                register(user["id"], ws, set(ok))
                await ws.send_json({"type": "SUBSCRIBED", "channels": ok, "rejected": rejected})
            elif msg.get("op") == "ping":
                await ws.send_json({"type": "PONG"})
    except WebSocketDisconnect:
        unregister(ws)
        return


@router.websocket("/ws")
async def ws(ws: WebSocket):
    await _handle(ws)


@router.websocket("/api/ws")
async def api_ws(ws: WebSocket):
    """Canonical path per product contract (same handler as /ws)."""
    await _handle(ws)

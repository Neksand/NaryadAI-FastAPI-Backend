from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.db import get_pool
from app.deps import assert_can_read_order
from app.security import decode_access_token

router = APIRouter()

# In-process live socket registry: user_id -> list of (websocket, channels).
# Single-process demo/brew deployment; multi-instance fan-out goes via Redis pub.
_connections: dict[str, list[tuple[WebSocket, set[str]]]] = {}
_connection_auth: dict[int, tuple[dict[str, Any], str | None]] = {}


def register(
    user_id: str,
    ws: WebSocket,
    channels: set[str],
    user: dict[str, Any] | None = None,
    token: str | None = None,
) -> None:
    unregister(ws)
    _connections.setdefault(user_id, []).append((ws, set(channels)))
    if user is not None:
        _connection_auth[id(ws)] = (user, token)


def unregister(ws: WebSocket) -> None:
    _connection_auth.pop(id(ws), None)
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


async def broadcast(channels: list[str], type_: str, payload: dict[str, Any]) -> int:
    """Deliver a typed event to subscribed sockets. Returns socket count."""
    targets = set(channels)
    envelope = {"type": type_, "timestamp": datetime.now(UTC).isoformat(), "payload": payload}
    sent = 0
    for _uid, lst in list(_connections.items()):
        for ws, subscribed in list(lst):
            if not (subscribed & targets):
                continue
            try:
                if ws.client_state.name != "CONNECTED":
                    continue
                auth = _connection_auth.get(id(ws))
                if auth:
                    user, token = auth
                    if token:
                        current_user = await _user_from_token(token)
                        if not current_user:
                            await ws.close(code=4401)
                            unregister(ws)
                            continue
                        user = current_user
                    if not await _can_receive(user, type_, payload):
                        continue
                await ws.send_json(envelope)
                sent += 1
            except Exception:
                unregister(ws)
    return sent


async def _can_receive(user: dict[str, Any], type_: str, payload: dict[str, Any]) -> bool:
    """A broad shift subscription is not permission to read every site's data."""
    if user["role"] in ("admin", "manager"):
        return True
    if type_ == "WORKER_STATUS_CHANGED":
        employee_id = payload.get("id") or payload.get("employee_id")
        if user["role"] == "worker":
            return str(employee_id) == user["id"]
        pool = await get_pool()
        return bool(
            await pool.fetchval(
                "SELECT EXISTS(SELECT 1 FROM employee_areas WHERE employee_id=$1::uuid AND area_id=ANY($2::uuid[]))",
                str(employee_id),
                user["areaIds"],
            )
        )
    order_id = payload.get("id") or payload.get("work_order_id")
    if payload.get("related_entity_type") == "work_order":
        order_id = payload.get("related_entity_id")
    if not order_id:
        return False
    try:
        await assert_can_read_order(user, str(order_id))
        return True
    except Exception:
        return False


async def _user_from_token(token: str) -> dict[str, Any] | None:
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
            str(claims.get("sub")),
        )
        if not row or int(row["auth_version"]) != int(claims.get("authv", -1)):
            return None
        sess = await pool.fetchrow(
            "SELECT id FROM refresh_sessions WHERE id=$1 AND employee_id=$2 AND revoked_at IS NULL AND expires_at > now()",
            str(claims.get("sid")),
            str(claims.get("sub")),
        )
        if not sess:
            return None
        return {
            "id": str(row["id"]),
            "role": row["role"],
            "lang": row["lang"],
            "areaIds": [str(a) for a in (row["area_ids"] or [])],
            "crewId": str(row["crew_id"]) if row["crew_id"] else None,
        }
    except Exception:
        return None


async def _handle(ws: WebSocket) -> None:
    await ws.accept()
    token = ws.query_params.get("token", "")
    user = await _user_from_token(token)
    if not user:
        await ws.close(code=4401)
        return
    try:
        while True:
            msg = await ws.receive_json()
            user = await _user_from_token(token)
            if not user:
                await ws.close(code=4401)
                unregister(ws)
                return
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
                register(
                    user["id"],
                    ws,
                    {
                        f"user:{user['id']}"
                        if channel == "user:me"
                        else "shift:current"
                        if channel == "shift"
                        else channel
                        for channel in ok
                    },
                    user=user,
                    token=token,
                )
                await ws.send_json({"type": "SUBSCRIBED", "channels": ok, "rejected": rejected})
            elif msg.get("op") == "ping":
                await ws.send_json({"type": "PONG"})
    except WebSocketDisconnect:
        unregister(ws)
        return


@router.websocket("/ws")
async def ws(ws: WebSocket) -> None:
    await _handle(ws)


@router.websocket("/api/ws")
async def api_ws(ws: WebSocket) -> None:
    """Canonical path per product contract (same handler as /ws)."""
    await _handle(ws)

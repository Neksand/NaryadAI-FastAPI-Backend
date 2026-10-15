"""WebSocket authentication, subscription scope, and broadcast cleanup tests."""

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketDisconnect

from app.routers import realtime


class Pool:
    def __init__(self, *, employee=None, session=None, fail=False):
        self.employee = employee
        self.session = session
        self.fail = fail
        self.fetchrow_calls = []

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        if self.fail:
            raise RuntimeError("database unavailable")
        return self.employee if "FROM employees" in query else self.session


class FakeWebSocket:
    def __init__(self, token="valid", messages=()):
        self.query_params = {"token": token}
        self.messages = list(messages)
        self.sent = []
        self.accepted = False
        self.closed = None
        self.client_state = SimpleNamespace(name="CONNECTED")

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000):
        self.closed = code
        self.client_state.name = "DISCONNECTED"

    async def receive_json(self):
        if not self.messages:
            raise WebSocketDisconnect(code=1000)
        return self.messages.pop(0)

    async def send_json(self, message):
        self.sent.append(message)


def valid_user(pool, monkeypatch, *, role="worker", user_id=None):
    user_id = user_id or str(uuid.uuid4())
    row = {
        "id": user_id,
        "role": role,
        "crew_id": None,
        "full_name": "Worker",
        "auth_version": 3,
        "lang": "kk",
        "area_ids": [uuid.uuid4()],
    }
    pool.employee, pool.session = row, {"id": "session-1"}
    monkeypatch.setattr(
        realtime,
        "decode_access_token",
        lambda token: {
            "sub": user_id,
            "sid": "session-1",
            "authv": 3,
        },
    )

    async def get_pool():
        return pool

    monkeypatch.setattr(realtime, "get_pool", get_pool)
    return user_id


def test_socket_rejects_invalid_expired_or_revoked_sessions(monkeypatch):
    def invalid(_):
        raise ValueError("invalid token")

    monkeypatch.setattr(realtime, "decode_access_token", invalid)
    socket = FakeWebSocket("broken")
    asyncio.run(realtime._handle(socket))
    assert socket.accepted and socket.closed == 4401

    stale = FakeWebSocket()
    stale_pool = Pool()
    valid_user(stale_pool, monkeypatch)
    stale_pool.session = None
    asyncio.run(realtime._handle(stale))
    assert stale.closed == 4401


def test_socket_subscriptions_enforce_user_order_shift_and_manager_scope(monkeypatch):
    pool = Pool()
    user_id = valid_user(pool, monkeypatch)

    async def order_access(user, order_id):
        if order_id == "allowed":
            return {"id": order_id}
        raise LookupError("not in scope")

    monkeypatch.setattr(realtime, "assert_can_read_order", order_access)
    socket = FakeWebSocket(
        messages=[
            {
                "op": "subscribe",
                "channels": [
                    "user:me",
                    f"user:{user_id}",
                    "user:other",
                    "shift:current",
                    "manager",
                    "order:allowed",
                    "order:denied",
                    "unknown",
                ],
            },
            {"op": "ping"},
        ]
    )
    asyncio.run(realtime.api_ws(socket))
    result = socket.sent[0]
    assert result["type"] == "SUBSCRIBED"
    assert set(result["channels"]) == {"user:me", f"user:{user_id}", "order:allowed"}
    assert set(result["rejected"]) == {
        "user:other",
        "shift:current",
        "manager",
        "order:denied",
        "unknown",
    }
    assert realtime.channels_for(user_id, socket) == set()  # disconnect unregisters the socket
    assert socket.sent[1] == {"type": "PONG"}
    assert user_id not in realtime._connections


@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        ("manager", {"shift:current", "shift", "manager", "user:me"}),
        ("admin", {"shift:current", "shift", "manager", "user:me"}),
        ("master", {"shift:current", "shift", "user:me"}),
    ],
)
def test_socket_role_policy_controls_shift_and_manager_channels(monkeypatch, role, allowed):
    valid_user(Pool(), monkeypatch, role=role)
    socket = FakeWebSocket(
        messages=[{"op": "subscribe", "channels": ["shift:current", "shift", "manager", "user:me"]}]
    )
    asyncio.run(realtime.ws(socket))
    result = socket.sent[0]
    assert set(result["channels"]) == allowed
    assert set(result["rejected"]) == {"shift:current", "shift", "manager", "user:me"} - allowed


class Socket:
    def __init__(self, *, state="CONNECTED", fail=False):
        self.client_state = SimpleNamespace(name=state)
        self.fail = fail
        self.messages = []

    async def send_json(self, message):
        if self.fail:
            raise RuntimeError("client disconnected")
        self.messages.append(message)


def test_broadcast_sends_only_intersecting_subscriptions_and_cleans_broken_sockets():
    realtime._connections.clear()
    healthy, broken, disconnected = Socket(), Socket(fail=True), Socket(state="DISCONNECTED")
    realtime.register("u-1", healthy, {"order:1", "shift"})
    realtime.register("u-2", broken, {"order:1"})
    realtime.register("u-3", disconnected, {"order:1"})
    assert asyncio.run(realtime.broadcast(["order:1"], "WORK_ORDER_UPDATED", {"id": "1"})) == 1
    assert healthy.messages[0]["type"] == "WORK_ORDER_UPDATED"
    assert healthy.messages[0]["payload"] == {"id": "1"}
    assert "u-2" not in realtime._connections
    assert "u-3" in realtime._connections
    assert asyncio.run(realtime.broadcast(["unrelated"], "NOOP", {})) == 0
    realtime._connections.clear()


def test_user_token_check_catches_database_failure_and_auth_version_mismatch(monkeypatch):
    user_id = str(uuid.uuid4())
    monkeypatch.setattr(
        realtime, "decode_access_token", lambda _: {"sub": user_id, "sid": "sid", "authv": 3}
    )

    async def get_pool():
        return Pool(fail=True)

    monkeypatch.setattr(realtime, "get_pool", get_pool)
    assert asyncio.run(realtime._user_from_token("token")) is None

    async def stale_pool():
        return Pool(employee={"id": user_id, "auth_version": 2}, session={"id": "sid"})

    monkeypatch.setattr(realtime, "get_pool", stale_pool)
    assert asyncio.run(realtime._user_from_token("token")) is None


def test_user_me_subscription_receives_canonical_user_events(monkeypatch):
    pool = Pool()
    uid = valid_user(pool, monkeypatch)

    async def allowed(*_args):
        return {"id": "order"}

    monkeypatch.setattr(realtime, "assert_can_read_order", allowed)

    class BroadcastingSocket(FakeWebSocket):
        async def receive_json(self):
            if not self.messages:
                await realtime.broadcast([f"user:{uid}"], "WORK_ORDER_ASSIGNED", {"id": "order"})
            return await super().receive_json()

    socket = BroadcastingSocket(messages=[{"op": "subscribe", "channels": ["user:me"]}])
    asyncio.run(realtime._handle(socket))
    assert any(item["type"] == "WORK_ORDER_ASSIGNED" for item in socket.sent)
    session_query, session_args = pool.fetchrow_calls[-1]
    assert "employee_id=$2" in session_query
    assert session_args[1] == uid


def test_master_shift_subscription_cannot_receive_other_area_order(monkeypatch):
    realtime._connections.clear()
    master_a, master_b = Socket(), Socket()
    user_a = {"id": "master-a", "role": "master", "areaIds": ["area-a"]}
    user_b = {"id": "master-b", "role": "master", "areaIds": ["area-b"]}

    async def order_access(user, order_id):
        assert order_id == "order-a"
        if "area-a" not in user["areaIds"]:
            raise LookupError("Outside assigned site")

    monkeypatch.setattr(realtime, "assert_can_read_order", order_access)
    realtime.register("master-a", master_a, {"shift:current"}, user=user_a)
    realtime.register("master-b", master_b, {"shift:current"}, user=user_b)
    try:
        count = asyncio.run(
            realtime.broadcast(["shift:current"], "WORK_ORDER_STARTED", {"id": "order-a"})
        )
        assert count == 1
        assert len(master_a.messages) == 1 and master_b.messages == []
    finally:
        realtime.unregister(master_a)
        realtime.unregister(master_b)


def test_master_shift_subscription_scopes_worker_status_to_assigned_areas(monkeypatch):
    class AreaPool:
        async def fetchval(self, query, employee_id, areas):
            assert "employee_areas" in query
            return employee_id == "worker-a" and "area-a" in areas

    async def pool():
        return AreaPool()

    monkeypatch.setattr(realtime, "get_pool", pool)
    a = {"id": "master-a", "role": "master", "areaIds": ["area-a"]}
    b = {"id": "master-b", "role": "master", "areaIds": ["area-b"]}
    assert asyncio.run(realtime._can_receive(a, "WORKER_STATUS_CHANGED", {"id": "worker-a"}))
    assert not asyncio.run(realtime._can_receive(b, "WORKER_STATUS_CHANGED", {"id": "worker-a"}))


def test_broadcast_disconnects_expired_or_revoked_sessions_before_delivery(monkeypatch):
    class CloseableSocket(Socket):
        async def close(self, code):
            self.closed = code

    socket = CloseableSocket()

    async def revoked(_token):
        return None

    monkeypatch.setattr(realtime, "_user_from_token", revoked)
    realtime.register(
        "master",
        socket,
        {"shift:current"},
        user={"id": "master", "role": "master", "areaIds": []},
        token="revoked-token",
    )
    assert (
        asyncio.run(realtime.broadcast(["shift:current"], "WORK_ORDER_UPDATED", {"id": "order"}))
        == 0
    )
    assert socket.messages == [] and socket.closed == 4401
    assert realtime.channels_for("master", socket) == set()

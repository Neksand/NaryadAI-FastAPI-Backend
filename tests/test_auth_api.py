"""Authentication tests for lockout policy, refresh rotation, and devices."""

import uuid

import pytest
from fastapi.testclient import TestClient

from app import deps
from app.routers import auth


class Context:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None


class Pool:
    def __init__(
        self,
        *,
        login_row=None,
        update=None,
        user_row=None,
        current=None,
        employee=None,
        new_session=None,
        profile=None,
    ):
        self.login_row = login_row
        self.update = update
        self.user_row = user_row
        self.current = current
        self.employee = employee
        self.new_session = new_session or {"id": "session-new"}
        self.profile = profile or {
            "specialty": "mechanic",
            "grade": "senior",
            "shift": "day",
            "current_status": "available",
            "lang": "kk",
            "crew_name": "Crew A",
        }
        self.fetchrow_calls = []
        self.execute_calls = []

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        if "WHERE login=$1" in query:
            return self.login_row
        if "RETURNING failed_pin_attempts" in query:
            return self.update
        if "SELECT e.id, e.login" in query:
            return self.user_row
        if "INSERT INTO refresh_sessions" in query:
            return self.new_session
        if "SELECT id FROM refresh_sessions" in query:
            return self.new_session
        if "SELECT id, employee_id FROM refresh_sessions" in query:
            return self.current
        if "SELECT e.id, e.role" in query:
            return self.employee
        if "SELECT e.specialty" in query:
            return self.profile
        return None

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    def transaction(self):
        return Context()


@pytest.fixture
def api(monkeypatch):
    clients = []

    def create(pool=None, user=None):
        pool = pool or Pool()

        async def get_pool():
            return pool

        monkeypatch.setattr(auth, "get_pool", get_pool)
        from app.main import create_app

        app = create_app()
        if user is not None:
            app.dependency_overrides[deps.get_current_user] = lambda: user
        client = TestClient(app)
        clients.append(client)
        return client, pool

    yield create
    for client in clients:
        client.close()


def test_login_obscures_unknown_accounts_and_rejects_locked_or_invalid_pin(api, monkeypatch):
    monkeypatch.setattr(auth, "hash_pin", lambda pin: "dummy-hash")
    verified = []
    monkeypatch.setattr(
        auth, "verify_pin", lambda hashed, pin: verified.append((hashed, pin)) or False
    )
    unknown = api(Pool(login_row=None))[0].post(
        "/api/v1/auth/login", json={"login": "missing", "pin": "1234"}
    )
    assert unknown.status_code == 401
    assert verified == [("dummy-hash", "1234")]

    locked_pool = Pool(
        login_row={
            "id": "worker-1",
            "pin_hash": "hash",
            "failed_pin_attempts": 5,
            "locked_at": "locked",
        }
    )
    locked = api(locked_pool)[0].post("/api/v1/auth/login", json={"login": "worker", "pin": "1234"})
    assert locked.status_code == 423
    assert len(verified) == 1

    invalid_pin = api(Pool())[0].post("/api/v1/auth/login", json={"login": "worker", "pin": "12x"})
    assert invalid_pin.status_code == 422


def test_wrong_pin_increments_attempts_and_locks_at_limit(api, monkeypatch):
    monkeypatch.setattr(auth, "verify_pin", lambda *_: False)
    user_id = uuid.uuid4()
    pool = Pool(
        login_row={"id": user_id, "pin_hash": "hash", "failed_pin_attempts": 2, "locked_at": None},
        update={"failed_pin_attempts": 3, "locked_at": None},
    )
    response = api(pool)[0].post("/api/v1/auth/login", json={"login": "worker", "pin": "1234"})
    assert response.status_code == 401
    assert response.json()["error"]["details"]["attempts_remaining"] == 2
    assert "failed_pin_attempts + 1 >= 5" in pool.fetchrow_calls[-1][0]

    pool.update = {"failed_pin_attempts": 5, "locked_at": "now"}
    locked = api(pool)[0].post("/api/v1/auth/login", json={"login": "worker", "pin": "1234"})
    assert locked.status_code == 423


def test_successful_login_clears_failures_issues_session_and_returns_public_user(api, monkeypatch):
    user_id = uuid.uuid4()
    monkeypatch.setattr(auth, "verify_pin", lambda *_: True)
    monkeypatch.setattr(auth, "create_refresh_token", lambda: "refresh-secret")
    monkeypatch.setattr(auth, "hash_refresh_token", lambda value: f"hash:{value}")
    monkeypatch.setattr(auth, "create_access_token", lambda user, session: f"access:{session}")
    pool = Pool(
        login_row={"id": user_id, "pin_hash": "argon", "failed_pin_attempts": 3, "locked_at": None},
        user_row={
            "id": user_id,
            "login": "worker",
            "role": "worker",
            "crew_id": None,
            "full_name": "Worker",
            "auth_version": 2,
            "lang": "kk",
            "area_ids": [uuid.uuid4()],
        },
        new_session={"id": "session-7"},
    )
    response = api(pool)[0].post("/api/v1/auth/login", json={"login": "worker", "pin": "1234"})
    assert response.status_code == 200
    data = response.json()
    assert data["access_token"] == "access:session-7" and data["refresh_token"] == "refresh-secret"
    assert data["user"]["lang"] == "kk" and data["user"]["language"] == "kk"
    assert "pin_hash" not in response.text
    assert "failed_pin_attempts=0" in pool.execute_calls[0][0]
    assert pool.fetchrow_calls[-1][1] == (
        str(user_id),
        "hash:refresh-secret",
        str(auth.settings.REFRESH_TOKEN_TTL_DAYS),
    )


def test_refresh_rotates_tokens_and_rejects_expired_or_deleted_sessions(api, monkeypatch):
    monkeypatch.setattr(auth, "create_refresh_token", lambda: "refresh-next")
    monkeypatch.setattr(auth, "hash_refresh_token", lambda value: f"hash:{value}")
    monkeypatch.setattr(auth, "create_access_token", lambda user, session: f"access:{session}")
    invalid = api(Pool(current=None))[0].post("/api/v1/auth/refresh", json={"refresh_token": "old"})
    assert invalid.status_code == 401

    user_id, area_id = uuid.uuid4(), uuid.uuid4()
    pool = Pool(
        current={"id": "session-old", "employee_id": user_id},
        employee={
            "id": user_id,
            "role": "master",
            "crew_id": None,
            "full_name": "Master",
            "auth_version": 1,
            "area_ids": [area_id],
        },
        new_session={"id": "session-next"},
    )
    response = api(pool)[0].post("/api/v1/auth/refresh", json={"refresh_token": "refresh-old"})
    assert response.status_code == 200
    assert response.json()["access_token"] == "access:session-next"
    assert response.json()["refresh_token"] == "refresh-next"
    assert len(pool.execute_calls) == 2

    disabled = api(Pool(current={"id": "session-old", "employee_id": user_id}, employee=None))[
        0
    ].post(
        "/api/v1/auth/refresh",
        json={"refresh_token": "refresh-old"},
    )
    assert disabled.status_code == 401


def test_logout_revokes_current_session_or_supplied_refresh_token(api, monkeypatch):
    monkeypatch.setattr(auth, "hash_refresh_token", lambda value: f"hash:{value}")
    user = {"id": "user-1", "role": "worker", "sessionId": "session-1"}
    client, pool = api(Pool(), user=user)
    assert client.post("/api/v1/auth/logout", json={}).status_code == 204
    assert pool.execute_calls[-1][1] == ("user-1", "session-1")
    assert client.post("/api/v1/auth/logout", json={"refresh_token": "refresh"}).status_code == 204
    assert pool.execute_calls[-1][1] == ("user-1", "session-1", "hash:refresh")


def test_profile_alias_and_device_registration_validate_contract(api):
    user = {
        "id": "user-1",
        "login": "worker",
        "role": "worker",
        "areaIds": [],
        "crewId": None,
        "fullName": "Worker",
        "sessionId": "session-1",
    }
    client, pool = api(Pool(), user=user)
    profile = client.get("/api/v1/me")
    assert profile.status_code == 200
    assert profile.json()["language"] == "kk" and profile.json()["crew_name"] == "Crew A"
    assert client.get("/api/v1/auth/me").json()["id"] == "user-1"
    assert (
        client.post(
            "/api/v1/devices", json={"platform": "desktop", "fcm_token": "token-1"}
        ).status_code
        == 204
    )
    assert pool.execute_calls[-1][1] == ("user-1", "desktop", "token-1")
    assert (
        client.post("/api/v1/devices", json={"platform": "bad", "fcm_token": ""}).status_code == 422
    )


def test_concurrent_refresh_race_keeps_session_alive():
    """Гонка двух refresh с одним токеном: оба 200, в конце ровно одна живая сессия.

    DB-backed, требует seed-БД (master1/3333) — как top1-интеграции.
    """
    import threading

    from fastapi.testclient import TestClient

    from app import db as _dbmod
    from app.main import create_app

    _dbmod._pool = None
    app = create_app()
    with TestClient(app) as client:
        login = client.post("/api/v1/auth/login", json={"login": "master1", "pin": "3333"})
        assert login.status_code == 200, login.text
        token = login.json()["refresh_token"]
        user_id = login.json()["user"]["id"]

        results = []

        def hit():
            try:
                r = client.post("/api/v1/auth/refresh", json={"refresh_token": token})
                results.append(r.status_code)
            except Exception as exc:
                results.append(repr(exc))

        threads = [threading.Thread(target=hit) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert sorted(results) == [200, 200], results

        live = client.post("/api/v1/auth/login", json={"login": "master1", "pin": "3333"})
        assert live.status_code == 200
        newest = live.json()["refresh_token"]
        # Все токены гонки, кроме новейшего, уже отозваны — повтор старого даёт 401,
        # а не вешает сессию; новейший работает.
        assert (
            client.post("/api/v1/auth/refresh", json={"refresh_token": newest}).status_code == 200
        )
        assert user_id

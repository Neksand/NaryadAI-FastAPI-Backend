"""Health and exception-handler contracts tested without starting worker loops."""

import pytest
from fastapi.testclient import TestClient

from app import main
from app.errors import AppError


class HealthyPool:
    async def fetchval(self, *_):
        return 1


class HealthyRedis:
    def __init__(self, *, ping_error=None):
        self.ping_error = ping_error
        self.closed = False

    async def ping(self):
        if self.ping_error:
            raise self.ping_error
        return True

    async def aclose(self):
        self.closed = True


def client():
    # Do not enter the context manager: these endpoint contracts do not need
    # database startup, object storage, or the long-running background tasks.
    return TestClient(main.create_app())


def test_root_advertises_discoverable_health_and_api_docs():
    response = client().get("/")

    assert response.status_code == 200
    assert response.json() == {
        "service": "naryadai-api",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "health": "/health/live",
        "ready": "/health/ready",
    }


@pytest.mark.parametrize(
    ("mode", "enabled", "text_provider", "api_key", "expected"),
    [
        ("mock", False, "off", "", "mock"),
        ("provider", True, "estimin3n", "", "estimin3n"),
        ("provider", False, "off", "gemini-key", "configured"),
        ("provider", False, "off", "", "local"),
    ],
)
def test_health_reports_database_and_effective_ai_mode(
    monkeypatch, mode, enabled, text_provider, api_key, expected
):
    monkeypatch.setattr(main, "get_pool", lambda: _pool())
    monkeypatch.setattr(main.settings, "AI_MODE", mode)
    monkeypatch.setattr(main.settings, "ESTIMIN3N_ENABLED", enabled)
    monkeypatch.setattr(main.settings, "AI_TEXT_PROVIDER", text_provider)
    monkeypatch.setattr(main.settings, "AI_VISION_PROVIDER", "off")
    monkeypatch.setattr(main.settings, "AI_AUDIO_PROVIDER", "off")
    monkeypatch.setattr(main.settings, "GEMINI_API_KEY", api_key)
    monkeypatch.setattr(main.settings, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(main.settings, "GROQ_API_KEY", "")

    response = client().get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "ai": expected}


async def _pool():
    return HealthyPool()


def test_health_degrades_when_database_probe_fails(monkeypatch):
    async def unavailable_pool():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(main, "get_pool", unavailable_pool)

    response = client().get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["database"] == "down"


def test_readiness_checks_database_and_closes_redis_client(monkeypatch):
    redis = HealthyRedis()
    monkeypatch.setattr(main, "get_pool", lambda: _pool())
    monkeypatch.setattr(main.aioredis, "from_url", lambda *_args, **_kwargs: redis)

    response = client().get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert redis.closed


@pytest.mark.parametrize("failure", ["database", "redis"])
def test_readiness_returns_503_when_either_dependency_fails(monkeypatch, failure):
    redis = HealthyRedis(
        ping_error=RuntimeError("redis unavailable") if failure == "redis" else None
    )
    if failure == "database":

        async def unavailable_pool():
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(main, "get_pool", unavailable_pool)
    else:
        monkeypatch.setattr(main, "get_pool", lambda: _pool())
    monkeypatch.setattr(main.aioredis, "from_url", lambda *_args, **_kwargs: redis)

    response = client().get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert redis.closed is (failure == "redis")


def test_exception_handlers_preserve_request_id_and_normalize_error_payloads():
    app = main.create_app()

    @app.get("/_test/app-error")
    async def raise_app_error():
        raise AppError(409, "invalid_transition", "Invalid state", {"state": "done"})

    @app.get("/_test/validation")
    async def validation_contract(count: int):
        return {"count": count}

    test_client = TestClient(app)
    app_error = test_client.get("/_test/app-error", headers={"X-Request-Id": "trace-123"})
    invalid = test_client.get(
        "/_test/validation?count=not-a-number", headers={"X-Request-Id": "trace-456"}
    )

    assert app_error.status_code == 409
    assert app_error.json() == {
        "error": {
            "code": "invalid_transition",
            "message": "Invalid state",
            "translation_key": "error.invalid_work_order_state",
            "details": {"state": "done"},
        },
        "request_id": "trace-123",
    }
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "validation_error"
    assert invalid.json()["request_id"] == "trace-456"
    assert invalid.json()["error"]["details"][0]["path"] == "query.count"

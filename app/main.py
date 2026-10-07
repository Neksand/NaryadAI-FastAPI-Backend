import asyncio
import uuid
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.config import settings
from app.db import close_pool, get_pool
from app.errors import AppError
from app.routers import admin, ai, auth, catalog, notifications, photos, realtime, reports, users, work_orders
from app.storage import ensure_upload_bucket


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.services.background import deadline_loop, insights_loop, outbox_loop

    ensure_upload_bucket()
    tasks = [asyncio.create_task(outbox_loop()), asyncio.create_task(deadline_loop()), asyncio.create_task(insights_loop())]
    yield
    for t in tasks:
        t.cancel()
    await close_pool()


def create_app() -> FastAPI:
    app = FastAPI(title="НарядAI API", version="1.0.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins or [],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "Accept-Language", "X-Request-Id"],
    )

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        key = exc.translation_key
        if exc.code == "invalid_transition":
            key = "error.invalid_work_order_state"
        body: dict = {"error": {"code": exc.code, "message": exc.message, "translation_key": key}}
        if exc.details is not None:
            body["error"]["details"] = exc.details
        body["request_id"] = request.headers.get("x-request-id", str(uuid.uuid4()))
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        body = {"error": {"code": "validation_error", "message": "Проверьте входные данные",
                          "translation_key": "error.validation_error",
                          "details": [{"path": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()]},
                "request_id": request.headers.get("x-request-id", str(uuid.uuid4()))}
        return JSONResponse(status_code=422, content=body)

    @app.get("/")
    async def root():
        return {"service": "naryadai-api", "docs": "/docs", "openapi": "/openapi.json",
                "health": "/health/live", "ready": "/health/ready"}

    @app.get("/health", summary="Health (contract)")
    async def health():
        from app.config import settings as _s
        db_ok = True
        try:
            pool = await get_pool()
            await pool.fetchval("SELECT 1")
        except Exception:
            db_ok = False
        ai = "mock" if (_s.AI_MODE or "mock") == "mock" else "configured"
        if not (_s.GEMINI_API_KEY or _s.OPENROUTER_API_KEY or _s.GROQ_API_KEY):
            ai = "mock"
        return {"status": "ok" if db_ok else "degraded", "database": "ok" if db_ok else "down", "ai": ai}

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready():
        try:
            pool = await get_pool()
            await pool.fetchval("SELECT 1")
            r = aioredis.from_url(settings.REDIS_URL, socket_connect_timeout=1.5)
            try:
                await r.ping()
            finally:
                await r.aclose()
            return {"status": "ready"}
        except Exception:
            return JSONResponse(status_code=503, content={"status": "not_ready"})

    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(users.router, prefix="/api/v1")
    app.include_router(catalog.router, prefix="/api/v1")
    app.include_router(notifications.router, prefix="/api/v1")
    app.include_router(work_orders.router, prefix="/api/v1")
    app.include_router(photos.router, prefix="/api/v1")
    app.include_router(admin.router, prefix="/api/v1")
    app.include_router(ai.router, prefix="/api/v1")
    app.include_router(reports.router, prefix="/api/v1")
    # analytics router is mounted under same prefix inside work_orders? mount separately:
    from app.routers import analytics as analytics_router
    app.include_router(analytics_router.router, prefix="/api/v1")
    app.include_router(realtime.router)
    return app


app = create_app()

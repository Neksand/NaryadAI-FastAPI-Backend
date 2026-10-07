# NaryadAI FastAPI Backend

Порт TypeScript-бэкенда (`../NaryadAI`) на Python FastAPI + Pydantic v2.
Исходный TS-проект не изменён; это изолированная папка.

## Стек

FastAPI, uvicorn, asyncpg (raw SQL, та же схема `migrations/001_initial.sql`),
redis asyncio (pub/sub outbox), boto3 (S3), PyJWT + argon2-cffi, openpyxl/fpdf2.

Фоновые задачи — asyncio в процессе API (вместо BullMQ):
outbox publisher каждые 500мс + deadline monitor каждые 30с.

## Запуск

```sh
cp .env.example .env
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m scripts.migrate
python -m scripts.seed   # только dev/test
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

Docker: `docker compose up --build` (миграции выполняются перед стартом API).
OpenAPI: `/openapi.json`, docs: `/docs`, health: `/health/live`, `/health/ready`.
Prod: `python -m scripts.migrate && python -m scripts.create_admin`.

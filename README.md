# НарядAI — бэкенд

Цифровые наряды для производства: мастер выдаёт работу с телефона, исполнитель ведёт её по статусам с фото и материалами, руководитель смотрит аналитику. ИИ следит за сроками, проверяет закрытые наряды и ищет проблемные участки.

## Стек

Python 3.12, FastAPI, Pydantic v2, PostgreSQL 17, Redis, S3-совместимое хранилище. ИИ: локальный движок `rules-v1` из коробки, опционально внешние провайдеры (ключи — в `.env`).

## Быстрый старт

```sh
cp .env.example .env
brew install postgresql@17 redis          # postgres + redis локально
brew services start postgresql@17 redis
createuser -s naryadai && createdb -O naryadai naryadai
pip install "moto[server]"                # эмулятор S3 для разработки
moto_server s3 -p 9000 &

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m scripts.migrate                 # SQL-миграции
python -m scripts.seed                    # демо-данные (только dev/test)
uvicorn app.main:app --reload
```

Проверки: `GET /` → ссылки, `/docs` → Swagger, `/health/live` → ok, `/health/ready` → ready (нужны БД и Redis).

Прод: `docker compose up --build` (миграции выполняются сами), первый админ — `python -m scripts.create_admin` (сид в проде заблокирован).

Демо-вход: `admin / 1234`, `master1 / 3333`, `manager1 / 2222`, `worker01…worker15 / 1001…1015`.

## ИИ (все ключи опциональны)

Без ключей работает локальный движок: эвристики полноты/материалов/сроков, dHash-сравнение фото, EXIF-свежесть, keyword-релевантность, rule-детектор аномалий. Ключи кладутся в `.env`:

| Переменная | Где взять бесплатно | Что включает |
|---|---|---|
| `AI_MODE` | `mock` (офлайн-демо) / `external` | Выбор движка ИИ |
| `GEMINI_API_KEY` | https://aistudio.google.com — бесплатная квота, текст+зрение | Сравнение фото «до/после» (оценка 1–5), проверка соответствия работ проблеме |
| `TELEGRAM_ENABLED` | `false` (адаптер выключен) / `true` + `TELEGRAM_BOT_TOKEN` (@BotFather) | Push-уведомления — опциональный канал; ядро работает без него |
| `OPENROUTER_API_KEY` | https://openrouter.ai — бесплатные `:free` модели | Текстовая проверка вместо локальной эвристики |
| `GROQ_API_KEY` | https://console.groq.com — бесплатный тир | Текст + транскрибация голосовых заметок (`POST /ai/transcribe`) |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_DEFAULT_CHAT_ID` | @BotFather | Push-уведомления: новые наряды, просрочки, эскалации, вердикты ИИ |

`AI_TEXT_PROVIDER` / `AI_VISION_PROVIDER`: `auto` (по порядку) или конкретный провайдер, `off` — только локальный движок. Наружу уходят только обезличенные тексты и фото — без имён, ПИНов и токенов.

## API

База `/api/v1`, ошибки — `{error:{code,message,translation_key}, request_id}` (`translation_key` вида `error.invalid_work_order_state` — для `locales/ru.json`, `locales/kk.json` фронтенда). Мутации принимают `Idempotency-Key: <UUID>` (без него сервер подставит разовый — без реплея при ретрае).

- Auth: `POST /auth/login|refresh|logout` (`token_type: bearer`, `user.language`), `GET /me`, `GET /auth/me`, `PATCH /users/me/preferences` (`{language: ru|kk}`), `POST /devices` (android/ios/web/desktop/tauri)
- Наряды: CRUD + `POST /work-orders/:id/{assign,accept,reject,start,pause,resume,complete,close,rework}` и общий `/transitions`, `GET /work-orders/my|my/active|my/history`, события, ai-review, отчёты, материалы, `GET /shift/board|summary`, история оборудования
- Каталоги: `/sites`, `/teams`, `/equipment`, `/materials`, `/fault-codes` (чтение); мутации — `/dict/:type` (admin)
- Уведомления: `GET /notifications`, `POST /notifications/:id/read|read-all` (in-app + WebSocket ядро, Telegram — опционально)
- Аналитика: `/analytics/{overview,work-orders,workers,equipment,downtime,ai-insights,...}`
- ИИ (`AI_MODE=mock` по умолчанию, без ключей и интернета): `POST /ai/work-orders/:id/{recommend-worker,inspect}`, `GET .../inspection`, `/ai/insights`, `suggest-assignee|fault-code`, `transcribe`
- Отчёты: `POST /reports/export` → `202 {job_id}` → `GET /reports/export/:job_id` (PDF/XLSX)
- Фото: JPEG/PNG/WebP до `MAX_UPLOAD_BYTES`, dHash-дубликаты, EXIF-свежесть, приватное хранение, подписанные ссылки
- Realtime: WebSocket `/api/ws?token=` (алиас `/ws`) — каналы `order:*`, `shift:current`, `user:*`; события `WORK_ORDER_*`, `AI_INSPECTION_*`, `DEADLINE_*`

## Структура

```
app/main.py            сборка приложения, health, lifespan-воркеры
app/routers/           auth, users (workers/preferences), catalog (sites/teams/...),
                       work_orders (+ per-action + my/*), admin, analytics (+алиасы),
                       ai (+контракт), reports, photos, notifications, realtime (/api/ws)
app/services/          ai_review (rules-v1), insights (детектор аномалий),
                       reports (XLSX/PDF), background (outbox+уведомления+WS, дедлайны, инсайты)
app/ai/                gateway + mock (ru/kk) + vision/inspection/recommendation/anomaly/reports
app/notifications.py   NotificationService (ru/kk шаблоны, персистентность, Telegram-адаптер)
app/ai_providers.py    внешние ИИ (все опциональны, fallback — локально)
migrations/            схема PostgreSQL (outbox, idempotency, notifications, ai_jobs, immutable-журналы)
scripts/               migrate, seed (4 участка / 25 единиц / 19 сотрудников / 20 шифров / 40 материалов / 500 нарядов + 5 паттернов), create_admin
tests/                 test_health, test_demo_flow (сквозной сценарий §11), test_contract (§41)
```

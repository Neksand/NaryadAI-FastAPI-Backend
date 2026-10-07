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
| `GEMINI_API_KEY` | https://aistudio.google.com — бесплатная квота, текст+зрение | Сравнение фото «до/после» (оценка 1–5), проверка соответствия работ проблеме |
| `OPENROUTER_API_KEY` | https://openrouter.ai — бесплатные `:free` модели | Текстовая проверка вместо локальной эвристики |
| `GROQ_API_KEY` | https://console.groq.com — бесплатный тир | Текст + транскрибация голосовых заметок (`POST /ai/transcribe`) |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_DEFAULT_CHAT_ID` | @BotFather | Push-уведомления: новые наряды, просрочки, эскалации, вердикты ИИ |

`AI_TEXT_PROVIDER` / `AI_VISION_PROVIDER`: `auto` (по порядку) или конкретный провайдер, `off` — только локальный движок. Наружу уходят только обезличенные тексты и фото — без имён, ПИНов и токенов.

## API

База `/api/v1`, ошибки — `{error:{code,message,details}, request_id}`. Мутации требуют заголовок `Idempotency-Key: <UUID>` (повтор с тем же ключом возвращает сохранённый ответ — так работает офлайн-очередь клиента).

- Auth: `POST /auth/login|refresh|logout`, `GET /me`, `POST /devices` (android/ios/web/desktop/tauri)
- Наряды: CRUD + `POST /work-orders/:id/transitions` (accept/queue/reject/start/pause/resume/complete/close/…), события, ai-review, отчёты, материалы, `GET /shift/board|summary`, история оборудования
- Справочники `/dict/:type`, оборудование по QR, настройки ИИ, аудит, сброс ПИН/разблокировка, алерты
- Аналитика: dashboard, участки, оборудование, patterns, downtime, материалы, инсайты (+ `POST /analytics/insights/generate`), рейтинги, NL-запрос
- ИИ: `POST /ai/suggest-assignee|suggest-fault-code|transcribe`
- Отчёты: `POST /reports/export` → `202 {job_id}` → `GET /reports/export/:job_id` (PDF/XLSX)
- Фото: JPEG/PNG/WebP до `MAX_UPLOAD_BYTES`, приватное хранение, подписанные ссылки
- Realtime: WebSocket `/ws?token=` — каналы `order:*`, `shift:current`, `user:*`

## Структура

```
app/main.py            сборка приложения, health, lifespan-воркеры
app/routers/           auth, work_orders, admin, analytics, ai, reports, photos, realtime
app/services/          ai_review (rules-v1), insights (детектор аномалий),
                       reports (XLSX/PDF), background (outbox 500мс, дедлайны 30с, инсайты 6ч)
app/ai_providers.py    внешние ИИ (все опциональны, fallback — локально)
app/notify.py          Telegram + FCM-хук
migrations/            схема PostgreSQL (outbox, idempotency, immutable-журналы)
scripts/               migrate, seed (4 участка / 25 единиц / 19 сотрудников / 20 шифров / 40 материалов / 500 нарядов), create_admin
tests/                 test_health, test_demo_flow (сквозной сценарий §11)
```

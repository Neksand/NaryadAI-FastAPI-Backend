# Запуск и подготовка деплоя

Состояние проверки смотрите в TEST_REPORT.md. Ни один production-сервер, домен или DNS не изменён. Docker отсутствовал на машине выполнения: контейнерную сборку и TLS нельзя считать проверенными. Реальная локальная проверка использует PostgreSQL 17, Redis, Moto S3 и API на хосте.

## Соседние репозитории

```
NaryadAI-FastAPI-Backend/
NaryadAI-frontend/
```

Требуются Python 3.12+, Node 24 LTS/npm, PostgreSQL 17, Redis. В проверке использован Node 24.19.0; Node 20 не подходит для части современных dev-зависимостей. На macOS инфраструктуру можно установить `brew install postgresql@17 redis`; служебные `brew services` для этого сценария не нужны.

## Локальный запуск без Docker

В backend:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m scripts.prepare_local_env
mkdir -p .runtime
```

`prepare_local_env` создаёт случайный token-secret, режим правил, локальные порты и синтетические credentials. При существующем `.env` команда отказывается его перезаписывать. Отредактируйте существующий файл осознанно или создайте новый checkout; не копируйте в него production credentials.

Для macOS задайте путь к PostgreSQL и создайте **новый** кластер:

```sh
export PG_BINDIR="$(brew --prefix postgresql@17)/bin"
"$PG_BINDIR/initdb" -D .runtime/postgres -U naryadai --auth=trust --locale=C -E UTF8
"$PG_BINDIR/pg_ctl" -D .runtime/postgres -l .runtime/postgres.log -o '-h 127.0.0.1 -p 25432 -k /tmp' start
"$PG_BINDIR/createdb" -h 127.0.0.1 -p 25432 -U naryadai naryadai_demo
redis-server --bind 127.0.0.1 --port 26379 --save '' --appendonly no
```

Последняя команда работает в отдельном терминале. В другом активированном Python-терминале:

```sh
moto_server -H 127.0.0.1 -p 29000
```

В третьем, из backend:

```sh
source .venv/bin/activate
python -m scripts.migrate
python -m scripts.seed
python -m scripts.verify_dataset
uvicorn app.main:app --host 127.0.0.1 --port 8080 --no-access-log
```

В frontend:

```sh
npm ci
npm run dev -- --host 127.0.0.1 --port 5173
```

Откройте `http://127.0.0.1:5173`. Vite проксирует `/api` на `127.0.0.1:8080`. Readiness: `http://127.0.0.1:8080/health/ready`; Swagger: `/docs`. Moto — непостоянный тестовый эмулятор: при его остановке фото исчезают. PostgreSQL сохраняется в выбранном `.runtime/postgres`; не удаляйте каталог, если нужны результаты.

Для повторной демонстрации seed повторно **не меняет** уже созданную seed-v2 базу. Для чистого повтора создайте новую БД, например `createdb ... naryadai_demo2`, измените только имя БД в локальном `.env`, затем выполните migrate + seed. Скрипт не очищает и не перезаписывает чужую историю. Фиксированная `SEED_REFERENCE_TIME=2026-10-14T08:00:00Z` воспроизводит даты истории; для живой демонстрации используйте текущую дату, поскольку аналитические окна отсчитываются от текущего времени БД.

## Проверки

```sh
pytest -q --cov=app --cov-branch
ruff check app tests
ruff format --check app tests
mypy app
python -m scripts.verify_dataset
python -m scripts.verify_analytics
python -m scripts.verify_insights
python -m scripts.verify_live
```

В frontend: `npm run typecheck`, `npm run lint`, `npm test`, `npm run test:coverage`, `npm run build`, `npm run check:contract`, `npm run test:e2e`. Live specs требуют работающий API; mock E2E сами по себе интеграцию с БД не доказывают.

## Production-профиль

Новый `compose.production.yaml` самостоятельный; не объединять его с dev-compose. Он использует одну API-реплику с её встроенными фоновыми циклами, закрытые порты PostgreSQL/Redis, непривилегированного пользователя API, healthcheck, ограничения памяти, Caddy TLS и **внешнее постоянное S3**. Фото не должны храниться в Moto.

1. Подготовьте отдельную БД/сервер, приватный S3 bucket с versioning/backup, DNS A/AAAA для выбранных доменов и открытые 80/443. Права S3 ключа ограничьте этим bucket; pre-create bucket предпочтительнее глобального права CreateBucket.
2. Скопируйте `.env.production.example` в `.env.production`, `chmod 600 .env.production`. Замените все `REPLACE`/`replace`, пароль БД, URL-encoded пароль в `COMPOSE_DATABASE_URL`, случайный JWT secret, случайный 8-значный PIN первого администратора, S3 credentials и ACME email. Домены в шаблоне — планируемые, их доступность не установлена.
3. Соберите frontend с относительным API URL: `npm ci` и `VITE_API_URL=/api/v1 VITE_WS_URL=/api/ws npm run build`. Убедитесь, что `dist` соседнего frontend содержит `index.html`.
4. На целевом хосте сначала выполните валидацию: `docker compose --env-file .env.production -f compose.production.yaml config --quiet`. Вывод полной config содержит секреты — не публикуйте его.
5. Только после отдельного разрешения владельца на изменение целевого окружения: `docker compose --env-file .env.production -f compose.production.yaml up -d --build --wait`. Миграции выполняются при запуске API; поэтому это уже изменение БД, а не диагностика.
6. Первый администратор: `docker compose --env-file .env.production -f compose.production.yaml exec api python -m scripts.create_admin`. При существующем администраторе команда откажется. Seed в production запрещён.
7. Проверьте HTTPS, `/health/ready`, вход по реальному разрешённому тестовому аккаунту, ограничение ролей, upload/download фото, WS reconnect, уведомления, экспорт и сохранность фото после перезапуска. Live smoke со стандартными demo-логинами не запускайте в production.

`AI_EXTERNAL_ENABLED=false` блокирует hosted text/photo/audio. Включать его и конкретного провайдера разрешается только после подготовки обезличенных данных и проверки внешнего сервиса. Автоматического удаления ФИО/лиц из произвольного текста/фото нет. Локальные правила — fallback, не мультимодальная модель. Локальный Estimin3n требует отдельных весов/памяти и в production Docker образ не входит.

## Резервное копирование, обновление, откат

Перед обновлением сохраните SHA обоих репозиториев, собранный frontend, image IDs и конфигурацию в закрытом хранилище. Создайте backup:

```sh
mkdir -p backups
docker compose --env-file .env.production -f compose.production.yaml exec -T postgres pg_dump -U naryadai -d naryadai -Fc > backups/naryadai.dump
```

Архив содержит производственные данные: права доступа ограничить, шифровать и хранить вне сервера. S3 versioning/backup настраивается отдельно, дамп БД фото не содержит. Проверка восстановления выполняется **в новую тестовую БД**, не поверх работающей: `pg_restore --no-owner --dbname=<new-test-db> backups/naryadai.dump`, затем smoke с соответствующим snapshot S3.

Для обновления: review миграций → backup → сборка новых образов → согласованное окно изменения → `up -d --build --wait` → smoke. Если новая схема совместима, откатить предыдущие image IDs и frontend. При несовместимой схеме не запускать автоматический down-migration: восстановить проверенный snapshot в новую БД и переключить приложение по отдельному плану. `down -v`, `dropdb` и перезапись production здесь не предлагаются.

Production Dockerfile использует `requirements.lock`: 45 зафиксированных runtime-пакетов, dry-run установки и аудит выполнены. Базовые Docker tags пока плавающие; зафиксируйте проверенные image digests на целевой Linux-платформе. Контейнерная сборка, Linux smoke и TLS остаются release gate.

## Локальные модели через Ollama (compose.ollama.yaml)

Отдельный профиль `compose.ollama.yaml` поднимает только сервер Ollama
(`127.0.0.1:11434`, volume `ollama_data`, healthcheck `ollama list`):

```sh
docker compose -f compose.ollama.yaml up -d
docker compose -f compose.ollama.yaml exec ollama ollama pull llama3.1:8b
```

Пример переключателя — переменные окружения `OLLAMA_HOST` и `OLLAMA_MODEL`
(см. environment сервиса). Локальные модели требуют GPU/железа заказчика;
качество распознавания и приёмки этим профилем не заявляется и не измерено.
Провайдерной обвязки Ollama в коде API нет: подключение — отдельная задача,
текущий fallback остаётся «локальные правила». Не объединять этот файл
с dev-compose и `compose.production.yaml`.

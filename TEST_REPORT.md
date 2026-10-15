# Фактические проверки — 14.10.2026

Локальный срез двух репозиториев после изменений, не сертификат production readiness. Исторические числа в README/docs исходных репозиториев не относятся к этому прогону. Все нагрузочные и изменяющие данные запросы направлены на loopback и синтетическую БД. Конкуренты проверены по исходному коду; их runtime-тесты не запускались.

## Среда и воспроизведение

macOS arm64; Python **3.12.14** (новая venv); Node **24.19.0**; PostgreSQL **17.11**; Redis **8.10.2**; FastAPI/asyncpg, Moto S3; Playwright **1.64**, Chromium **156**; Vitest **4.1.11**. API `127.0.0.1:8080`, PostgreSQL `25432`, Redis `26379`, Moto `29000`, Playwright Vite `4173`. AI_MODE=mock, hosted providers отключены: проверяется честный локальный fallback. Модельные timeout/invalid JSON сценарии — unit tests с контролируемыми ответами, не вызовы реальной модели.

Команды ниже выполняются из соответствующего репозитория с активной Python venv/Node24. Для native backup-тестов задаётся `PG_BINDIR=/opt/homebrew/opt/postgresql@17/bin` (на другой ОС — путь к pg_dump/pg_restore). Установка/запуск: [DEPLOYMENT.md](DEPLOYMENT.md). `requirements.lock` фиксирует runtime, dev-пакеты устанавливаются из requirements-dev.txt.

## Проверки backend

| Команда / проверка | Результат | Доказательство и предел |
|---|---|---|
| `ruff check app tests` + новые scripts; `ruff format --check …`; `mypy app`; `git diff --check` | PASS | 43 typed source files; пороги/правила не отключены |
| `python -m pytest -q --cov=app --cov-report=term-missing --cov-report=json:docs/evidence/backend-coverage.json` | PASS: **955 tests, 0 skipped**, 23.83s | [лог](docs/evidence/backend-tests.txt), [coverage](docs/evidence/backend-coverage.json): **94.41%**, порог 90%; unit/fake tests и реальные PostgreSQL/API integration в одном suite |
| `python -m pip wheel --no-deps --no-build-isolation . -w <work/wheels>` | PASS | Собран naryadai_backend-0.1.0-py3-none-any.whl; это упаковка Python, не Linux-container smoke |
| `python -m scripts.verify_live --output docs/LIVE_VERIFICATION.json` | PASS: **65 checks**, 13.894s | [JSON](docs/LIVE_VERIFICATION.json): реальные HTTP/WS, auth/IDOR, duplicate requests, переходы, фото, AI recommendation, human decision, audit, reports, inbox и дедлайн |
| `python -m scripts.verify_analytics` | PASS | [JSON](docs/evidence/analytics.json): реальный SQL на temporary tables, rollback; рейтинг 77.1, on-time1.0, штрафуются только неуважительные отказы |
| `python -m scripts.verify_insights` | PASS | [JSON](docs/evidence/insights-regression.json): завершённое ППР, completion-time window, недоступный сотрудник с несколькими участками |
| Native `pg_dump`/`pg_restore` тесты suite | PASS | Временная probe-таблица, проверка восстановления; production БД и S3 backup не проверялись |
| Новый тест атомарности XLSX | PASS | Реальный PostgreSQL TEMP table: первая строка вставляется, вторая вызывает constraint error, вся транзакция откатывается; заменён прежний постоянный skip |
| `python -m pip_audit -r requirements.txt -f json` | PASS | [JSON](docs/evidence/dependency-audit.json): 49 проверенных пакетов,0 известных уязвимостей;45 из них вошли в runtime lock |
| `pip install --dry-run --ignore-installed --only-binary=:all: -r requirements.lock`; `pip check` | PASS | [runtime evidence](docs/evidence/runtime-lock-validation.json):45 exact versions; macOS wheels, Linux не проверен |
| PyYAML safe_load + service/dependency/security structure всех 4Compose | PASS | Проверка YAML не подменяет Docker engine |
| Docker build / `docker compose config` / TLS | BLOCKED | Docker отсутствовал; целевой сервер/секреты не предоставлены |

В security regression входят роли, IDOR, назначение не тому участку, переходы/повторы, auth revocation/expiry на WS, приватность фото, forged JPEG, decompression limits, неправильные UUID/даты и маскирование токенов. Это целевые проверки, не полный независимый penetration test.

## Реальные данные и нагрузка

На **новой пустой** БД выполнены migrate→migrate→seed→seed→verify_dataset→verify_analytics→verify_insights: [fresh smoke](docs/evidence/fresh-database-smoke.json). Созданные ранее БД не очищались. Проверка свежего seed: 603 наряда,96.034 дня,4 участка,25 единиц оборудования,2 мастера,15 исполнителей/3 бригады,20 шифров,40 материалов,0 нарушений порядка lifecycle timestamp. [Данные и сигналы](docs/evidence/dataset.json).

Обнаружены **4/4 заранее заданных пары equipment/type**: повтор М-02 у конвейера, отказ после ППР у дробилки, расход масла у насоса, рост внеплановых ремонтов мельницы. Target recall=1.0 относится только к этим четырём целям. Всего 7 пар, включая 3top-downtime findings под legacyтипом material_anomaly. Глобальная precision/field accuracy не измерена, фиксированные confidence не являются вероятностями. Фото исторических 600 записей не моделируются; реальные upload проверяет отдельный live scenario.

| Измерение | Факт | Интерпретация |
|---|---|---|
| HTTP/WS lifecycle status | **341ms** accept→WS | Один локальный event, требование≤5s выполнено в данном прогоне |
| Background AI review | **511/517ms** | Правила/fallback, не LLM inference |
| Дедлайн→уведомления worker+master | **9.157s** | Реальные часы/фоновые циклы, не time mock |
| HTTP bounded load | 100GET, concurrency5,0 ошибок, p95 **5.098ms** | [JSON](docs/evidence/load.json); локальная короткая выборка, не capacity/SLA; команда `python -m scripts.verify_http_load` |
| WS bounded delivery | 5 авторизованных подписчиков, max **385.7ms**,0 пропусков | [JSON](docs/evidence/realtime-load.json); один transition. Invalid JWT/чужая подписка отвергнуты, негативный наблюдатель не получил событие |

Длинные интервалы escalation3/10/120 минут покрыты scheduler-тестами; живого ожидания всех этих интервалов не было. Межпроцессный fan-out/длительный soak не проверены. API production-профиля намеренно один worker.

## Frontend и браузер

| Команда | Результат | Границы доказательства |
|---|---|---|
| `npm ci` | PASS | Исходный npm10 update упал в Arborist; lock обновлён npm12.2.0 и используется Node24 |
| `npm run typecheck`; `npm run lint`; `npm run format:check`; `npm run build` | PASS | React/TypeScript/Vite + PWA build, lint имеет унаследованные warnings; Android/APK build не запускался |
| `npm run test:coverage` | PASS: **607 tests /59files** | Statements91.82%, branches80.91%, functions89.98%, lines94.44%; gates86/79/80/86 сохранены. [summary](../NaryadAI-frontend/docs/evidence/coverage-summary.json) |
| `npm audit --json` | PASS:0 | [audit](../NaryadAI-frontend/docs/evidence/frontend-audit-final.json); baseline имел 2critical+2moderate test-toolchain vulnerabilities |
| `npm run check:contract` | PASS | Health200; OpenAPI100paths/14required routes. Проверяет пути, не полную совместимость schema; её дополнительно проверяет livebrowser |
| `npx playwright test e2e/login-layout.spec.ts e2e/case-lifecycle.spec.ts --workers=1` | PASS:5 | Login320/390/768/1280 без horizontal overflow, loginbutton≥44px; реальные master+worker browsercontexts с API, upload и WS |
| FullPlaywright run | **146PASS,2FAIL,1SKIP** из 149, промежуточно | FAIL: устаревший MIME assertion и XLSX fixture с areaUUID чужой старойБД. Исправления/повторный результат будут указаны ниже; не считать полныйsuite зелёным по focusedPASS |

Focused live browser: выдача с beforephoto→приём/начало→материал 2.5+afterphoto+comment→backgroundreview→masteroverride85→closed, в том числе обновление worker через WS. Метрики [case-metrics.json](../NaryadAI-frontend/docs/evidence/case-metrics.json): автоматический test body **5.742s**, accept→master **311ms**. Это скорость автоматизации с подготовленными полями, не время человека. Скриншоты [320px worker](../NaryadAI-frontend/docs/evidence/worker-320.png), [completion](../NaryadAI-frontend/docs/evidence/worker-completion-mobile.png), [master](../NaryadAI-frontend/docs/evidence/master-decision.png) просмотрены визуально. Полные browser traces не распространяются: могут содержать тестовые JWT.

## История падений и ограничения

Первый полный backend suite:895PASS/23FAIL/1SKIP; затем 921/11/1. Исправлены реальные enum/timestamp/JSONB ошибки, устаревшие фикстуры, native backup-команды. Промежуточно 940/0/1, затем 941/0/0 после настоящего rollback-теста. После live WS security и XLSX foreign-key regressions финальный прогон 955/0/0. Ни один тест не отключался ради зелёного результата. Coveragefrontend после обновленияVitest первоначально не прошёл branchgate75.41%; добавлены смысловые проверки формы, auth/reconnect, admin/import и общего телефона до 80.91%.

**NOT RUN:** физический Android, APK install, камера/перчатки, offlinephoto recovery, мобильныйupload≤10s, пользовательская выдача≤6 действий/≤1 мин, ручное 7 миндемо, реальное hostedAI/vision qualitybenchmark, backgroundpush на lockeddevice, продакшн TLS/S3 restart/backup и полномасштабная нагрузка. **Отсутствует:** внешняя push интеграция и резервное 3 минвидео. Наличие исходников/настроек не меняет эти статусы.

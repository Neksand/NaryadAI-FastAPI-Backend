# Индекс доказательств — срез 14–15.10.2026

Живой указатель на проверенные артефакты. Первичные отчёты: [TEST_REPORT.md](../TEST_REPORT.md)
(фактические проверки 14.10.2026), [IMPLEMENTATION_REPORT.md](../IMPLEMENTATION_REPORT.md)
(что изменено), [LIVE_VERIFICATION.json](LIVE_VERIFICATION.json) (65 live-проверок).
Числа ниже относятся только к этому срезу; исторические отчёты в `docs/` прежних коммитов
его не подтверждают.

## Сводка

| Проверка | Факт | Доказательство |
|---|---|---|
| Backend-тесты | **955 passed, 0 skipped** (23.83s) | [лог](evidence/backend-tests.txt) |
| Backend-покрытие | **94.41%** (порог 90%) | [coverage JSON](evidence/backend-coverage.json) |
| Frontend-тесты | **607 passed / 59 файлов** | [coverage summary](../../NaryadAI-frontend/docs/evidence/coverage-summary.json) |
| Frontend-покрытие | statements **91.82%**, branches 80.91%, functions 89.98%, lines 94.44% | см. выше |
| Live E2E (focused) | **5/5** (`login-layout` + `case-lifecycle`, живой API + браузер) | [case-metrics](../../NaryadAI-frontend/docs/evidence/case-metrics.json) |
| Full Playwright run | 146 passed, **2 failed**, 1 skipped из 149 (промежуточно) | см. TEST_REPORT.md; focused-пас не красит полный suite |
| Planted-сигналы аналитики | **4/4** заданных пар equipment/type (target recall 1.0 только по ним) | [dataset](evidence/dataset.json) |
| AI хаос-тест (15.10.2026) | **6 passed**: 4 сида × 8 деградаций + взрыв вижн-провайдера + dead-letter/override/loop на scratch-БД | `tests/test_ai_chaos.py` |
| Аудит зависимостей | 49 пакетов, 0 известных уязвимостей; runtime lock 45 пакетов | [audit](evidence/dependency-audit.json), [lock](evidence/runtime-lock-validation.json) |
| Frontend-аудит | `npm audit`: 0 | [audit](../../NaryadAI-frontend/docs/evidence/frontend-audit-final.json) |

## Задержки (локальный стенд, не SLA)

Замеры одного локального прогона на macOS (loopback, синтетическая БД).
Не являются гарантией производительности и не переносятся на прод.

| Измерение | Факт |
|---|---|
| accept → WS-доставка | **341ms** (требование ≤5s выполнено в данном прогоне) |
| Background AI review (правила/fallback, не LLM) | **~511ms** (второе измерение 517ms) |
| Дедлайн → уведомления worker+master | 9.157s (реальные часы/фоновые циклы) |
| HTTP bounded load (100 GET, concurrency 5) | 0 ошибок, p95 5.098ms ([JSON](evidence/load.json)) |
| WS bounded delivery (5 подписчиков) | max 385.7ms, 0 пропусков ([JSON](evidence/realtime-load.json)) |

## Что показывает хаос-тест

`tests/test_ai_chaos.py`, прогнан на scratch-БД `naryadai_chaos` (localhost:15432,
migrate+seed, `DATABASE_URL`-override; базы `naryadai`, `naryadai_stand`, `naryadai_p0`,
`naryadai_ver`, `naryadai_ins`, `naryadai_ins2` не затрагивались):

- текст/вижн провайдеры случайно деградируют (`None`, мусорный JSON, исключение вижн):
  ревью всегда завершается, `verdict` присутствует, строка `ai_reviews` и событие
  `order.ai_review_ready` записаны;
- падающий `run_ai_review` уходит в dead-letter `needs_master_review`, мастер закрывает
  через override тем же SQL, что роут close, повторный fallback идемпотентен;
- после сбоя цикл `outbox_loop` публикует последующие события, ревью по ним создаются.
- Честная граница зафиксирована в docstring теста: исключение из *текстового* провайдера
  гасится не внутри `run_ai_review` (прямой `await _llm_relevance`), а уровнем выше —
  `_handle_ai_review_event` (attempts → dead-letter).

## NOT RUN (не проверялось)

Физический Android / установка APK / камера и работа в перчатках / offline-восстановление
фото / мобильный upload ≤10s / выдача ≤6 действий за ≤1 мин / ручное 7-минутное демо /
реальное hosted-AI и benchmark качества vision / background push на заблокированный
телефон / прод-TLS и прод-S3 (restart/backup/сохранность) / полномасштабная нагрузка /
межпроцессный fan-out и длительный soak / полный независимый penetration test.
Отсутствуют: внешняя push-интеграция и резервное 3-минутное видео.

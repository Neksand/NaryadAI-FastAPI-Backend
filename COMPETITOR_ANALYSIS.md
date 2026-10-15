# Конкурентный анализ NaryadAI — 14 октября 2026

## Метод и границы

Проверены README и исходники **всех четырёх** заданных публичных репозиториев. Они клонированы командой `git clone --depth 1 <URL>.git work/competitors/<имя>`; SHA подтверждены `git rev-parse HEAD`. Ни один конкурент не запускался, APK не устанавливался, внешние AI/FCM/production API не вызывались. Наличие реализации означает «подтверждено чтением кода», а не «успешно прошло независимый E2E». Числа качества/производительности из README не приняты за измерения этого аудита.

Код конкурентов не переносился. По поиску `rg --files … | rg '(^|/)(LICENSE|COPYING)'` файлов лицензии с такими именами не найдено; это не юридическое заключение и не разрешение копировать код.

| Обозначение | Репозиторий | Проверенный HEAD | Роль |
|---|---|---|---|
| Mobile | [tut-titki/naryad-ai-mobile-frontend](https://github.com/tut-titki/naryad-ai-mobile-frontend) | `2cadf2acefd18385e8ab223a12ebd499b1dc6bdb` | Flutter-клиент мастера/исполнителя, Android-проект и APK |
| Backend G | [g0v4k0/naryad-ai-backend](https://github.com/g0v4k0/naryad-ai-backend) | `b83351ea95a07f0dd7b1a02a0a4300d5f065a331` | Express/TypeScript/Prisma/MySQL API |
| CRM | [lobdhd/hackathon_crm](https://github.com/lobdhd/hackathon_crm) | `891b169f5b97ffa0d0fdd8e64235c0d9135c1e92` | React/Vite веб-клиент |
| McLOVINs | [ZLHRS/Industry_Hackathon_McLOVINs_Case1](https://github.com/ZLHRS/Industry_Hackathon_McLOVINs_Case1) | `06450baa8990bf1f6b200bf37c6b0de242256a8a` | Полный FastAPI/PostgreSQL/React-продукт «ТехНаряд» |

Для сравнения использован исходный NaryadAI: backend `e9386fefcf12c946fdccf471f53a698c7b9fb4ea`, frontend `d9063301696a80074f07f982c9f9d1ff66b726b7`. Этот документ описывает **baseline до изменений текущей работы**; их результаты следует читать в IMPLEMENTATION_REPORT и TEST_REPORT.

Mobile и CRM используют один базовый адрес и контракты `/api/work-orders`, `/api/auth`, Socket.IO; CRM README прямо ссылается на `naryad-ai-backend`. Поэтому считать их тремя независимо реализованными backend-продуктами было бы ошибкой. Это клиентские части совместимой экосистемы; организационная принадлежность не устанавливалась.

В финальном `git status --short` Mobile/CRM/McLOVINs чисты. Backend G показывает дополнительное имя PDF в NFC при tracked-имени того же кейса в NFD на macOS; исходники не редактировались, этот артефакт нормализации имён не удалялся.

Уверенность: **высокая** — прямой читаемый путь исполнения; **средняя** — статическая интеграция нескольких модулей без запуска; **не проверено** — измерение, устройство, внешний сервис или deployed-версия.

## Сравнительная матрица

Ссылки на доказательства расшифрованы ниже. «Через Backend G» означает, что самостоятельной серверной реализации в клиентском репозитории нет.

| Критерий | NaryadAI, baseline | Mobile | Backend G | CRM | McLOVINs | Доказательство / уверенность | Действие для NaryadAI |
|---|---|---|---|---|---|---|---|
| Жизненный цикл и защита мутаций | Переходы на сервере, row lock, журнал, idempotency по пользователю | Реальный REST, clientActionId для действий | Явная state machine и журнал; replay до проверки владельца; обновление без сравнения состояния | REST actions, guards в UI; серверные гарантии через G | Доменная машина, ожидаемая версия, row lock, повторная auth replay | N1, G1, M1, C1, T1 / высокая | Сохранить блокировки; тестировать повтор, конкурирующие команды и потерю доступа |
| Мобильная работа | React + Tauri; очередь действий есть, фото отправляется отдельным онлайн-запросом | Flutter, secure storage, FCM, сохранение локальных фото в очереди | API для offline действий и FCM | Адаптивный React, локальная очередь без доказательства нативного push | React/PWA, IndexedDB, Web Push | N2, M1–M3, C1, T4 / средняя | Перенести надёжность offline на фото и черновик; проверить реальный Android |
| Быстрое создание | Отдельная форма MasterNew; фактическое число действий не измерено | Выбор оборудования, рекомендации, сохранение черновика, защита повторной кнопки | Подбор исполнителя и создание с валидацией | Быстрая форма с подсказанным исполнителем/участком | UI выдачи, бригады и рекомендации | M4, C2; UI не исполнялся / средняя | Измерить выдачу с фото и ручной проверкой назначения; цель кейса ≤6 действий |
| ИИ и решение человека | Локальные правила, внешние провайдеры, объяснение; фиксированные confidence; автоматический rework по verdict | Показывает серверную проверку, отдельного AI нет | Правила + Ollama + RAG; fallback и needsMasterReview; отдельное CLOSE мастером | Отображает API AI/аналитики | Структурированный ответ, timeout, worker/lease, при недоступности null-оценка и явное решение мастера | N3, G2, T2 / высокая | Разделить правила/модель/неизвестность, убрать иллюзию калиброванной confidence; финальное решение мастеру |
| Фото | SHA/pHash, EXIF, S3-подобное хранение, правила | Камера/галерея, продолжение частично загруженной очереди | Sharp, подписи URL, vision/pHash | Upload API, UI фото | Фото-проверки с ограничениями доказательности; privacy/redaction | M1, G2, T2 / высокая по коду | Схожее фото = сигнал проверки, не доказанный обман; проверить низкую уверенность |
| Уведомления и real-time | DB + WebSocket; external push отсутствует в config | FCM регистрация, foreground/background, Socket.IO reconnect | DB + Socket.IO + FCM; deadline scan раз в минуту | Socket.IO invalidation + reconnect + offline flush | Transactional outbox, дедупликация, WebSocket revisions, Web Push | N4, G3, M2, M3, C3, T3 / средняя | Надёжно показать новое назначение, просрочку, reconnect; отдельно обозначить пределы push |
| Аналитика | Реальные SQL/сервисы, экономика с предположениями; ручная формула названа probability | API отчёты и UI рейтинга | Повторы 7 дней, базовые частоты, биномиальный порог, нормы материалов | Графики на API и состояния ошибки; старый mock-модуль не используется найденными импортами | Повторы, ППР, возвраты, медианный расход с denominator/order_ids/formula | N5, G4, C4, T5 / высокая | Демонстрировать исходные наряды, знаменатель, формулу; риск-индекс не называть вероятностью отказа |
| Тесты | Backend и frontend suites имеются; результаты отдельно в TEST_REPORT | Flutter unit/widget tests по ролям, фото, race, localization | Vitest unit/integration, research-скрипты | В package.json нет test script; поиск test/spec файлов не дал результатов | Python unit/integration, frontend unit и Playwright E2E | G5, M5, C5, T6 / высокая о наличии | Не сравнивать число тестов без запуска и содержания; проверять свой E2E |
| Эксплуатация | Compose/PostgreSQL/Redis/S3, Caddy, миграции | Android SDK/JDK/Flutter + настроенный внешний API | Compose/MySQL/Ollama/Whisper/FCM; потребность в ресурсах | Vite, env API, PM2 config | Compose с PostgreSQL digest, worker, healthchecks, secrets files, seed | package/config/compose всех репо / средняя | Дать проверенный локальный запуск и чистую конфигурацию; production-ready не заявлять |
| Русский / казахский | ru/kk словари, тест parity | ru/kk ARB, enterprise UTC+5 | Поле языка/системные сообщения | ru/kk JSON и переключение | ru/kk translation modules/tests | каталоги i18n/l10n / высокая о наличии | Пройти реальный путь на обоих языках; наличие словаря не равно полноте перевода |

## Подробные факты и доказательства

### Mobile

- **M1 — реальный offline путь с фото.** `lib/features/executor/data/api_executor_repository.dart:902–989`, `_syncQueue`, читает очередь текущего сотрудника, загружает `_localPhotos`, после каждого успешного upload сохраняет URL в durable storage, затем шлёт действие; `:992–1005` передаёт `clientActionId`. `pending_action_storage_io.dart:18–44` сериализует операции и пишет temp+rename; `:51–63` проверяет владельца. Важная оговорка: простой `OfflineActionQueue` связан с demo-store; в сравнении использован именно API repository, не demo-класс.
- **M2 — push действительно подключён в коде.** `lib/core/services/push_notification_service.dart:75–190`: Firebase initialization, background handler, onMessage, token refresh, разрешение и registerPushDevice. Это сильнее простого значка уведомлений, но фактическая доставка требует Firebase-конфигурации, токена устройства, разрешения и сети; не проверялась.
- **M3 — reconnect и хранение сессии.** `lib/core/services/master_realtime_service.dart:17–54` использует token auth и инвалидирует данные после reconnect; missed events не считаются автоматически доставленными. `lib/features/auth/data/auth_session.dart:5–39` — secure storage на устройстве и память в Web. В `:125` зашит адрес API, параметр окружения для URL в README прямо назван нереализованным.
- **M4 — создание.** `lib/features/orders/screens/order_screens.dart:552–604` — debounce рекомендаций; `:766–905` — guards, валидация, снимок формы, загрузка фото, отдельный POST. На обрыве сети показана просьба проверить список перед повтором: создания с серверной идемпотентностью нет, авторы осознанно не делают автоматический retry. Это слабее безопасного same-key повторного создания NaryadAI. Заявленные README «3 нажатия плюс текст» не измерялись независимо.
- **M5 — тестовый код.** `test/quick_order_creation_test.dart`, `order_creation_safety_test.dart`, `order_response_races_test.dart`, `executor_queue_and_storage_test.dart`, `push_notifications_test.dart`, `localization_test.dart`. Наличие тестов подтверждено, результат Flutter test не получен.
- **Отделённый demo mode.** `lib/main.dart:10`, `lib/app/app.dart:430–458`: `DEMO_MODE` выбирает demo repository; обычный режим выбирает API. Наличие demo-store не означает, что вся программа фиктивна. README честно ограничивает demo режим UI и не обещает настоящий auth/push/AI.
- **Ограничения:** самостоятельного backend/AI здесь нет; менеджер/админ в README направлены в Web; URL требует изменения исходника; natively delivered push и удобство в перчатках не проверены. Существование `release/app-release.apk` не подтверждает соответствие бинарника HEAD, подпись или работоспособность.

Основной permalink: [API offline queue](https://github.com/tut-titki/naryad-ai-mobile-frontend/blob/2cadf2acefd18385e8ab223a12ebd499b1dc6bdb/lib/features/executor/data/api_executor_repository.dart#L902).

### Backend G

- **G1 — реальные правила и два статических риска.** `src/domain/work-order-state.ts:15–46` перечисляет переходы. `src/routes/work-orders.ts:226–294` содержит права, transaction, запись события и вызов проверки. Однако `:231–233` возвращает наряд по совпавшему глобальному `clientActionId` **раньше** owner/role checks `:235–237`; потенциальный обход объектной авторизации при известном ключе требует исправления/проверки. `:229` читает статус до transaction, `:257` обновляет только по id: две разные concurrent actions могут пройти по одному старому статусу. Это выводы статического анализа, не результаты атак. Секреты и production не проверялись.
- **G2 — AI не заглушка.** `src/services/ai-review.ts:53–143`, `reviewWorkOrder`: обязательные поля, аварийное фото, норматив материала ×1.5, safety regex, Ollama, photo analysis, RAG-прецеденты, validation response и bounds score. При отказе Ollama `:95–100` выставляется базовый результат; `:128–129` помечает обязательную проверку мастером. Недостаток: fallback имеет numeric score=4 при заполненных полях, что само по себе не доказывает качество ремонта. Переход в AI_REVIEW выполняется `:138`; CLOSE — отдельное действие мастера.
- **G3 — уведомления.** `src/services/deadlines.ts:54–90`: сканирование сроков, мастер/исполнитель/руководитель, 3/10 минут непринятия, рекомендация свободного работника; cron каждую минуту. `src/realtime.ts:13–38`: JWT, комнаты ролей и пользователей. Ограничения: Socket.IO CORS `origin: true`; роль берётся из JWT при соединении и в этом модуле нет периодической перепроверки отключения/смены роли. `notifyOnce` сначала читает запись, затем создаёт: безопасность от дубликатов при нескольких экземплярах требует отдельного анализа ограничений БД. In-memory Socket.IO без adapter не доказывает масштабирование на несколько процессов.
- **G4 — аналитика вычисляется.** `src/services/analytics.ts:6–25` — повтор той же неисправности/оборудования за 7 дней; `:32–120` — baseline, binomialTail и минимальные условия для сигналов по исполнителю/бригаде; `:166+` — аномалии, `:257+` — прогноз. Это содержательнее декоративного графика. Статистическая связь не доказывает вину работника или причинность; вне собственного synthetic/research набора accuracy не установлена.
- **G5 — качество и запуск.** `test/unit/work-order-state.test.ts`, `test/integration/work-orders.test.ts`, `ai-review.test.ts`, `photo-vision.test.ts`, `analytics.test.ts`, `deadlines.test.ts`, `one-c.test.ts`; package.json содержит build/test/research. README заявляет 345 тестов, 98.5% coverage и 97.2% AI accuracy; **в этом аудите эти числа не воспроизводились**. Деплой использует MySQL, локальные модели и отдельный Whisper, плюс credentials FCM при push. Нельзя приравнивать наличие compose к измеренной готовности.

Первоисточники: [state machine](https://github.com/g0v4k0/naryad-ai-backend/blob/b83351ea95a07f0dd7b1a02a0a4300d5f065a331/src/domain/work-order-state.ts#L15), [action route](https://github.com/g0v4k0/naryad-ai-backend/blob/b83351ea95a07f0dd7b1a02a0a4300d5f065a331/src/routes/work-orders.ts#L226), [AI review](https://github.com/g0v4k0/naryad-ai-backend/blob/b83351ea95a07f0dd7b1a02a0a4300d5f065a331/src/services/ai-review.ts#L53).

### CRM

- **C1 — client offline реален.** `src/offline/workOrderOfflineQueue.js:3–6,28–42,105–147`: ключ на пользователя, максимум 500 записей, localStorage. `workOrderOfflineProcessor.js:165–238` — FIFO; `:294–328` — 409 удаляет действие и обновляет данные, `:331–385` — постоянные 4xx удаляются, 401/сеть/5xx остаются. Конфликт показывается пользователю, но удалённая операция не сохраняется в persistent blocked queue для ручного редактирования. NaryadAI notices и McLOVINs blocked entries — полезные альтернативы.
- **C2 — быстрая выдача.** `src/components/orders/CreateOrderModal.jsx:172–273` — оборудование, derived area, быстрые/полные work suggestions, первый рекомендованный assignee; `:357–375` — реальный API payload/error. Голос и QR присутствуют как подключённые UI-компоненты. Число кликов/полная доступность не измерены.
- **C3 — real-time.** `src/hooks/useRealtimeSync.js:203–302` — token, reconnect, invalidateQueries; `:311–396` — notification:new и work-order:changed. API клиент в `src/api/client.js` использует Axios, timeout 30s и удаление token на 401. Скрытие UI по роли не объявляется серверной гарантией.
- **C4 — аналитика и mocks.** `src/pages/analytics/Analytics.jsx:158–175` берёт dashboard/shift/downtime/forecast/anomalies из queries; `:349–515` различает failure/empty/loading. `src/api/orders.api.js` содержит mockOrders, однако `rg -n 'orders.api|useOrders' src --glob '!orders.api.js'` не нашёл потребителей. Нельзя объявить активную аналитику моковой по наличию этого старого файла.
- **C5 — тестовая слабость и зависимости.** package.json содержит dev/build/lint, но нет test script. По `rg --files … | rg '(test|spec)\.(js|jsx|ts|tsx)$'` тестовые файлы не найдены. Это не доказательство отсутствия любой внешней CI-проверки. TypeScript не используется, API schemas в основном обеспечиваются backend. Существуют ru/kk JSON и React hooks; качество перевода/визуальные контрасты требуют просмотра.

Первоисточники: [offline processor](https://github.com/lobdhd/hackathon_crm/blob/891b169f5b97ffa0d0fdd8e64235c0d9135c1e92/src/offline/workOrderOfflineProcessor.js#L165), [quick create](https://github.com/lobdhd/hackathon_crm/blob/891b169f5b97ffa0d0fdd8e64235c0d9135c1e92/src/components/orders/CreateOrderModal.jsx#L172).

### McLOVINs / ТехНаряд

- **T1 — наиболее явно оформленная модель конкурентных изменений среди прочитанных конкурентов.** `src/naryadai/domain/lifecycle.py:151–250`: чистая policy с role/status/meaningful reason, AI не принимает финальное решение. `application/orders.py:548–571`: idempotency связана с principal/operation/payload; replay повторно проверяет доступ; новая команда блокирует строку и проверяет `expected_version`. `:805–815` поздний AI-ответ сверяется с submission version. Это конкретное сильное инженерное свойство, а не общий рейтинг продукта.
- **T2 — честная неопределённость и фоновые jobs.** `ai/repair_review.py:60–90`: отсутствие ключа даёт verdict/score=None, needs_master_review=True и источник unavailable; `:94–191` — детерминированные проверки и ограничения фотографии, времени, статистических норм; `:194–231` — timeout/errors; `:427–455` — структурная валидация. `application/ai_reviews.py:68–98,401–462` — claim/lease, retries и manual fallback. `domain/lifecycle.py:240–248` не позволяет AI автоматически закрыть или отправить на доработку.
- **T3 — надежность уведомлений.** `application/notification_engine.py:140–178` — persistent дедупликация; `:408–453` — transactional outbox; `:548–693` — непринятие/просрочка/эскалации. `api/realtime.py:31–50,75–125`: origin allowlist, лимиты соединений, auth без query token, периодическая revalidation сессии и revision refresh. Масштабирование и задержка ≤5 секунд не измерены.
- **T4 — offline и UX.** `frontend/src/lib/offline.ts:44–53` — IndexedDB; `:127+` — actor-scoped queue со state pending/blocked; `:287+` — retry. Есть PWA, device setup, photo redaction, ru/kk, frontend tests и e2e. Нативная Android сборка по этим файлам не подтверждается. Наличие service worker не подтверждает offline для каждого сценария.
- **T5 — аналитические доказательства.** `analytics/insights.py:19–106` выводит order_ids, count, denominator и formula для повторов/после-ППР, отмечает отсутствие доказанной причинности; `:109–157` привязывает возвращённую работу к исходному исполнителю даже после переназначения; `:164–195` требует ≥5 наблюдений и расход ≥3 медиан по материалу+типу оборудования. Это понятнее жюри, чем один AI-абзац без выборки. Метод всё ещё эвристический, не модель прогноза отказов с калибровкой.
- **T6 — инженерная полнота и ограничения.** pyproject.toml задаёт Python 3.13, Ruff, strict mypy, pytest и coverage gate; `frontend/e2e/` содержит сценарии, `compose.yaml` — PostgreSQL pin, healthchecks, worker и volumes. README описывает 600 синтетических нарядов и 500+ за 90 дней. Эти числа в реальной базе не пересчитаны. Внешний AI требует API key, real push требует VAPID/browser permission; без этого полноценное поведение отличается. Дополнительные fault reports, Excel catalog import, repair memory/prevention увеличивают поверхность поддержки — их наличие не означает автоматически более удобный MVP.

Первоисточники: [конкурентные мутации](https://github.com/ZLHRS/Industry_Hackathon_McLOVINs_Case1/blob/06450baa8990bf1f6b200bf37c6b0de242256a8a/src/naryadai/application/orders.py#L548), [manual fallback](https://github.com/ZLHRS/Industry_Hackathon_McLOVINs_Case1/blob/06450baa8990bf1f6b200bf37c6b0de242256a8a/src/naryadai/ai/repair_review.py#L60), [аналитические доказательства](https://github.com/ZLHRS/Industry_Hackathon_McLOVINs_Case1/blob/06450baa8990bf1f6b200bf37c6b0de242256a8a/src/naryadai/analytics/insights.py#L19).

### NaryadAI: основания для сравнения

- **N1:** backend `app/routers/work_orders.py:620–865` — server transition, `FOR UPDATE` на `:651`, role/area policy; `app/deps.py:149–222` — idempotency, `(employee_id,key)`, hash mismatch, row lock. `app/deps.py:16–65` перепроверяет enabled user, revocation сессии и auth_version. Это сильнее одного JWT role на lifetime соединения Backend G; и не повод объявлять весь проект безопасным без негативных тестов.
- **N2:** frontend `src/pages/WorkerOrder.tsx:127–149` ставит offline actions с UUID в очередь; `:170–180` фото сразу требует upload. `src/hooks/useOfflineQueue.ts:58–110` разбирает постоянные конфликты и фиксирует notices. Mobile сохраняет сами фото для более полного offline completion; это объективный разрыв baseline.
- **N3:** backend `app/services/ai_review.py:203–254` проверяет обязательные поля и материалы, но содержит фиксированную confidence `.68/.52` и score `90/82`; `:295–297` auto rework. Эти числа — эвристические уровни, не измеренная вероятность правильности. `app/ai/gateway.py` честно называет default `AI_MODE=mock`; нельзя демонстрировать его как вызов внешней модели.
- **N4:** `app/config.py:44` явно указывает DB+WebSocket без external push. Это не ошибка скрытого отсутствия FCM, а честное ограничение каналов. Native background delivery требует дополнительного провайдера.
- **N5:** `app/services/economics.py:8–110` сохраняет неизвестные цены/нормы как unknown, публикует assumptions; `:144–194` считает сценарий NPV/IRR/ROI без AI-выдумок. В прочитанных конкурентных модулях аналогичный столь явный сценарный расчёт не обнаружен. `app/routers/forecast.py:43–54` — ручная bounded formula, поле probability без калибровки; лучше сохранять API совместимость и ясно маркировать показатель индексом риска.

## Что у конкурентов сильнее

1. Mobile: offline загрузка фото с сохранением прогресса и нативная цепочка FCM. В NaryadAI baseline фото требует сети, external push отсутствует. Работоспособность на конкретном Android ещё надо измерить.
2. McLOVINs: явная version-конкуренция, повторная авторизация replay и проверка submission version позднего AI-ответа. У NaryadAI блокировки/idempotency уже есть, но это не тождественно optimistic version contract.
3. McLOVINs: при отказе AI нет числового балла «качества», решение всегда остаётся мастеру. Это яснее фиксированных confidence/auto rework baseline.
4. McLOVINs: аналитический ответ сразу содержит знаменатель, формулу и связанные наряды; Backend G добавляет baseline и проверку случайности для отдельных сигналов. Такие доказательства ценнее агрессивных прогнозных процентов без проверки.
5. Backend G: предусмотрены локальные текст/vision/embedding/voice модели и очередь 1С. Преимущество актуально, если предприятие обеспечит оборудование и интеграцию; ни скорость GPU, ни реальная 1С этим аудитом не подтверждены.

## Что у NaryadAI уже хорошо и следует сохранить

1. Server-side row locking, user-scoped idempotency, area scoping и проверка отзывов сессий — полезная основа; нельзя заменять её UI-условиями ради простоты.
2. В frontend есть настоящий API, offline notices и автоматические тесты компонентов/форм; у CRM тестовый набор в просмотренном дереве не найден. Количество/прохождение тестов сравнивать без запуска нельзя.
3. Экономические сценарии публикуют входные допущения и не подставляют отсутствующие цены. Это защищает доверие при демонстрации потенциального эффекта.
4. Backend/frontend уже содержат тесты AI safety, upload privacy, idempotency, интеграции и локализации — их следует запустить/усилить, а не переписывать систему.

## Общие слабые места и возможность преимущества

- Ни один независимый Android/производственный тест, измерение задержки или SLA конкурентов здесь не получен. Можно выиграть доказуемым live E2E и честными логами, не количеством экранов.
- AI score и confidence легко выглядят научнее, чем являются. Преимущество — показать источник проверки, known/unknown, failure mode и решение мастера.
- Offline должен включать фото, черновики, смену пользователя, конфликт и повтор после неизвестного ответа сервера. Запись JSON в storage сама по себе этого не гарантирует.
- История ремонта позволяет строить сигналы, но не доказывает причину отказа/вину исполнителя. Полезны ссылки на события, размер выборки, версия метода и ограничение вывода.
- Realtime без проверки reconnect/revocation/двух процессов не означает надёжной доставки. Durable notifications и измеренная задержка нужны отдельно.

## 10 приоритетных улучшений по весам жюри

Оценки трудоёмкости — инженерные ориентиры на подготовленном проекте, не фактически затраченное время. Состояние выполнения фиксируется в основном IMPLEMENTATION_REPORT.

| Приоритет | Изменение | Баллы кейса, которым помогает | Риск / трудоёмкость | Зависимость | Проверка готовности |
|---|---|---|---|---|---|
| P0 | Сквозной master→worker→AI→master; запрет чужих/недопустимых действий | MVP 25, применимость 10 | Высокий / 1–2 дня | Запуск БД/API/UI | Реальный E2E + негативные API; журнал автора/времени |
| P1 | Решение мастера окончательное; честный AI unavailable/low-confidence | AI 25, доверие 10 | Средний / 0.5–1 день | Вердикт и UI приёмки | Нет auto-close/rework от модели; failure/timeout/invalid-json тесты |
| P1 | Объяснимые правила: фото, поля, материалы, длительность, mismatch | AI 25 | Средний / 0.5–1 день | Справочники и нормы | Контролируемые positive/negative/edge примеры, причины видны |
| P1 | Проверяемый seed ≥500, ≥90 дней и ожидаемые аналитические сигналы | Аналитика 15, демо 10 | Средний / 0.5–1 день | Миграции и генератор | Пересчёт количества/периода; assert ожидаемых order_ids/сигналов |
| P1 | Reconnect, дедуп уведомлений, эскалация непринятого/просроченного | MVP 25, удобство 15 | Средний / 0.5–1 день | Фоновый worker | Два пользователя, задержка измерена, reconnect и повтор сканирования |
| P1 | Воспроизводимый локальный Compose/нативный запуск, health/smoke | MVP 25, демо 10 | Средний / 0.5–1 день | Runtime/DB/S3 | Чистый старт, миграции, seed, login, create и photo smoke |
| P1 | Risk score с честной маркировкой вместо неподтверждённой probability | Аналитика 15 | Низкий / 1–3 часа | API/UI контракт | UI и report называют индексом; формула и ограничения доступны |
| P2 | Offline фото + черновики + persistent conflict/retry | Удобство 15, применимость 10 | Средний / 1–2 дня | IndexedDB/storage, upload API | Airplane-mode: заполнить, закрыть, перезапустить, синхронизировать один раз |
| P2 | Быстрое создание: оборудование→описание→подбор→выдать | Удобство 15, демо 10 | Средний / 0.5–1 день | Рекомендации, безопасные default | Измеренный сценарий ≤6 действий, ошибку назначения можно исправить |
| P2 | Нативный/Web Push через отключаемый provider с delivery evidence | Применимость 10, MVP 25 | Средний / 1–2 дня + credentials | FCM/VAPID, устройство | При заблокированном экране получен тестовый push; отказ разрешения не ломает inbox |

## Что действительно показывать в живом демо

1. Две роли с настоящей авторизацией; запись одного и того же наряда на обоих экранах.
2. Реальное фото и создание; повтор запроса с тем же ключом не создаёт второй наряд.
3. Приём, очередь/пауза, начало, закрытие исполнителем; история содержит автора/время.
4. Пропущенное обязательное фото или превышение нормы → конкретная причина; AI недоступен → честная ручная проверка.
5. Решение мастера и отчёт смены; итоговая оценка/формула раскрывается.
6. Проверяемая planted anomaly с переходом к исходным нарядам, размером выборки и ограничением причинного вывода.
7. Разрыв связи и reconnect с замером обновления; offline-работу с фото показывать только после фактической проверки.

Нельзя заявлять в презентации без отдельных измерений: победу над конкурентами, проценты снижения простоев, production-ready, калиброванную вероятность отказа, точность AI из чужого README, фактическую FCM-доставку или пройденный Android-тест.

## Протокол выполненной проверки конкурентов

| Команда / действие | Результат |
|---|---|
| `git clone --depth 1` для всех четырёх URL | PASS: исходники доступны |
| `git rev-parse HEAD` в каждом clone | PASS: SHA зафиксированы выше |
| `rg --files`, чтение README/package/config и перечисленных модулей с `nl -ba` | PASS: статическая проверка фактов |
| `git status --short` | PASS для сохранности исходников; NFC/NFD PDF-имя Backend G описано выше |
| Backend G `npm test`, `npm run build` | NOT RUN: чужой runtime/БД не разворачивались |
| CRM build/lint/browser UX | NOT RUN |
| Mobile `flutter test`, APK install, FCM | NOT RUN |
| McLOVINs pytest/frontend/E2E/Compose | NOT RUN |
| Production endpoints конкурентов | NOT RUN: никаких мутаций/нагрузки/входа |

Вывод анализа: конкуренты содержат значимые реальные реализации. Самый полезный путь для NaryadAI — довести существующие сильные механизмы до воспроизводимого сквозного сценария, устранить разрыв между AI-оценкой и человеческим решением, а затем доказать аналитику и устойчивость к плохой связи. Честного общего ранжирования без одинаковых runtime-тестов этот аудит не позволяет.

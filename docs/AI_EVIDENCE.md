# AI Evidence — честные измерения mock-конвейера

> **Честная рамка:** наш AI — детерминированный mock (`app/ai/mock.py`),
> без GPU, без обучения LLM. Ниже — методология + **измеренные цифры
> mock-конвейера** из реального прогона. Цифры относятся только
> к детерминированным правилам, не к «интеллекту» модели.

## 1. Что прочитано

- `app/ai/mock.py` — ветки `inspect_completion` (единственное место вердиктов):
  - `FAIL` (score 42 / `REWORK`), если `ctx["duplicate"]` или `ctx["missing"]`;
  - `REVIEW` (score 74 / `MASTER_REVIEW`), если `ctx["material_flag"]` или `ctx["stale_exif"]`;
  - иначе `PASS` (score 91 / `CLOSE`). `provider` всегда `"mock"`, объяснения ru/kk.
- `app/ai/gateway.py` — `inspect_completion` делегирует напрямую в mock
  (детерминированные правила первыми, внешний vision/text — слоем выше в `ai_review`).
- `app/ai/inspection.py` — тонкий `inspect()` → `gateway.inspect_completion`.

`app/` не изменялся, коммитов нет.

## 2. Методика

Файл: `tests/test_ai_evidence.py` — pure unit, sync + `asyncio.run`
(как в `tests/test_top1_round3.py`). Маппинг «отчёт → ctx» —
`report_to_ctx()`: пустой/бланк/генерик (`сделано`, `выполнено`, `ок`, …)
или текст короче 15 символов → `missing=["work_done_text"]`;
пустой шифр → `missing+=["fault_code_id"]`; флаги
`duplicate_photo / material_flag / stale_exif` пробрасываются как есть.

### Матрица типов (синтетическая база, 24 отчёта)

| Группа | N | Типы | Ожидание |
|---|---|---|---|
| good-01…12 | 12 | полные `work_done_text` + шифр + материалы + фото-флаги ok (ru ×8, kk ×4, сантехника/электрика/кровля/двери) | `PASS` |
| bad-01 пустой | 1 | `""` | `FAIL` |
| bad-02 бланк | 1 | `"    "` | `FAIL` |
| bad-03/04/05 генерик | 3 | `сделано` / `выполнено` / `ок` | `FAIL` |
| bad-06 без шифра | 1 | полный текст, `fault_code_id=""` | `FAIL` |
| bad-07/08 дубли | 2 | хороший текст + `duplicate_photo=True` (ru + kk) | `FAIL` |
| bad-09/10 материалы | 2 | хороший текст + `material_flag=True` (ru + kk) | `REVIEW` |
| bad-11/12 stale EXIF | 2 | хороший текст + `stale_exif=True` (ru + kk) | `REVIEW` |

Каждый отчёт × языки `ru/kk` → 24×2 = 48 проверок.
Ассёрты: плохие → `FAIL`/`REVIEW`, никогда `PASS`; хорошие → `PASS`
(плюс `recommendation`/`score`/`provider=="mock"`).

### Holdout-разбивка (8 вручную написанных новых кейсов)

4 good + 4 bad, тексты отсутствуют в остальных тестах
(проверено `grep -rF` по `tests/` — совпадений нет, кроме самого
`test_ai_evidence.py`):

- `hold-good-01` насос Pedrollo TOP 2 → `PASS`;
- `hold-good-02` (kk) LED-шамдар + датчик → `PASS`;
- `hold-good-03` редуктор Valtec → `PASS`;
- `hold-good-04` шов/Вилатерм кв.47 → `PASS`;
- `hold-bad-01` генерик `всё сделано` → `FAIL`;
- `hold-bad-02` счётчик ITELMA + дубликат → `FAIL`;
- `hold-bad-03` теплоноситель 80 л + `material_flag` → `REVIEW`;
- `hold-bad-04` (kk) сүзгі + `stale_exif` → `REVIEW`.

8×2 языка = 16 проверок, те же ассёрты. Итого уникальных
отчётов 12+12+8 = **32**, вердикт-проверок 32×2 = **64**
(+3 структурных теста: уникальность holdout, делегация
gateway/inspection ×2 языка = **67** тестов в файле).

### Как воспроизвести одной командой

```bash
.venv/bin/python -m pytest tests/test_ai_evidence.py -q
```

`ruff check` + `ruff format` для файла — чисто.

## 3. Таблица результатов (реальный прогон, 2026-10-14)

Прогон: `67 passed in 0.10s`. Распределение вердиктов
по 64 проверкам: `PASS 32 / FAIL 20 / REVIEW 12`.

| Метрика | Определение | Значение |
|---|---|---|
| Верные вердикты | совпало с ожиданием / 64 | **100% (64/64)** |
| Найденные плохие (recall bad) | плохие → FAIL/REVIEW / 32 | **100% (32/32)** |
| Ложные доработки (FPR good) | хорошие → FAIL/REVIEW / 32 | **0% (0/32)** |
| Base-good | PASS / 24 | 100% (24/24) |
| Base-bad | FAIL/REVIEW / 24 | 100% (24/24) |
| Holdout (unseen) | верные / 16 | 100% (16/16) |

## 4. Честные границы

1. **Mock без LLM:** измерен детерминированный конвейер
   (`missing/duplicate → FAIL`, `material_flag/stale_exif → REVIEW`),
   а не обученная модель. 100% — следствие правил, а не обобщения нейросети.
2. **GPU-исследований нет:** обучения, файнтюна, GPU-бенчмарков не было.
3. **Синтетика + маппинг:** `report_to_ctx()` — тестовая эвристика
   (генерик/короткий текст = missing). Реальный `run_ai_review`
   (EXIF 12ч, phash ≤6, материалы >2× нормы, keyword/LLM-релевантность)
   покрыт отдельно в `test_ai_review_service.py` и здесь не измерялся.
4. **Границы обобщения:** измерена проверка детерминированных веток,
   а не обобщающая способность обученной модели. 100% отражает свойства
   правил, а не ML-качество.
5. Существующие тесты не тронуты; изменены только
   `tests/test_ai_evidence.py` (новый) и `docs/AI_EVIDENCE.md` (новый).

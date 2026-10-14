"""AI evidence: deterministic mock-pipeline methodology check. Pure unit.

What this is (honest): our AI is a deterministic mock (app/ai/mock.py),
no GPU / LLM training. This file measures the mock conveyor itself:
synthetic base (24 reports) + manually written holdout (8 new texts,
absent from other tests) x languages ru/kk. No DB/Redis/S3/network.

Branch map (mock.py :: inspect_completion):
  FAIL   if ctx["duplicate"] or ctx["missing"] (score 42 / REWORK)
  REVIEW if ctx["material_flag"] or ctx["stale_exif"] (score 74 / MASTER_REVIEW)
  PASS   otherwise (score 91 / CLOSE)
"""

import asyncio

import pytest

from app.ai.mock import MockAIProvider

LANGS = ["ru", "kk"]

# Generic / too-short texts count as missing work description.
GENERIC_TEXTS = {
    "сделано",
    "сделано.",
    "выполнено",
    "готово",
    "ок",
    "ok",
    "done",
    "все сделано",
    "всё сделано",
    "сделали",
    "сделали все",
    "сделали всё",
    "жасалды",
    "орындалды",
    "істелді",
}
SHORT_THRESHOLD = 15


def run(coro):
    return asyncio.run(coro)


def report_to_ctx(report: dict) -> dict:
    """Map a human-like report to mock ctx flags (deterministic rules)."""
    missing: list[str] = []
    text = report.get("work_done_text") or ""
    norm = text.strip().lower()
    if not norm or norm in GENERIC_TEXTS or len(text.strip()) < SHORT_THRESHOLD:
        missing.append("work_done_text")
    if not report.get("fault_code_id"):
        missing.append("fault_code_id")
    return {
        "missing": missing,
        "duplicate": bool(report.get("duplicate_photo")),
        "material_flag": bool(report.get("material_flag")),
        "stale_exif": bool(report.get("stale_exif")),
    }


def expected_of(report: dict) -> str:
    ctx = report_to_ctx(report)
    if ctx["duplicate"] or ctx["missing"]:
        return "FAIL"
    if ctx["material_flag"] or ctx["stale_exif"]:
        return "REVIEW"
    return "PASS"


# 12 good: full work_done_text + cipher + materials ok + photo flags ok.
GOOD_REPORTS = [
    {
        "id": "good-01",
        "work_done_text": "Заменён циркуляционный насос Wilo Star-RS 25/4, система промыта, давление 4.2 бар в норме.",
        "fault_code_id": "F-01",
        "materials": [{"name": "Насос Wilo", "qty": 1}],
        "duplicate_photo": False,
        "material_flag": False,
        "stale_exif": False,
    },
    {
        "id": "good-02",
        "work_done_text": "Отремонтирован шиберный затвор Ду-150 на стояке отопления, заменён сальник, протечек нет.",
        "fault_code_id": "F-02",
        "materials": [{"name": "Сальник Ду-150", "qty": 1}],
        "duplicate_photo": False,
        "material_flag": False,
        "stale_exif": False,
    },
    {
        "id": "good-03",
        "work_done_text": "Ауыстырылды айналым сорғысының мойынтірегі, май құйылды, сынақтан сәтті өтті, ағу жоқ.",
        "fault_code_id": "F-03",
        "materials": [{"name": "Мойынтірек 6204", "qty": 2}],
        "duplicate_photo": False,
        "material_flag": False,
        "stale_exif": False,
    },
    {
        "id": "good-04",
        "work_done_text": "Перетянуты контактные соединения в щите ЩР-2, заменён автомат ABB S203 C16, замеры в норме.",
        "fault_code_id": "F-04",
        "materials": [{"name": "Автомат ABB C16", "qty": 1}],
        "duplicate_photo": False,
        "material_flag": False,
        "stale_exif": False,
    },
    {
        "id": "good-05",
        "work_done_text": "Заменён картридж фильтра Honeywell FF06 1/2, промыт грязевик, расход воды восстановлен.",
        "fault_code_id": "F-05",
        "materials": [{"name": "Картридж FF06", "qty": 1}],
        "duplicate_photo": False,
        "material_flag": False,
        "stale_exif": False,
    },
    {
        "id": "good-06",
        "work_done_text": "Жылыту радиаторының ағуы жойылды, тығыздағыш ауыстырылды, қысым 1.5 бар деңгейінде ұсталды.",
        "fault_code_id": "F-06",
        "materials": [{"name": "Тығыздағыш", "qty": 2}],
        "duplicate_photo": False,
        "material_flag": False,
        "stale_exif": False,
    },
    {
        "id": "good-07",
        "work_done_text": "Заменён участок трубопровода ХВС 32 мм длиной 2.5 м, установлены краны Bugatti, опрессовка пройдена.",
        "fault_code_id": "F-07",
        "materials": [{"name": "Труба ППР 32мм", "qty": 2.5}],
        "duplicate_photo": False,
        "material_flag": False,
        "stale_exif": False,
    },
    {
        "id": "good-08",
        "work_done_text": "Прочищен канализационный стояк до подвала, извлечены отложения, сток свободный, фото до и после приложены.",
        "fault_code_id": "F-08",
        "materials": [{"name": "Трос сантехнический", "qty": 1}],
        "duplicate_photo": False,
        "material_flag": False,
        "stale_exif": False,
    },
    {
        "id": "good-09",
        "work_done_text": "Электр қалқанындағы күйген сымдар ауыстырылды, кернеу 220 В қалыпты, қорғаныс автоматтары тексерілді.",
        "fault_code_id": "F-09",
        "materials": [{"name": "Сым ВВГ 2.5", "qty": 4}],
        "duplicate_photo": False,
        "material_flag": False,
        "stale_exif": False,
    },
    {
        "id": "good-10",
        "work_done_text": "Заменён ТЭН бойлера Ariston 2 кВт, бак промыт от накипи, нагрев до 60 градусов за 45 минут.",
        "fault_code_id": "F-10",
        "materials": [{"name": "ТЭН 2кВт", "qty": 1}],
        "duplicate_photo": False,
        "material_flag": False,
        "stale_exif": False,
    },
    {
        "id": "good-11",
        "work_done_text": "Отрегулирована доводочная пружина двери подъезда, заменены петли, закрытие плавное без стука.",
        "fault_code_id": "F-11",
        "materials": [{"name": "Петля дверная", "qty": 2}],
        "duplicate_photo": False,
        "material_flag": False,
        "stale_exif": False,
    },
    {
        "id": "good-12",
        "work_done_text": "Шатырдың 3 шаршы метр жабыны ауыстырылды, су ағарлар тазартылды, қайта ағу байқалмады.",
        "fault_code_id": "F-12",
        "materials": [{"name": "Жабын парағы", "qty": 3}],
        "duplicate_photo": False,
        "material_flag": False,
        "stale_exif": False,
    },
]

# 12 bad: empty / generic / duplicates / material_flag / stale_exif.
BAD_REPORTS = [
    {
        "id": "bad-01-empty",
        "work_done_text": "",
        "fault_code_id": "F-01",
        "materials": [],
        "duplicate_photo": False,
        "material_flag": False,
        "stale_exif": False,
        "expected": "FAIL",
    },
    {
        "id": "bad-02-blank",
        "work_done_text": "    ",
        "fault_code_id": "F-02",
        "materials": [],
        "duplicate_photo": False,
        "material_flag": False,
        "stale_exif": False,
        "expected": "FAIL",
    },
    {
        "id": "bad-03-generic-ru",
        "work_done_text": "сделано",
        "fault_code_id": "F-03",
        "materials": [],
        "duplicate_photo": False,
        "material_flag": False,
        "stale_exif": False,
        "expected": "FAIL",
    },
    {
        "id": "bad-04-generic-done",
        "work_done_text": "выполнено",
        "fault_code_id": "F-04",
        "materials": [],
        "duplicate_photo": False,
        "material_flag": False,
        "stale_exif": False,
        "expected": "FAIL",
    },
    {
        "id": "bad-05-short-ok",
        "work_done_text": "ок",
        "fault_code_id": "F-05",
        "materials": [],
        "duplicate_photo": False,
        "material_flag": False,
        "stale_exif": False,
        "expected": "FAIL",
    },
    {
        "id": "bad-06-no-cipher",
        "work_done_text": "Промыт теплообменник котла Navien, удалена накипь, розжиг стабильный, тяга в норме.",
        "fault_code_id": "",
        "materials": [{"name": "Промывка", "qty": 1}],
        "duplicate_photo": False,
        "material_flag": False,
        "stale_exif": False,
        "expected": "FAIL",
    },
    {
        "id": "bad-07-duplicate",
        "work_done_text": "Заменён смеситель в санузле Grohe Eurosmart, установлены эксцентрики, течь устранена полностью.",
        "fault_code_id": "F-07",
        "materials": [{"name": "Смеситель", "qty": 1}],
        "duplicate_photo": True,
        "material_flag": False,
        "stale_exif": False,
        "expected": "FAIL",
    },
    {
        "id": "bad-08-duplicate-kk",
        "work_done_text": "Жертөледегі кәріз құбырының 2 метр учаскесі ауыстырылды, қосылыстар герметиктелді, иіс жоқ.",
        "fault_code_id": "F-08",
        "materials": [{"name": "Құбыр 110мм", "qty": 2}],
        "duplicate_photo": True,
        "material_flag": False,
        "stale_exif": False,
        "expected": "FAIL",
    },
    {
        "id": "bad-09-material",
        "work_done_text": "Покрашены стены лестничной клетки 40 кв.м, нанесены два слоя водоэмульсионной краски Dulux.",
        "fault_code_id": "F-09",
        "materials": [{"name": "Краска Dulux", "qty": 99}],
        "duplicate_photo": False,
        "material_flag": True,
        "stale_exif": False,
        "expected": "REVIEW",
    },
    {
        "id": "bad-10-material-kk",
        "work_done_text": "Кіреберіс қабырғалары сырланды, 25 шаршы метр, екі қабат акрил бояуы жағылды, жолақтар жоқ.",
        "fault_code_id": "F-10",
        "materials": [{"name": "Бояу", "qty": 99}],
        "duplicate_photo": False,
        "material_flag": True,
        "stale_exif": False,
        "expected": "REVIEW",
    },
    {
        "id": "bad-11-stale",
        "work_done_text": "Заменён сифон под раковиной в кухне, установлено гофроколено 40мм, слив свободный без подтеканий.",
        "fault_code_id": "F-11",
        "materials": [{"name": "Сифон", "qty": 1}],
        "duplicate_photo": False,
        "material_flag": False,
        "stale_exif": True,
        "expected": "REVIEW",
    },
    {
        "id": "bad-12-stale-kk",
        "work_done_text": "Ас үйдегі шүмектің иілгіш жалғауы ауыстырылды, су қысымы қалыпты, тамшы ағу тоқтады толық.",
        "fault_code_id": "F-12",
        "materials": [{"name": "Жалғау", "qty": 1}],
        "duplicate_photo": False,
        "material_flag": False,
        "stale_exif": True,
        "expected": "REVIEW",
    },
]

# Holdout: 8 manually written NEW texts, absent from other tests
# (verified via grep over tests/). 4 good + 4 bad.
HOLDOUT_REPORTS = [
    {
        "id": "hold-good-01",
        "work_done_text": "Перемотана обмотка дренажного насоса Pedrollo TOP 2, заменён поплавковый выключатель, откачка проверена.",
        "fault_code_id": "H-01",
        "materials": [{"name": "Выключатель поплавковый", "qty": 1}],
        "duplicate_photo": False,
        "material_flag": False,
        "stale_exif": False,
        "expected": "PASS",
    },
    {
        "id": "hold-good-02",
        "work_done_text": "Кіреберіс шамдары жарықдиодтыға ауыстырылды 6 дана, қозғалыс датчигі бапталды, жарық біркелкі.",
        "fault_code_id": "H-02",
        "materials": [{"name": "Шам LED", "qty": 6}],
        "duplicate_photo": False,
        "material_flag": False,
        "stale_exif": False,
        "expected": "PASS",
    },
    {
        "id": "hold-good-03",
        "work_done_text": "Заменён редуктор давления Valtec 1/2 на вводе, настроено 3 бара, манометр опломбирован мастером.",
        "fault_code_id": "H-03",
        "materials": [{"name": "Редуктор Valtec", "qty": 1}],
        "duplicate_photo": False,
        "material_flag": False,
        "stale_exif": False,
        "expected": "PASS",
    },
    {
        "id": "hold-good-04",
        "work_done_text": "Герметизирован межпанельный шов квартиры 47 длиной 6 м, уложен Вилатерм и мастика, протечек нет.",
        "fault_code_id": "H-04",
        "materials": [{"name": "Вилатерм", "qty": 6}],
        "duplicate_photo": False,
        "material_flag": False,
        "stale_exif": False,
        "expected": "PASS",
    },
    {
        "id": "hold-bad-01-generic",
        "work_done_text": "всё сделано",
        "fault_code_id": "H-05",
        "materials": [],
        "duplicate_photo": False,
        "material_flag": False,
        "stale_exif": False,
        "expected": "FAIL",
    },
    {
        "id": "hold-bad-02-duplicate",
        "work_done_text": "Установлен новый счётчик ХВС ITELMA WFK20, показания 12.4 куб.м, пломба номер 88412 цела.",
        "fault_code_id": "H-06",
        "materials": [{"name": "Счётчик ITELMA", "qty": 1}],
        "duplicate_photo": True,
        "material_flag": False,
        "stale_exif": False,
        "expected": "FAIL",
    },
    {
        "id": "hold-bad-03-material",
        "work_done_text": "Промыта система отопления подъезда, залит теплоноситель 80 л, давление 2 бара держится сутки.",
        "fault_code_id": "H-07",
        "materials": [{"name": "Теплоноситель", "qty": 80}],
        "duplicate_photo": False,
        "material_flag": True,
        "stale_exif": False,
        "expected": "REVIEW",
    },
    {
        "id": "hold-bad-04-stale",
        "work_done_text": "Су есептегіштің сүзгісі тазартылды, тығыздағыштар ауыстырылды, ағу толық тоқтады, қысым қалыпты.",
        "fault_code_id": "H-08",
        "materials": [{"name": "Тығыздағыш", "qty": 2}],
        "duplicate_photo": False,
        "material_flag": False,
        "stale_exif": True,
        "expected": "REVIEW",
    },
]


def _assert_pass(out: dict, lang: str):
    assert out["verdict"] == "PASS"
    assert out["recommendation"] == "CLOSE"
    assert out["provider"] == "mock"
    assert out["score"] == 91
    assert out["issues"] == []


def _assert_not_pass(out: dict, expected: str, lang: str):
    assert out["verdict"] == expected
    assert out["verdict"] in ("FAIL", "REVIEW")
    assert out["verdict"] != "PASS"
    assert out["provider"] == "mock"
    if expected == "FAIL":
        assert out["recommendation"] == "REWORK"
    else:
        assert out["recommendation"] == "MASTER_REVIEW"


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("report", GOOD_REPORTS, ids=[r["id"] for r in GOOD_REPORTS])
def test_good_reports_pass(report, lang):
    m = MockAIProvider()
    out = run(m.inspect_completion(report_to_ctx(report), lang))
    assert expected_of(report) == "PASS"
    _assert_pass(out, lang)


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("report", BAD_REPORTS, ids=[r["id"] for r in BAD_REPORTS])
def test_bad_reports_never_pass(report, lang):
    m = MockAIProvider()
    out = run(m.inspect_completion(report_to_ctx(report), lang))
    assert expected_of(report) == report["expected"]
    _assert_not_pass(out, report["expected"], lang)


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("report", HOLDOUT_REPORTS, ids=[r["id"] for r in HOLDOUT_REPORTS])
def test_holdout_unseen_reports(report, lang):
    m = MockAIProvider()
    out = run(m.inspect_completion(report_to_ctx(report), lang))
    assert expected_of(report) == report["expected"]
    if report["expected"] == "PASS":
        _assert_pass(out, lang)
    else:
        _assert_not_pass(out, report["expected"], lang)


def test_holdout_texts_are_new_and_unique():
    base_texts = {r["work_done_text"].strip().lower() for r in GOOD_REPORTS + BAD_REPORTS}
    hold_texts = [r["work_done_text"].strip().lower() for r in HOLDOUT_REPORTS]
    assert len(set(hold_texts)) == len(hold_texts) == 8
    assert not (set(hold_texts) & base_texts)
    assert len(GOOD_REPORTS) == 12
    assert len(BAD_REPORTS) == 12
    assert len(HOLDOUT_REPORTS) == 8
    assert len(GOOD_REPORTS) + len(BAD_REPORTS) + len(HOLDOUT_REPORTS) >= 32


@pytest.mark.parametrize("lang", LANGS)
def test_gateway_and_inspection_delegate_branches(lang):
    import app.config as cfg
    from app.ai import gateway
    from app.ai.inspection import inspect

    cfg.settings.AI_MODE = "mock"
    good = run(gateway.inspect_completion(report_to_ctx(GOOD_REPORTS[0]), lang))
    assert good["verdict"] == "PASS"
    bad = run(inspect(report_to_ctx(BAD_REPORTS[0]), lang))
    assert bad["verdict"] == "FAIL"

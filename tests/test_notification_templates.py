"""Every persistent notification type renders ru/kk text, never raw codes."""

from app.notifications import TEMPLATES, reason_text, verdict_name


def test_all_persistent_types_have_templates():
    from app.services import background

    notify_values = set(background.ACTION_NOTIFY.values())
    # Типы, которые реально уходят в notify(): все значения маппингов плюс
    # прямые ветки _notify_for_event.
    required = notify_values | {
        "AI_INSPECTION_READY",
        "DEADLINE_EXCEEDED",
        "DEADLINE_WARNING",
        "DEADLINE_30MIN",
        "LONG_OVERDUE_120MIN",
        "NOT_ACCEPTED_WITH_CANDIDATE",
        "WORK_ORDER_CLOSED",
        "WORK_ORDER_REJECTED",
        "WORK_ORDER_ASSIGNED",
        "WORK_ORDER_CREATED",
        "WORKER_STATUS_CHANGED",
    }
    missing = sorted(t for t in required if t not in TEMPLATES)
    assert not missing, f"no ru/kk template for: {missing}"


def test_templates_render_without_english_codes():
    data = {"number": 7, "verdict": "needs_master_review", "minutes_left": 12}
    for lang in ("ru", "kk"):
        for type_, builder in TEMPLATES.items():
            title, message = builder(dict(data), lang)
            assert title and message, type_
            for code in ("accepted", "needs_rework", "needs_master_review", "None"):
                assert code not in message or type_ == "WORK_ORDER_UPDATED", (type_, message)


def test_verdict_names_cover_all_known_verdicts():
    assert verdict_name("accepted", "ru") == "Принято"
    assert verdict_name("needs_rework", "kk") == "Пысықтауға"
    assert verdict_name("needs_master_review", "ru") == "Требуется проверка мастером"
    assert verdict_name("mystery", "ru") == "—"


def test_reason_text_prefers_resolved_names():
    d = {"reason": "duplicate", "reason_ru": "Дублирующий наряд", "reason_kk": "Қайталанатын наряд"}
    assert reason_text(d, "ru") == "Дублирующий наряд"
    assert reason_text(d, "kk") == "Қайталанатын наряд"
    assert reason_text({"reason": "x"}, "ru") == "x"
    assert reason_text({}, "ru") == "—"

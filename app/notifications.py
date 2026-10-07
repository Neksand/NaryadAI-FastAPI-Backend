"""Central NotificationService.

BUSINESS EVENT -> NotificationService.notify() -> persistent DB row
  -> WebSocket to the app. No external push services involved.
"""

import json
import logging

log = logging.getLogger("notifications")


def _t(ru: str, kk: str, lang: str) -> str:
    return kk if lang == "kk" else ru


TEMPLATES = {
    "WORK_ORDER_ASSIGNED": lambda d, lang: (
        _t("Новый наряд", "Жаңа наряд", lang),
        _t(f"Вам назначен наряд №{d['number']}.", f"Сізге №{d['number']} наряд тағайындалды.", lang)),
    "WORK_ORDER_ACCEPTED": lambda d, lang: (
        _t("Наряд принят", "Наряд қабылданды", lang),
        _t(f"Исполнитель принял наряд №{d['number']}.", f"Орындаушы №{d['number']} нарядты қабылдады.", lang)),
    "WORK_ORDER_REJECTED": lambda d, lang: (
        _t("Наряд отклонён", "Наряд қабылданбады", lang),
        _t(f"Наряд №{d['number']} отклонён: {d.get('reason') or '—'}.", f"№{d['number']} наряд қабылданбады: {d.get('reason') or '—'}.", lang)),
    "WORK_ORDER_STARTED": lambda d, lang: (
        _t("Работа начата", "Жұмыс басталды", lang),
        _t(f"Наряд №{d['number']} в работе.", f"№{d['number']} наряд орындалуда.", lang)),
    "WORK_ORDER_PAUSED": lambda d, lang: (
        _t("Работа приостановлена", "Жұмыс тоқтатылды", lang),
        _t(f"Наряд №{d['number']} на паузе: {d.get('reason') or '—'}.", f"№{d['number']} наряд кідіртілді: {d.get('reason') or '—'}.", lang)),
    "WORK_ORDER_COMPLETED": lambda d, lang: (
        _t("Работа выполнена", "Жұмыс орындалды", lang),
        _t(f"Наряд №{d['number']} отправлен на проверку ИИ.", f"№{d['number']} наряд ЖИ тексеруіне жіберілді.", lang)),
    "WORK_ORDER_CLOSED": lambda d, lang: (
        _t("Наряд закрыт", "Наряд жабылды", lang),
        _t(f"Наряд №{d['number']} закрыт с оценкой {d.get('score', '—')}.", f"№{d['number']} наряд {d.get('score', '—')} бағамен жабылды.", lang)),
    "WORK_ORDER_REWORK": lambda d, lang: (
        _t("Доработайте наряд", "Нарядты пысықтаңыз", lang),
        _t(f"Наряд №{d['number']} возвращён на доработку: {d.get('reason') or '—'}.", f"№{d['number']} наряд пысықтауға қайтарылды: {d.get('reason') or '—'}.", lang)),
    "AI_INSPECTION_READY": lambda d, lang: (
        _t("Проверка ИИ готова", "ЖИ тексеруі дайын", lang),
        _t(f"Наряд №{d['number']}: вердикт — {d.get('verdict', '—')}.", f"№{d['number']} наряд: қорытынды — {d.get('verdict', '—')}.", lang)),
    "DEADLINE_WARNING": lambda d, lang: (
        _t("Срок подходит", "Мерзімі жақындады", lang),
        _t(f"Наряд №{d['number']}: до срока осталось {d.get('minutes_left', '—')} мин.", f"№{d['number']} наряд: мерзімге {d.get('minutes_left', '—')} мин қалды.", lang)),
    "DEADLINE_EXCEEDED": lambda d, lang: (
        _t("Наряд просрочен", "Наряд мерзімінен кешікті", lang),
        _t(f"Наряд №{d['number']} просрочен на {d.get('minutes_overdue', '—')} мин.", f"№{d['number']} наряд {d.get('minutes_overdue', '—')} мин кешікті.", lang)),
}


async def notify(conn, recipient_ids: list[str], type_: str, data: dict,
                 related_entity_type: str | None = None, related_entity_id: str | None = None) -> list[dict]:
    """Persist one notification row per recipient in the recipient's language.

    Must be called INSIDE the business transaction (rows commit with it).
    Returns the created rows for WS fan-out.
    """
    rows = []
    for rid in dict.fromkeys(recipient_ids):
        lang = await conn.fetchval("SELECT lang FROM employees WHERE id=$1::uuid", rid) or "ru"
        if lang not in ("ru", "kk"):
            lang = "ru"
        builder = TEMPLATES.get(type_)
        title, message = builder(data, lang) if builder else (type_, json.dumps(data, default=str)[:300])
        row = await conn.fetchrow(
            """INSERT INTO notifications(recipient_id, type, title, message, lang, related_entity_type, related_entity_id, metadata)
               VALUES ($1::uuid,$2,$3,$4,$5,$6,$7::uuid,$8::jsonb)
               RETURNING id, recipient_id, type, title, message, lang, is_read, created_at""",
            rid, type_, title, message, lang, related_entity_type, related_entity_id, json.dumps(data, default=str))
        rows.append({"id": str(row["id"]), "recipient_id": str(row["recipient_id"]), "type": row["type"],
                     "title": row["title"], "message": row["message"], "lang": row["lang"]})
    return rows

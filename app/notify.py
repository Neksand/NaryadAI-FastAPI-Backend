"""Push notifications. Telegram bot is the demo transport; FCM tokens are
registered via /devices for the future mobile app (provider hook below)."""
import logging

import httpx

log = logging.getLogger("notify")


async def send_telegram(text: str, chat_id: str | None = None) -> bool:
    from app.config import settings

    if not settings.TELEGRAM_BOT_TOKEN:
        return False
    chat = chat_id or settings.TELEGRAM_DEFAULT_CHAT_ID
    if not chat:
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat, "text": text, "parse_mode": "HTML"},
            )
            return r.status_code == 200
    except Exception as e:  # noqa: BLE001 - notify must never break the request
        log.warning("Telegram send failed: %s", e)
        return False


def format_overdue(number: int, equipment: str, area: str, assignee: str, status: str, minutes: int, comment: str | None) -> str:
    return (f"⚠️ <b>Наряд №{number} просрочен на {minutes} мин.</b>\n"
            f"{equipment}, участок: {area}.\nИсполнитель: {assignee}. Статус: {status}."
            + (f"\nПоследний комментарий: «{comment}»" if comment else ""))


def format_new_order(number: int, equipment: str, priority: str, due: str) -> str:
    alarm = "🔴 <b>АВАРИЙНЫЙ — срочно в работу!</b>\n" if priority == "critical" else "🆕 Новый наряд\n"
    return f"{alarm}№{number}, {equipment}. Срок: {due}."


async def send_fcm(token: str, title: str, body: str) -> bool:
    """FCM provider hook: wire firebase-admin here when the mobile app lands."""
    log.info("FCM stub -> %s...: %s", token[:12], title)
    return False

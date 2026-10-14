"""Worker recommendation orchestration (master decides, AI only suggests)."""

from typing import Any

from app.ai import gateway as _gw

_STATUS_TEXT: dict[str, dict[str, str]] = {
    "free": {"ru": "свободен сейчас", "kk": "қазір бос"},
    "queue": {"ru": "есть очередь, но с меньшей загрузкой", "kk": "кезегі бар, жүктемесі азырақ"},
    "busy": {"ru": "занят текущим нарядом", "kk": "ағымдағы нарядпен бос емес"},
    "off": {"ru": "не на смене", "kk": "ауысымда емес"},
}
_LOAD_TEXT: dict[str, dict[str, str]] = {
    "low": {"ru": "низкая текущая загрузка", "kk": "ағымдағы жүктемесі төмен"},
    "high": {"ru": "высокая загрузка: {n} в очереди", "kk": "жоғары жүктеме: {n} кезекте"},
}


def _pick(table: dict[str, dict[str, str]], key: str, language: str) -> str:
    entry = table[key]
    return entry.get(language, entry["ru"])


def explain_candidate(current_status: str, queue_count: int, language: str = "ru") -> list[str]:
    """Human-readable reasons behind an assignee suggestion (explainability).

    Deterministic and language-aware; used by the master to see *why* a worker
    was suggested. Advice only — the master makes the decision.
    """
    status = current_status if current_status in _STATUS_TEXT else "off"
    reasons = [_pick(_STATUS_TEXT, status, language)]
    if queue_count <= 1:
        reasons.append(_pick(_LOAD_TEXT, "low", language))
    elif queue_count >= 4:
        reasons.append(_pick(_LOAD_TEXT, "high", language).format(n=queue_count))
    return reasons


async def recommend(ctx: dict[str, Any], language: str = "ru") -> dict[str, Any]:
    return await _gw.recommend_worker(ctx, language)

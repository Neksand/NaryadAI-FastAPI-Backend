"""Report/explanation texts via gateway (localized)."""

from typing import Any


async def summarize_shift(counters: dict[str, Any], language: str = "ru") -> str:
    if language == "kk":
        return (
            f"Ауысым: берілгені {counters.get('issued', 0)}, орындалғаны {counters.get('done', 0)}, "
            f"кешіккені {counters.get('overdue', 0)}."
        )
    return (
        f"Смена: выдано {counters.get('issued', 0)}, выполнено {counters.get('done', 0)}, "
        f"просрочено {counters.get('overdue', 0)}."
    )

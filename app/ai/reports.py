"""Report/explanation texts via gateway (localized)."""
from app.ai import gateway as _gw


async def summarize_shift(counters: dict, language: str = "ru") -> str:
    if language == "kk":
        return (f"Ауысым: берілгені {counters.get('issued', 0)}, орындалғаны {counters.get('done', 0)}, "
                f"кешіккені {counters.get('overdue', 0)}.")
    return (f"Смена: выдано {counters.get('issued', 0)}, выполнено {counters.get('done', 0)}, "
            f"просрочено {counters.get('overdue', 0)}.")

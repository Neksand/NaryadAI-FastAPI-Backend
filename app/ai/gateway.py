"""AI gateway: mock by default (AI_MODE=mock), external providers when keys exist.

Business layer calls these functions. External failures always fall back to
mock/local — the work-order lifecycle never breaks because of AI.
"""
from app.ai.mock import MockAIProvider

_mock = MockAIProvider()


def _mode() -> str:
    from app.config import settings
    return (settings.AI_MODE or "mock").lower()


async def recommend_worker(ctx: dict, language: str = "ru") -> dict:
    if _mode() == "mock":
        return await _mock.recommend_worker(ctx, language)
    try:
        from app.ai_providers import chat_text
        out, provider = await chat_text(
            "Ты диспетчер ремонтов. Ответь строго JSON: {\"index\": 0, \"confidence\": 0.0-1.0, \"reason\": \"...\"}.",
            f"Кандидаты: {ctx.get('candidates', [])}. Выбери лучшего (индекс).",
        )
        if out:
            import json
            s, e = out.index("{"), out.rindex("}") + 1
            d = json.loads(out[s:e])
            idx = int(d.get("index", 0))
            cands = ctx.get("candidates", [])
            if 0 <= idx < len(cands):
                return {"recommended_worker_id": cands[idx].get("employee_id"), "confidence": float(d.get("confidence", 0.7)),
                        "reason": str(d.get("reason", ""))[:300], "alternatives": [], "provider": provider}
    except Exception:
        pass
    return await _mock.recommend_worker(ctx, language)


async def inspect_completion(ctx: dict, language: str = "ru") -> dict:
    # Deterministic rules first (mock), external vision/text layered in ai_review.
    return await _mock.inspect_completion(ctx, language)


async def explain_anomaly(ctx: dict, language: str = "ru") -> str:
    if _mode() != "mock":
        try:
            from app.ai_providers import chat_text
            out, _ = await chat_text(
                "Объясни производственную аномалию простым языком + рекомендация. 2-3 предложения.",
                f"Заголовок: {ctx.get('headline')}. Данные: {ctx.get('evidence')}. Язык: {'казахский' if language == 'kk' else 'русский'}.",
            )
            if out:
                return out[:600]
        except Exception:
            pass
    return await _mock.explain_anomaly(ctx, language)

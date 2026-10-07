"""Worker recommendation orchestration (master decides, AI only suggests)."""
from app.ai import gateway as _gw


async def recommend(ctx: dict, language: str = "ru") -> dict:
    return await _gw.recommend_worker(ctx, language)

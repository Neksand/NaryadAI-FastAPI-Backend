"""Completion inspection orchestration: deterministic rules + gateway."""
from app.ai import gateway as _gw


async def inspect(ctx: dict, language: str = "ru") -> dict:
    return await _gw.inspect_completion(ctx, language)

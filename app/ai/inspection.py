"""Completion inspection orchestration: deterministic rules + gateway."""

from typing import Any

from app.ai import gateway as _gw


async def inspect(ctx: dict[str, Any], language: str = "ru") -> dict[str, Any]:
    return await _gw.inspect_completion(ctx, language)

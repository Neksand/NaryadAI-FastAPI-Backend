"""Vision helpers: local dHash/EXIF live in routers/photos.py.
This module is the external-vision entrypoint (Gemini) used by inspection."""
from app.ai_providers import compare_photos as _compare


async def compare_before_after(before: bytes, after: bytes, context: str):
    return await _compare(before, after, context)

"""External AI providers. All optional — local rules-v1 works without keys.

Gemini goes through the official `google-genai` SDK (supports current
auth keys); OpenRouter/Groq/Pollinations use OpenAI-compatible REST.
Only anonymized work texts and photo bytes leave the контур — never names,
PINs or tokens. Every call is short-timeout and returns None on failure.
"""

import asyncio
import base64
import logging

import httpx

log = logging.getLogger("ai_providers")

TEXT_TIMEOUT = 20.0


def _gemini_client():
    from app.config import settings

    if not settings.GEMINI_API_KEY:
        return None, None
    return settings.GEMINI_API_KEY, settings.GEMINI_MODEL


def _settings():
    from app.config import settings

    return settings


async def _post_json(url: str, headers: dict, payload: dict, timeout: float = TEXT_TIMEOUT) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, headers=headers, json=payload)
            if r.status_code != 200:
                log.warning("AI provider %s -> HTTP %s", url, r.status_code)
                return None
            return r.json()
    except Exception as e:  # noqa: BLE001 - provider failures always fall back
        log.warning("AI provider %s failed: %s", url, e)
        return None


def _openai_text(data: dict) -> str | None:
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, AttributeError):
        return None


async def _gemini_text(system: str, user: str) -> str | None:
    key, model = _gemini_client()
    if not key:
        return None
    data = await _post_json(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        {"x-goog-api-key": key, "Content-Type": "application/json"},
        {"system_instruction": {"parts": [{"text": system}]},
         "contents": [{"parts": [{"text": user}]}],
         "generationConfig": {"temperature": 0.2, "maxOutputTokens": 512}},
    )
    if not data:
        return None
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        return None


async def _openai_compat_text(base_url: str, api_key: str, model: str, system: str, user: str) -> str | None:
    if not api_key:
        return None
    data = await _post_json(
        f"{base_url}/chat/completions",
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        {"model": model, "temperature": 0.2, "max_tokens": 512,
         "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
    )
    return _openai_text(data) if data else None


async def _pollinations_text(system: str, user: str) -> str | None:
    data = await _post_json(
        "https://text.pollinations.ai/openai",
        {"Content-Type": "application/json"},
        {"model": "openai", "temperature": 0.2, "max_tokens": 512,
         "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
    )
    return _openai_text(data) if data else None


async def chat_text(system: str, user: str) -> tuple[str | None, str]:
    """Return (text, provider). All configured providers race; first success wins.

    Provider is 'local' when nothing is configured or all fail.
    """
    s = _settings()
    order = [p.strip() for p in s.AI_TEXT_PROVIDER.split(",") if p.strip()] or ["auto"]
    if order == ["auto"]:
        order = ["gemini", "openrouter", "groq", "pollinations"]
    order = [p for p in order if p != "off"]
    if not order:
        return None, "local"

    async def one(name: str) -> str | None:
        if name == "gemini":
            return await _gemini_text(system, user)
        if name == "openrouter":
            return await _openai_compat_text("https://openrouter.ai/api/v1", s.OPENROUTER_API_KEY,
                                             s.OPENROUTER_MODEL, system, user)
        if name == "groq":
            return await _openai_compat_text("https://api.groq.com/openai/v1", s.GROQ_API_KEY,
                                             s.GROQ_MODEL, system, user)
        if name == "pollinations":
            return await _pollinations_text(system, user)
        return None

    tasks = {asyncio.create_task(one(n)): n for n in order}
    try:
        while tasks:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                name = tasks.pop(t)
                try:
                    out = t.result()
                except Exception:
                    continue
                if out:
                    for p in tasks:
                        p.cancel()
                    return out, name
        return None, "local"
    finally:
        for t in tasks:
            t.cancel()


async def compare_photos(before: bytes, after: bytes, context: str) -> tuple[str | None, str]:
    """Multimodal before/after comparison via Gemini REST. Returns (verdict_text, provider)."""
    from app.config import settings

    if not settings.GEMINI_API_KEY or settings.AI_VISION_PROVIDER == "off":
        return None, "local"
    key, model = _gemini_client()
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    data = await _post_json(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        {"x-goog-api-key": key, "Content-Type": "application/json"},
        {"system_instruction": {"parts": [{"text": (
            "Ты контролёр ремонта. Сравни фото ДО и ПОСЛЕ. Ответь строго JSON: "
            '{"fixed": true|false, "score": 1-5, "note": "коротко по-русски"}. '
            "Никаких персональных данных в ответе.")}]},
         "contents": [{"parts": [
             {"text": f"Контекст наряда (обезличен): {context}. Первое фото — ДО, второе — ПОСЛЕ."},
             {"inline_data": {"mime_type": "image/jpeg", "data": b64(before)}},
             {"inline_data": {"mime_type": "image/jpeg", "data": b64(after)}},
         ]}],
         "generationConfig": {"temperature": 0.2, "maxOutputTokens": 256}},
        timeout=30.0,
    )
    if not data:
        return None, "local"
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return text.strip(), "gemini-vision"
    except (KeyError, IndexError):
        return None, "local"


async def transcribe_audio(data: bytes, filename: str = "voice.ogg") -> tuple[str | None, str]:
    """Voice note transcription via Groq Whisper free tier. Returns (text, provider)."""
    s = _settings()
    if not s.GROQ_API_KEY:
        return None, "local"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {s.GROQ_API_KEY}"},
                data={"model": s.GROQ_WHISPER_MODEL, "language": "ru"},
                files={"file": (filename, data)},
            )
            if r.status_code != 200:
                log.warning("Whisper -> HTTP %s", r.status_code)
                return None, "local"
            return r.json().get("text", "").strip() or None, "groq-whisper"
    except Exception as e:  # noqa: BLE001
        log.warning("Whisper failed: %s", e)
        return None, "local"

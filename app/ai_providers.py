"""External AI providers. All optional — local rules-v1 works without keys.

Free tiers suitable for the hackathon demo (put keys into `.env`, never commit):
  1. GEMINI_API_KEY — Google AI Studio (https://aistudio.google.com), generous free
     quota, text + vision. Primary recommendation: one key covers text checks,
     photo before/after comparison and summaries.
  2. OPENROUTER_API_KEY — https://openrouter.ai, models with `:free` suffix,
     OpenAI-compatible endpoint, no card required.
  3. GROQ_API_KEY — https://console.groq.com, free rate-limited tier for text
     and Whisper transcription (voice notes).
  4. Pollinations — https://pollinations.ai, no key, free community endpoints
     (fallback only, no SLA).

Personal data rule: prompts sent outside contain only work texts, fault codes and
photo bytes — never employee names, PINs or tokens (see ANONYMIZE in prompts).
Every call has a short timeout and returns None on any failure; callers must
fall back to local heuristics.
"""

import base64
import logging

import httpx

log = logging.getLogger("ai_providers")

TEXT_TIMEOUT = 20.0


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
    s = _settings()
    if not s.GEMINI_API_KEY:
        return None
    data = await _post_json(
        f"https://generativelanguage.googleapis.com/v1beta/models/{s.GEMINI_MODEL}:generateContent",
        {"x-goog-api-key": s.GEMINI_API_KEY, "Content-Type": "application/json"},
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
    """Return (text, provider). Provider is 'local' when nothing is configured."""
    s = _settings()
    order = [p.strip() for p in s.AI_TEXT_PROVIDER.split(",") if p.strip()] or ["auto"]
    if order == ["auto"]:
        order = ["gemini", "openrouter", "groq", "pollinations"]
    for name in order:
        if name == "off":
            return None, "local"
        if name == "gemini":
            out = await _gemini_text(system, user)
        elif name == "openrouter":
            out = await _openai_compat_text("https://openrouter.ai/api/v1", s.OPENROUTER_API_KEY,
                                            s.OPENROUTER_MODEL, system, user)
        elif name == "groq":
            out = await _openai_compat_text("https://api.groq.com/openai/v1", s.GROQ_API_KEY,
                                            s.GROQ_MODEL, system, user)
        elif name == "pollinations":
            out = await _pollinations_text(system, user)
        else:
            continue
        if out:
            return out, name
    return None, "local"


async def compare_photos(before: bytes, after: bytes, context: str) -> tuple[str | None, str]:
    """Multimodal before/after comparison. Returns (verdict_text, provider)."""
    s = _settings()
    if not s.GEMINI_API_KEY or s.AI_VISION_PROVIDER == "off":
        return None, "local"
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    data = await _post_json(
        f"https://generativelanguage.googleapis.com/v1beta/models/{s.GEMINI_MODEL}:generateContent",
        {"x-goog-api-key": s.GEMINI_API_KEY, "Content-Type": "application/json"},
        {"system_instruction": {"parts": [{"text": (
            "Ты контролёр ремонта. Сравни фото ДО и ПОСЛЕ. Ответь строго JSON: "
            '{"fixed": true|false, "score": 1-5, "note": "коротко по-русски"}. '
            "Никаких персональных данных в ответе." )}]},
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

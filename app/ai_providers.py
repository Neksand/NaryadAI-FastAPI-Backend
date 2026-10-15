"""External AI providers. All optional — local rules-v1 works without keys.

Gemini goes through the official `google-genai` SDK (supports current
auth keys); OpenRouter/Groq/Pollinations use OpenAI-compatible REST.
Hosted text, photo and audio calls require AI_EXTERNAL_ENABLED. Free text and
pixels are not automatically anonymized; operators must approve and de-identify
data before opting in. Every call is short-timeout and returns None on failure.
"""

import asyncio
import base64
import io
import logging
import threading
from typing import Any, cast

import httpx

log = logging.getLogger("ai_providers")

TEXT_TIMEOUT = 20.0


def _gemini_client() -> tuple[str | None, str | None]:
    from app.config import settings

    if not settings.GEMINI_API_KEY:
        return None, None
    return settings.GEMINI_API_KEY, settings.GEMINI_MODEL


def _settings() -> Any:
    from app.config import settings

    return settings


async def _post_json(
    url: str, headers: dict[str, Any], payload: dict[str, Any], timeout: float = TEXT_TIMEOUT
) -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, headers=headers, json=payload)
            if r.status_code != 200:
                log.warning("AI provider %s -> HTTP %s", url, r.status_code)
                return None
            return cast(dict[str, Any] | None, r.json())
    except Exception as e:
        log.warning("AI provider %s failed: %s", url, e)
        return None


def _openai_text(data: dict[str, Any]) -> str | None:
    try:
        return cast(str, data["choices"][0]["message"]["content"].strip())
    except (KeyError, IndexError, AttributeError):
        return None


async def _gemini_text(system: str, user: str) -> str | None:
    key, model = _gemini_client()
    if not key:
        return None
    data = await _post_json(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        {"x-goog-api-key": key, "Content-Type": "application/json"},
        {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 512},
        },
    )
    if not data:
        return None
    try:
        return cast(str, data["candidates"][0]["content"]["parts"][0]["text"].strip())
    except (KeyError, IndexError):
        return None


async def _openai_compat_text(
    base_url: str, api_key: str, model: str, system: str, user: str
) -> str | None:
    if not api_key:
        return None
    data = await _post_json(
        f"{base_url}/chat/completions",
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        {
            "model": model,
            "temperature": 0.2,
            "max_tokens": 512,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        },
    )
    return _openai_text(data) if data else None


async def _pollinations_text(system: str, user: str) -> str | None:
    data = await _post_json(
        "https://text.pollinations.ai/openai",
        {"Content-Type": "application/json"},
        {
            "model": "openai",
            "temperature": 0.2,
            "max_tokens": 512,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        },
    )
    return _openai_text(data) if data else None


async def _estimin3n_text(system: str, user: str) -> str | None:
    if not _settings().ESTIMIN3N_ENABLED:
        return None
    messages = [
        {"role": "system", "content": [{"type": "text", "text": system}]},
        {"role": "user", "content": [{"type": "text", "text": user}]},
    ]
    try:
        return await asyncio.to_thread(_estimin3n_generate, messages, 512)
    except Exception as e:
        log.warning("Estimin3n text inference failed: %s", e)
        return None


async def chat_text(system: str, user: str) -> tuple[str | None, str]:
    """Return (text, provider). All configured providers race; first success wins.

    Provider is 'local' when nothing is configured or all fail.
    """
    s = _settings()
    order = [p.strip() for p in s.AI_TEXT_PROVIDER.split(",") if p.strip()] or ["auto"]
    if order == ["auto"]:
        order = ["gemini", "openrouter", "groq"]
    order = [p for p in order if p != "off"]
    if not getattr(s, "AI_EXTERNAL_ENABLED", False):
        order = [p for p in order if p == "estimin3n"]
    if not order:
        return None, "local"

    async def one(name: str) -> str | None:
        if name == "gemini":
            return await _gemini_text(system, user)
        if name == "openrouter":
            return await _openai_compat_text(
                "https://openrouter.ai/api/v1",
                s.OPENROUTER_API_KEY,
                s.OPENROUTER_MODEL,
                system,
                user,
            )
        if name == "groq":
            return await _openai_compat_text(
                "https://api.groq.com/openai/v1", s.GROQ_API_KEY, s.GROQ_MODEL, system, user
            )
        if name == "pollinations":
            return await _pollinations_text(system, user)
        if name == "estimin3n":
            return await _estimin3n_text(system, user)
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
    """Compare photos using the selected local or hosted vision model."""
    from app.config import settings

    provider = settings.AI_VISION_PROVIDER.lower()
    if provider == "off":
        return None, "local"
    if provider == "estimin3n":
        if not settings.ESTIMIN3N_ENABLED:
            return None, "estimin3n"
        try:
            text = await asyncio.to_thread(_estimin3n_compare_photos, before, after, context)
            return text or None, "estimin3n"
        except Exception as e:
            log.warning("Estimin3n photo comparison failed: %s", e)
            return None, "estimin3n"
    if provider not in ("auto", "gemini") or not settings.GEMINI_API_KEY:
        return None, "local"
    if not settings.AI_EXTERNAL_ENABLED:
        return None, "local"
    key, model = _gemini_client()

    def b64(b: bytes) -> str:
        return base64.b64encode(b).decode()

    data = await _post_json(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        {"x-goog-api-key": key, "Content-Type": "application/json"},
        {
            "system_instruction": {
                "parts": [
                    {
                        "text": (
                            "Ты контролёр ремонта. Сравни фото ДО и ПОСЛЕ. Ответь строго JSON: "
                            '{"fixed": true|false, "score": 1-5, "confidence": 0.0-1.0, "note": "коротко по-русски"}. '
                            "Никаких персональных данных в ответе."
                        )
                    }
                ]
            },
            "contents": [
                {
                    "parts": [
                        {
                            "text": f"Контекст наряда (обезличен): {context}. Первое фото — ДО, второе — ПОСЛЕ."
                        },
                        {"inline_data": {"mime_type": "image/jpeg", "data": b64(before)}},
                        {"inline_data": {"mime_type": "image/jpeg", "data": b64(after)}},
                    ]
                }
            ],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 256},
        },
        timeout=30.0,
    )
    if not data:
        return None, "local"
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return text.strip(), "gemini-vision"
    except (KeyError, IndexError):
        return None, "local"


_ESTIMIN3N_LOCK = threading.Lock()
_ESTIMIN3N_RUNTIME: tuple[object, object, str, object] | None = None


def _estimin3n_local_snapshot(settings: Any) -> Any:
    """Resolve the cached snapshot path without making Hub metadata requests."""
    import json
    from pathlib import Path

    repo_cache = Path(settings.ESTIMIN3N_CACHE_DIR).expanduser() / (
        "models--" + settings.ESTIMIN3N_MODEL_ID.replace("/", "--")
    )
    ref_file = repo_cache / "refs" / "main"
    if not ref_file.is_file():
        raise FileNotFoundError(
            f"Estimin3n is not cached under {settings.ESTIMIN3N_CACHE_DIR}; "
            "run snapshot_download before starting local inference"
        )
    snapshot = repo_cache / "snapshots" / ref_file.read_text().strip()
    index_file = snapshot / "model.safetensors.index.json"
    if not index_file.is_file():
        raise FileNotFoundError(f"Estimin3n snapshot metadata is missing: {snapshot}")
    weight_files = set(json.loads(index_file.read_text()).get("weight_map", {}).values())
    missing = sorted(name for name in weight_files if not (snapshot / name).is_file())
    if missing:
        raise FileNotFoundError(
            f"Estimin3n weights are incomplete in {snapshot} ({len(missing)} shards missing)"
        )
    return snapshot


def _estimin3n_runtime() -> Any:
    """Load Estimin3n once; keep its weights in the configured local HF cache."""
    global _ESTIMIN3N_RUNTIME
    import torch
    from transformers import AutoProcessor, Gemma3nForConditionalGeneration

    from app.config import settings

    if _ESTIMIN3N_RUNTIME is None:
        model_path = _estimin3n_local_snapshot(settings)
        requested_device = settings.ESTIMIN3N_DEVICE.lower()
        if requested_device == "auto":
            if torch.cuda.is_available():
                device = "cuda"
            elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                device = "mps"
            else:
                device = "cpu"
        else:
            device = requested_device

        # The downloaded checkpoint is BF16. Keeping that dtype on CPU avoids
        # doubling its ~16 GB weight footprint to FP32 on machines without MPS.
        dtype = torch.float16 if device == "mps" else torch.bfloat16
        load_options = {
            "trust_remote_code": True,
            "torch_dtype": dtype,
            "low_cpu_mem_usage": True,
            "cache_dir": settings.ESTIMIN3N_CACHE_DIR,
            "local_files_only": True,
        }
        if device == "cuda":
            load_options["device_map"] = "auto"
            if settings.ESTIMIN3N_LOAD_IN_4BIT:
                load_options["load_in_4bit"] = True
        elif settings.ESTIMIN3N_LOAD_IN_4BIT:
            raise ValueError(
                "ESTIMIN3N_LOAD_IN_4BIT requires CUDA; bitsandbytes is not used on MPS/CPU"
            )
        model = Gemma3nForConditionalGeneration.from_pretrained(str(model_path), **load_options)
        if device != "cuda":
            model.to(device)
        processor = AutoProcessor.from_pretrained(  # type: ignore[no-untyped-call]
            str(model_path),
            trust_remote_code=True,
            local_files_only=True,
        )
        model.eval()
        _ESTIMIN3N_RUNTIME = (model, processor, device, dtype)
    return _ESTIMIN3N_RUNTIME


def _estimin3n_generate(messages: list[dict[str, Any]], max_new_tokens: int = 256) -> str:
    """Run serialized local inference to avoid concurrent model memory spikes."""
    import torch

    with _ESTIMIN3N_LOCK:
        model, processor, device, dtype = _estimin3n_runtime()
        inputs = processor.apply_chat_template(
            messages,
            add_generation_prompt=True,
            tokenize=True,
            return_dict=True,
            return_tensors="pt",
        ).to(device)
        for name, tensor in inputs.items():
            # Keep tokenizer masks in their original integer/bool dtypes.
            # Gemma 3n's input_features_mask is boolean and becomes invalid
            # for masked_fill if it is coerced to long.
            if hasattr(tensor, "is_floating_point") and tensor.is_floating_point():
                inputs[name] = tensor.to(dtype)
        with torch.inference_mode():
            generated = model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                do_sample=False,
                pad_token_id=processor.tokenizer.eos_token_id,
                eos_token_id=processor.tokenizer.eos_token_id,
            )
        generated = generated[0][inputs["input_ids"].shape[1] :]
        return cast(str, processor.tokenizer.decode(generated, skip_special_tokens=True).strip())


def _estimin3n_transcribe(data: bytes, language: str = "ru") -> str:
    """Transcribe an audio clip locally in the requested language."""
    import librosa
    import numpy as np
    import soundfile as sf

    audio, sample_rate = sf.read(io.BytesIO(data), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sample_rate != 16_000:
        audio = librosa.resample(audio, orig_sr=sample_rate, target_sr=16_000)
    audio = np.asarray(audio, dtype=np.float32)
    language = language.lower().strip()
    language_name = "Kazakh" if language == "kk" else "Russian"
    instruction = (
        f"You are an expert assistant that accurately transcribes {language_name} speech. "
        f"Output clean {language_name} text only. Do not translate, summarize, or add words."
    )
    if language == "kk":
        instruction += " Preserve Kazakh Cyrillic letters ә, ғ, қ, ң, ө, ұ, ү, һ, і accurately."
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": [{"type": "text", "text": instruction}]},
        {
            "role": "user",
            "content": [
                {"type": "audio", "audio": audio},
                {"type": "text", "text": "Transcribe this audio exactly."},
            ],
        },
    ]
    return _estimin3n_generate(messages, 256)


def _estimin3n_compare_photos(before: bytes, after: bytes, context: str) -> str:
    """Review before/after photos using the model's image and text inputs."""
    from PIL import Image

    before_image = Image.open(io.BytesIO(before)).convert("RGB")
    after_image = Image.open(io.BytesIO(after)).convert("RGB")
    prompt = (
        f"Контекст наряда (обезличен): {context}. Первое фото — ДО, второе — ПОСЛЕ. "
        "Сравни состояние оборудования и результат ремонта. Ответь строго JSON: "
        '{"fixed": true|false, "score": 1-5, "confidence": 0.0-1.0, "note": "коротко по-русски"}. '
        "Не выдумывай невидимые детали и не включай персональные данные."
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": [{"type": "text", "text": "Ты контролёр качества ремонта."}]},
        {
            "role": "user",
            "content": [
                {"type": "image", "image": before_image},
                {"type": "image", "image": after_image},
                {"type": "text", "text": prompt},
            ],
        },
    ]
    return _estimin3n_generate(messages, 192)


async def transcribe_audio(
    data: bytes, filename: str = "voice.ogg", language: str = "ru"
) -> tuple[str | None, str]:
    """Transcribe via configured provider, optionally using Estimin3n locally."""
    s = _settings()
    provider = s.AI_AUDIO_PROVIDER.lower()

    if provider not in ("auto", "groq", "estimin3n", "off"):
        log.warning("Unknown AI_AUDIO_PROVIDER=%s", provider)
        return None, "local"
    if provider == "off":
        return None, "local"

    if provider in ("auto", "groq") and s.GROQ_API_KEY and getattr(s, "AI_EXTERNAL_ENABLED", False):
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {s.GROQ_API_KEY}"},
                    data={"model": s.GROQ_WHISPER_MODEL, "language": language},
                    files={"file": (filename, data)},
                )
                if r.status_code == 200:
                    text = r.json().get("text", "").strip()
                    if text:
                        return text, "groq-whisper"
                else:
                    log.warning("Whisper -> HTTP %s", r.status_code)
        except Exception as e:
            log.warning("Whisper failed: %s", e)
        if provider == "groq":
            return None, "groq-whisper"
    elif provider == "groq":
        return None, "groq-whisper"

    if provider == "estimin3n" or (provider == "auto" and s.ESTIMIN3N_ENABLED):
        try:
            text = await asyncio.to_thread(_estimin3n_transcribe, data, language)
            return text or None, "estimin3n"
        except Exception as e:
            log.warning("Estimin3n transcription failed: %s", e)
            return None, "estimin3n"
    return None, "local"

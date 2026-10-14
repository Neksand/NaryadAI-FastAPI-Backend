"""Provider-routing tests for voice transcription; no network or model weights."""

import asyncio
from types import SimpleNamespace

import pytest

from app import ai_providers


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeAsyncClient:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.request = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def post(self, url, **kwargs):
        self.request = (url, kwargs)
        return self.response


def _settings(**overrides):
    defaults = {
        "AI_AUDIO_PROVIDER": "off",
        "GROQ_API_KEY": "test-key",
        "GROQ_WHISPER_MODEL": "whisper-test",
        "ESTIMIN3N_ENABLED": False,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_off_provider_does_not_call_any_transcriber(monkeypatch):
    monkeypatch.setattr(ai_providers, "_settings", lambda: _settings(AI_AUDIO_PROVIDER="off"))
    monkeypatch.setattr(
        ai_providers, "_estimin3n_transcribe", lambda *_: pytest.fail("model must stay idle")
    )
    monkeypatch.setattr(
        ai_providers.httpx, "AsyncClient", lambda **_: pytest.fail("network must stay idle")
    )

    assert asyncio.run(ai_providers.transcribe_audio(b"wav")) == (None, "local")


@pytest.mark.parametrize("language", ["ru", "kk"])
def test_local_provider_passes_selected_language_and_reports_provider(monkeypatch, language):
    received = []
    monkeypatch.setattr(
        ai_providers,
        "_settings",
        lambda: _settings(AI_AUDIO_PROVIDER="estimin3n", ESTIMIN3N_ENABLED=True),
    )
    monkeypatch.setattr(
        ai_providers,
        "_estimin3n_transcribe",
        lambda data, requested_language: received.append((data, requested_language)) or "сөздер",
    )

    result = asyncio.run(ai_providers.transcribe_audio(b"pcm", "note.wav", language))

    assert result == ("сөздер", "estimin3n")
    assert received == [(b"pcm", language)]


def test_explicit_local_provider_does_not_leak_audio_to_groq(monkeypatch):
    monkeypatch.setattr(
        ai_providers,
        "_settings",
        lambda: _settings(AI_AUDIO_PROVIDER="estimin3n", ESTIMIN3N_ENABLED=False),
    )
    monkeypatch.setattr(
        ai_providers.httpx, "AsyncClient", lambda **_: pytest.fail("unexpected remote call")
    )

    assert asyncio.run(ai_providers.transcribe_audio(b"pcm", language="kk")) == (None, "estimin3n")


def test_groq_receives_exact_filename_audio_and_kazakh_language(monkeypatch):
    client = FakeAsyncClient(FakeResponse(200, {"text": "Құбыр тексерілді"}))
    monkeypatch.setattr(ai_providers, "_settings", lambda: _settings(AI_AUDIO_PROVIDER="groq"))
    monkeypatch.setattr(ai_providers.httpx, "AsyncClient", lambda **kwargs: client)

    result = asyncio.run(ai_providers.transcribe_audio(b"wav-bytes", "voice.wav", "kk"))

    assert result == ("Құбыр тексерілді", "groq-whisper")
    url, request = client.request
    assert url == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert request["headers"] == {"Authorization": "Bearer test-key"}
    assert request["data"] == {"model": "whisper-test", "language": "kk"}
    assert request["files"]["file"] == ("voice.wav", b"wav-bytes")


@pytest.mark.parametrize("payload", [{}, {"text": "   "}])
def test_groq_empty_transcript_is_pending_not_success(monkeypatch, payload):
    client = FakeAsyncClient(FakeResponse(200, payload))
    monkeypatch.setattr(ai_providers, "_settings", lambda: _settings(AI_AUDIO_PROVIDER="groq"))
    monkeypatch.setattr(ai_providers.httpx, "AsyncClient", lambda **kwargs: client)

    assert asyncio.run(ai_providers.transcribe_audio(b"wav")) == (None, "groq-whisper")


def test_groq_http_failure_is_graceful_and_keeps_provider(monkeypatch):
    client = FakeAsyncClient(FakeResponse(503))
    monkeypatch.setattr(ai_providers, "_settings", lambda: _settings(AI_AUDIO_PROVIDER="groq"))
    monkeypatch.setattr(ai_providers.httpx, "AsyncClient", lambda **kwargs: client)

    assert asyncio.run(ai_providers.transcribe_audio(b"wav")) == (None, "groq-whisper")


def test_auto_falls_back_from_groq_to_local_kazakh_transcription(monkeypatch):
    client = FakeAsyncClient(FakeResponse(502))
    received = []
    monkeypatch.setattr(
        ai_providers,
        "_settings",
        lambda: _settings(AI_AUDIO_PROVIDER="auto", ESTIMIN3N_ENABLED=True),
    )
    monkeypatch.setattr(ai_providers.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(
        ai_providers,
        "_estimin3n_transcribe",
        lambda data, language: received.append((data, language)) or "мойынтірек ауыстырылды",
    )

    result = asyncio.run(ai_providers.transcribe_audio(b"wav", "voice.wav", "kk"))

    assert result == ("мойынтірек ауыстырылды", "estimin3n")
    assert received == [(b"wav", "kk")]


def test_auto_without_configured_provider_is_offline_and_predictable(monkeypatch):
    monkeypatch.setattr(
        ai_providers,
        "_settings",
        lambda: _settings(AI_AUDIO_PROVIDER="auto", GROQ_API_KEY="", ESTIMIN3N_ENABLED=False),
    )
    monkeypatch.setattr(ai_providers.httpx, "AsyncClient", lambda **_: pytest.fail("no API key"))
    monkeypatch.setattr(
        ai_providers, "_estimin3n_transcribe", lambda *_: pytest.fail("model disabled")
    )

    assert asyncio.run(ai_providers.transcribe_audio(b"wav", language="kk")) == (None, "local")


def test_unknown_audio_provider_fails_closed_without_network(monkeypatch):
    monkeypatch.setattr(ai_providers, "_settings", lambda: _settings(AI_AUDIO_PROVIDER="surprise"))
    monkeypatch.setattr(
        ai_providers.httpx, "AsyncClient", lambda **_: pytest.fail("invalid provider")
    )

    assert asyncio.run(ai_providers.transcribe_audio(b"wav")) == (None, "local")

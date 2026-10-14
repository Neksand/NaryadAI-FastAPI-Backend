"""Deterministic routing and fail-closed tests for optional AI integrations."""

import asyncio
import json
import sys
import types
from types import SimpleNamespace

import pytest

from app import ai_providers


def run(coro):
    return asyncio.run(coro)


def settings(**overrides):
    defaults = {
        "AI_TEXT_PROVIDER": "off",
        "AI_VISION_PROVIDER": "off",
        "GEMINI_API_KEY": "gemini-key",
        "GEMINI_MODEL": "gemini-test",
        "OPENROUTER_API_KEY": "router-key",
        "OPENROUTER_MODEL": "router-test",
        "GROQ_API_KEY": "groq-key",
        "GROQ_MODEL": "groq-test",
        "ESTIMIN3N_ENABLED": False,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_openai_text_parser_accepts_content_and_rejects_bad_shapes():
    assert (
        ai_providers._openai_text({"choices": [{"message": {"content": "  готово  "}}]}) == "готово"
    )
    for payload in (
        {},
        {"choices": []},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": None}}]},
    ):
        assert ai_providers._openai_text(payload) is None


def test_chat_text_off_and_unknown_provider_are_local(monkeypatch):
    monkeypatch.setattr(ai_providers, "_settings", lambda: settings(AI_TEXT_PROVIDER="off, off"))
    assert run(ai_providers.chat_text("system", "user")) == (None, "local")
    monkeypatch.setattr(ai_providers, "_settings", lambda: settings(AI_TEXT_PROVIDER="unknown"))
    assert run(ai_providers.chat_text("system", "user")) == (None, "local")


def test_chat_text_uses_openrouter_credentials_and_returns_provider(monkeypatch):
    received = []

    async def provider(*args):
        received.append(args)
        return "результат"

    monkeypatch.setattr(ai_providers, "_settings", lambda: settings(AI_TEXT_PROVIDER="openrouter"))
    monkeypatch.setattr(ai_providers, "_openai_compat_text", provider)
    assert run(ai_providers.chat_text("правила", "запрос")) == ("результат", "openrouter")
    assert received == [
        ("https://openrouter.ai/api/v1", "router-key", "router-test", "правила", "запрос")
    ]


def test_chat_text_falls_back_after_first_provider_fails(monkeypatch):
    calls = []

    async def gemini(*_):
        calls.append("gemini")
        return None

    async def groq(*args):
        calls.append("groq")
        return "починено"

    async def compat(url, *_):
        return await groq() if "groq" in url else None

    monkeypatch.setattr(ai_providers, "_settings", lambda: settings(AI_TEXT_PROVIDER="gemini,groq"))
    monkeypatch.setattr(ai_providers, "_gemini_text", gemini)
    monkeypatch.setattr(ai_providers, "_openai_compat_text", compat)
    assert run(ai_providers.chat_text("system", "user")) == ("починено", "groq")
    assert "gemini" in calls and "groq" in calls


def test_chat_text_handles_provider_exceptions_and_auto_exhaustion(monkeypatch):
    async def broken(*_):
        raise RuntimeError("provider is down")

    async def success(*_):
        return "backup answer"

    monkeypatch.setattr(
        ai_providers, "_settings", lambda: settings(AI_TEXT_PROVIDER="gemini,openrouter")
    )
    monkeypatch.setattr(ai_providers, "_gemini_text", broken)
    monkeypatch.setattr(ai_providers, "_openai_compat_text", success)
    assert run(ai_providers.chat_text("s", "u")) == ("backup answer", "openrouter")

    async def no_result(*_args):
        return None

    monkeypatch.setattr(ai_providers, "_settings", lambda: settings(AI_TEXT_PROVIDER="auto"))
    monkeypatch.setattr(ai_providers, "_gemini_text", no_result)
    monkeypatch.setattr(ai_providers, "_openai_compat_text", no_result)
    monkeypatch.setattr(ai_providers, "_pollinations_text", no_result)
    monkeypatch.setattr(ai_providers, "_estimin3n_text", no_result)
    assert run(ai_providers.chat_text("s", "u")) == (None, "local")


def test_local_text_provider_obeys_enabled_flag_and_falls_back_on_model_errors(monkeypatch):
    from app.config import settings as real_settings

    monkeypatch.setattr(real_settings, "ESTIMIN3N_ENABLED", False)
    monkeypatch.setattr(
        ai_providers, "_estimin3n_generate", lambda *_: pytest.fail("disabled model loaded")
    )
    assert run(ai_providers._estimin3n_text("system", "user")) is None

    monkeypatch.setattr(real_settings, "ESTIMIN3N_ENABLED", True)
    received = []
    monkeypatch.setattr(
        ai_providers,
        "_estimin3n_generate",
        lambda messages, limit: received.append((messages, limit)) or "локальный ответ",
    )
    assert run(ai_providers._estimin3n_text("system", "user")) == "локальный ответ"
    assert received[0][0][1]["content"][0]["text"] == "user"
    assert received[0][1] == 512
    monkeypatch.setattr(
        ai_providers,
        "_estimin3n_generate",
        lambda *_: (_ for _ in ()).throw(RuntimeError("model unavailable")),
    )
    assert run(ai_providers._estimin3n_text("system", "user")) is None


def test_pollinations_uses_the_openai_compatible_payload(monkeypatch):
    captured = []

    async def post(url, headers, payload, **_kwargs):
        captured.append((url, headers, payload))
        return {"choices": [{"message": {"content": "answer"}}]}

    monkeypatch.setattr(ai_providers, "_post_json", post)
    assert run(ai_providers._pollinations_text("instructions", "question")) == "answer"
    url, headers, payload = captured[0]
    assert url == "https://text.pollinations.ai/openai"
    assert "Authorization" not in headers
    assert payload["messages"] == [
        {"role": "system", "content": "instructions"},
        {"role": "user", "content": "question"},
    ]


def test_openai_compatible_request_contract_and_empty_key_short_circuit(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "ответ"}}]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, url, **kwargs):
            captured.update(url=url, **kwargs)
            return Response()

    monkeypatch.setattr(ai_providers.httpx, "AsyncClient", lambda **_: Client())
    assert (
        run(
            ai_providers._openai_compat_text(
                "https://example.test/v1", "secret", "model", "sys", "usr"
            )
        )
        == "ответ"
    )
    assert captured["url"] == "https://example.test/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["json"]["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "usr"},
    ]
    assert run(ai_providers._openai_compat_text("https://example.test", "", "m", "s", "u")) is None


@pytest.mark.parametrize("status", [401, 429, 500])
def test_post_json_http_errors_fail_closed(monkeypatch, status):
    class Response:
        status_code = status

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(ai_providers.httpx, "AsyncClient", lambda **_: Client())
    assert run(ai_providers._post_json("https://example.test", {}, {})) is None


def test_post_json_network_and_invalid_json_errors_fail_closed(monkeypatch):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, *_args, **_kwargs):
            raise TimeoutError("slow provider")

    monkeypatch.setattr(ai_providers.httpx, "AsyncClient", lambda **_: Client())
    assert run(ai_providers._post_json("https://example.test", {}, {})) is None


def test_gemini_text_missing_key_skips_http(monkeypatch):
    monkeypatch.setattr(ai_providers, "_gemini_client", lambda: (None, None))
    monkeypatch.setattr(
        ai_providers, "_post_json", lambda *_args, **_kwargs: pytest.fail("unexpected HTTP")
    )
    assert run(ai_providers._gemini_text("system", "user")) is None


def test_gemini_text_parses_first_candidate(monkeypatch):
    monkeypatch.setattr(ai_providers, "_gemini_client", lambda: ("key", "model"))
    requests = []

    async def post(url, headers, payload, **kwargs):
        requests.append((url, headers, payload, kwargs))
        return {"candidates": [{"content": {"parts": [{"text": "  да  "}]}}]}

    monkeypatch.setattr(ai_providers, "_post_json", post)
    assert run(ai_providers._gemini_text("system", "user")) == "да"
    url, headers, payload, _ = requests[0]
    assert url.endswith("/models/model:generateContent")
    assert headers["x-goog-api-key"] == "key"
    assert payload["contents"][0]["parts"][0]["text"] == "user"


def test_gemini_client_resolves_only_configured_credentials(monkeypatch):
    from app.config import settings as real_settings

    monkeypatch.setattr(real_settings, "GEMINI_API_KEY", "")
    assert ai_providers._gemini_client() == (None, None)
    monkeypatch.setattr(real_settings, "GEMINI_API_KEY", "configured")
    monkeypatch.setattr(real_settings, "GEMINI_MODEL", "gemini-local-test")
    assert ai_providers._gemini_client() == ("configured", "gemini-local-test")


@pytest.mark.parametrize(
    "provider,expected",
    [
        ("off", (None, "local")),
        ("unknown", (None, "local")),
        ("auto", (None, "local")),
        ("gemini", (None, "local")),
    ],
)
def test_photo_provider_disabled_or_missing_credentials(monkeypatch, provider, expected):
    from app.config import settings as real_settings

    monkeypatch.setattr(real_settings, "AI_VISION_PROVIDER", provider)
    monkeypatch.setattr(real_settings, "GEMINI_API_KEY", "")
    monkeypatch.setattr(
        ai_providers, "_post_json", lambda *_args, **_kwargs: pytest.fail("unexpected HTTP")
    )
    assert run(ai_providers.compare_photos(b"before", b"after", "pump")) == expected


def test_photo_local_provider_disabled_does_not_load_model(monkeypatch):
    from app.config import settings as real_settings

    monkeypatch.setattr(real_settings, "AI_VISION_PROVIDER", "estimin3n")
    monkeypatch.setattr(real_settings, "ESTIMIN3N_ENABLED", False)
    monkeypatch.setattr(
        ai_providers, "_estimin3n_compare_photos", lambda *_: pytest.fail("disabled model loaded")
    )
    assert run(ai_providers.compare_photos(b"before", b"after", "pump")) == (None, "estimin3n")


def test_estimin3n_photo_provider_reports_result_and_failure(monkeypatch):
    from app.config import settings as real_settings

    monkeypatch.setattr(real_settings, "AI_VISION_PROVIDER", "estimin3n")
    monkeypatch.setattr(real_settings, "ESTIMIN3N_ENABLED", True)
    monkeypatch.setattr(ai_providers, "_estimin3n_compare_photos", lambda *_: '{"fixed":true}')
    assert run(ai_providers.compare_photos(b"before", b"after", "pump")) == (
        '{"fixed":true}',
        "estimin3n",
    )
    monkeypatch.setattr(
        ai_providers, "_estimin3n_compare_photos", lambda *_: (_ for _ in ()).throw(RuntimeError())
    )
    assert run(ai_providers.compare_photos(b"before", b"after", "pump")) == (None, "estimin3n")


def test_photo_gemini_encodes_both_images_and_parses_result(monkeypatch):
    from app.config import settings as real_settings

    monkeypatch.setattr(real_settings, "AI_VISION_PROVIDER", "gemini")
    monkeypatch.setattr(real_settings, "GEMINI_API_KEY", "key")
    monkeypatch.setattr(real_settings, "GEMINI_MODEL", "vision-test")
    captured = {}

    async def post(url, headers, payload, **kwargs):
        captured.update(url=url, headers=headers, payload=payload, kwargs=kwargs)
        return {"candidates": [{"content": {"parts": [{"text": "result"}]}}]}

    monkeypatch.setattr(ai_providers, "_post_json", post)
    assert run(ai_providers.compare_photos(b"before", b"after", "насос")) == (
        "result",
        "gemini-vision",
    )
    parts = captured["payload"]["contents"][0]["parts"]
    assert parts[1]["inline_data"]["data"] == "YmVmb3Jl"
    assert parts[2]["inline_data"]["data"] == "YWZ0ZXI="
    assert captured["kwargs"] == {"timeout": 30.0}


@pytest.mark.parametrize("fault", ["missing_ref", "missing_index", "missing_weight"])
def test_estimin3n_snapshot_validation_fails_with_actionable_context(tmp_path, fault):
    cache = tmp_path / "cache"
    repo = cache / "models--org--model"
    snapshot = repo / "snapshots" / "rev1"
    settings_obj = SimpleNamespace(ESTIMIN3N_CACHE_DIR=str(cache), ESTIMIN3N_MODEL_ID="org/model")
    if fault != "missing_ref":
        (repo / "refs").mkdir(parents=True)
        (repo / "refs" / "main").write_text("rev1")
        snapshot.mkdir(parents=True)
    if fault == "missing_weight":
        (snapshot / "model.safetensors.index.json").write_text(
            json.dumps({"weight_map": {"w": "part-1.safetensors"}})
        )

    expected = {
        "missing_ref": "run snapshot_download",
        "missing_index": "metadata is missing",
        "missing_weight": "weights are incomplete",
    }[fault]
    with pytest.raises(FileNotFoundError, match=expected):
        ai_providers._estimin3n_local_snapshot(settings_obj)


def test_estimin3n_snapshot_validation_accepts_complete_local_shards(tmp_path):
    cache = tmp_path / "cache"
    snapshot = cache / "models--org--model" / "snapshots" / "rev1"
    (snapshot / "model.safetensors.index.json").parent.mkdir(parents=True)
    (snapshot / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": {"a": "part-1", "b": "part-2"}})
    )
    (snapshot / "part-1").touch()
    (snapshot / "part-2").touch()
    (cache / "models--org--model" / "refs").mkdir()
    (cache / "models--org--model" / "refs" / "main").write_text("rev1")
    settings_obj = SimpleNamespace(ESTIMIN3N_CACHE_DIR=str(cache), ESTIMIN3N_MODEL_ID="org/model")

    assert ai_providers._estimin3n_local_snapshot(settings_obj) == snapshot


def test_estimin3n_generation_preserves_integer_masks_and_decodes_only_new_tokens(monkeypatch):
    converted = []

    class Tensor:
        shape = (1, 3)

        def __init__(self, floating=False):
            self.floating = floating

        def is_floating_point(self):
            return self.floating

        def to(self, destination):
            converted.append(destination)
            return self

    class Inputs(dict):
        def to(self, device):
            converted.append(device)
            return self

    inputs = Inputs(
        input_ids=Tensor(), input_features_mask=Tensor(floating=True), attention_mask=Tensor()
    )

    class Processor:
        tokenizer = SimpleNamespace(eos_token_id=7)

        @staticmethod
        def apply_chat_template(*_args, **_kwargs):
            return inputs

    class Model:
        @staticmethod
        def generate(**kwargs):
            assert kwargs["input_features_mask"] is inputs["input_features_mask"]
            assert kwargs["attention_mask"] is inputs["attention_mask"]
            assert kwargs["max_new_tokens"] == 9
            return [[1, 2, 3, 4, 5]]

    class Torch(types.ModuleType):
        def inference_mode(self):
            return _NullContext()

    class _NullContext:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    processor = Processor()
    decoded = []
    processor.tokenizer.decode = lambda tokens, **kwargs: (
        decoded.append((tokens, kwargs)) or "decoded"
    )
    monkeypatch.setitem(sys.modules, "torch", Torch("torch"))
    monkeypatch.setattr(
        ai_providers, "_estimin3n_runtime", lambda: (Model(), processor, "cpu", "bf16")
    )

    assert ai_providers._estimin3n_generate([], 9) == "decoded"
    assert decoded == [([4, 5], {"skip_special_tokens": True})]
    assert converted == ["cpu", "bf16"]


@pytest.mark.parametrize(
    ("language", "name", "kazakh_letters"), [("ru", "Russian", False), ("kk", "Kazakh", True)]
)
def test_local_transcription_resamples_mono_audio_and_sets_language_instruction(
    monkeypatch, language, name, kazakh_letters
):
    import numpy as np

    resampled = []
    generated = []
    soundfile = types.ModuleType("soundfile")
    soundfile.read = lambda *_args, **_kwargs: (
        np.array([[0.2, 0.4], [0.6, 0.8]], dtype=np.float32),
        8000,
    )
    librosa = types.ModuleType("librosa")
    librosa.resample = lambda audio, **kwargs: resampled.append((audio.copy(), kwargs)) or audio
    monkeypatch.setitem(sys.modules, "soundfile", soundfile)
    monkeypatch.setitem(sys.modules, "librosa", librosa)
    monkeypatch.setattr(
        ai_providers,
        "_estimin3n_generate",
        lambda messages, limit: generated.append((messages, limit)) or "текст",
    )

    assert ai_providers._estimin3n_transcribe(b"audio", language) == "текст"
    assert resampled[0][1] == {"orig_sr": 8000, "target_sr": 16000}
    assert generated[0][1] == 256
    instruction = generated[0][0][0]["content"][0]["text"]
    assert name in instruction
    assert ("ә, ғ, қ, ң, ө, ұ, ү, һ, і" in instruction) is kazakh_letters


def test_local_photo_comparison_preserves_before_after_order_and_privacy_prompt(monkeypatch):
    from PIL import Image

    def image_bytes(color):
        import io

        output = io.BytesIO()
        Image.new("RGB", (2, 2), color).save(output, format="PNG")
        return output.getvalue()

    messages = []
    monkeypatch.setattr(
        ai_providers,
        "_estimin3n_generate",
        lambda prompt, limit: messages.append((prompt, limit)) or "{}",
    )
    result = ai_providers._estimin3n_compare_photos(
        image_bytes("red"), image_bytes("blue"), "насос"
    )

    assert result == "{}"
    user_content = messages[0][0][1]["content"]
    assert user_content[0]["image"].getpixel((0, 0)) == (255, 0, 0)
    assert user_content[1]["image"].getpixel((0, 0)) == (0, 0, 255)
    assert "не включай персональные данные" in user_content[2]["text"]
    assert messages[0][1] == 192


@pytest.mark.parametrize(
    ("requested", "cuda", "mps", "four_bit", "expected"),
    [
        ("auto", True, True, False, "cuda"),
        ("auto", False, True, False, "mps"),
        ("auto", False, False, False, "cpu"),
        ("cuda", True, False, True, "cuda-4bit"),
    ],
)
def test_local_model_loader_selects_device_dtype_and_low_memory_options(
    monkeypatch, requested, cuda, mps, four_bit, expected
):
    from app.config import settings as real_settings

    calls = {"model": [], "processor": [], "to": [], "eval": 0}
    float16, bfloat16 = object(), object()

    class Model:
        def to(self, device):
            calls["to"].append(device)

        def eval(self):
            calls["eval"] += 1

    model = Model()

    class ModelLoader:
        @staticmethod
        def from_pretrained(path, **kwargs):
            calls["model"].append((path, kwargs))
            return model

    processor = object()

    class ProcessorLoader:
        @staticmethod
        def from_pretrained(path, **kwargs):
            calls["processor"].append((path, kwargs))
            return processor

    torch = types.ModuleType("torch")
    torch.float16 = float16
    torch.bfloat16 = bfloat16
    torch.cuda = SimpleNamespace(is_available=lambda: cuda)
    torch.backends = SimpleNamespace(mps=SimpleNamespace(is_available=lambda: mps))
    transformers = types.ModuleType("transformers")
    transformers.AutoProcessor = ProcessorLoader
    transformers.Gemma3nForConditionalGeneration = ModelLoader
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    monkeypatch.setattr(ai_providers, "_ESTIMIN3N_RUNTIME", None)
    monkeypatch.setattr(ai_providers, "_estimin3n_local_snapshot", lambda _settings: "/local/model")
    monkeypatch.setattr(real_settings, "ESTIMIN3N_DEVICE", requested)
    monkeypatch.setattr(real_settings, "ESTIMIN3N_CACHE_DIR", "/cache")
    monkeypatch.setattr(real_settings, "ESTIMIN3N_LOAD_IN_4BIT", four_bit)

    runtime = ai_providers._estimin3n_runtime()
    assert ai_providers._estimin3n_runtime() is runtime
    assert runtime[0] is model and runtime[1] is processor
    expected_device = "cuda" if expected == "cuda-4bit" else expected
    assert runtime[2] == expected_device
    options = calls["model"][0][1]
    assert options["trust_remote_code"] is True
    assert options["low_cpu_mem_usage"] is True
    assert options["local_files_only"] is True
    assert options["torch_dtype"] is (float16 if expected_device == "mps" else bfloat16)
    if expected_device == "cuda":
        assert options["device_map"] == "auto"
        assert options.get("load_in_4bit", False) is four_bit
        assert calls["to"] == []
    else:
        assert "device_map" not in options
        assert calls["to"] == [expected_device]
    assert calls["eval"] == 1 and len(calls["processor"]) == 1


def test_local_model_loader_rejects_4bit_without_cuda(monkeypatch):
    from app.config import settings as real_settings

    torch = types.ModuleType("torch")
    torch.float16 = object()
    torch.bfloat16 = object()
    torch.cuda = SimpleNamespace(is_available=lambda: False)
    torch.backends = SimpleNamespace(mps=SimpleNamespace(is_available=lambda: False))
    monkeypatch.setitem(sys.modules, "torch", torch)
    transformers = types.ModuleType("transformers")
    transformers.AutoProcessor = object
    transformers.Gemma3nForConditionalGeneration = object
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    monkeypatch.setattr(ai_providers, "_ESTIMIN3N_RUNTIME", None)
    monkeypatch.setattr(ai_providers, "_estimin3n_local_snapshot", lambda _settings: "/local/model")
    monkeypatch.setattr(real_settings, "ESTIMIN3N_DEVICE", "cpu")
    monkeypatch.setattr(real_settings, "ESTIMIN3N_LOAD_IN_4BIT", True)

    with pytest.raises(ValueError, match="requires CUDA"):
        ai_providers._estimin3n_runtime()

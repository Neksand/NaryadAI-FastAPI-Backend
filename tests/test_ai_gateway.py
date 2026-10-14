"""Gateway tests keep optional provider failures away from work-order flows."""

import asyncio
import json

from app.ai import gateway


def run(coro):
    return asyncio.run(coro)


class MockProvider:
    async def recommend_worker(self, ctx, language):
        return {"recommended_worker_id": None, "provider": "mock", "language": language}

    async def inspect_completion(self, ctx, language):
        return {"verdict": "PASS", "provider": "mock", "language": language}

    async def explain_anomaly(self, ctx, language):
        return f"mock:{language}:{ctx.get('headline', '')}"


def test_mock_gateway_routes_all_operations_to_local_provider(monkeypatch):
    monkeypatch.setattr(gateway, "_mode", lambda: "mock")
    monkeypatch.setattr(gateway, "_mock", MockProvider())
    assert run(gateway.recommend_worker({}, "kk"))["language"] == "kk"
    assert run(gateway.inspect_completion({}, "ru"))["verdict"] == "PASS"
    assert run(gateway.explain_anomaly({"headline": "Pump"}, "kk")) == "mock:kk:Pump"


def test_recommend_worker_parses_provider_json_and_bounds_reason(monkeypatch):
    monkeypatch.setattr(gateway, "_mode", lambda: "hosted")

    async def completion(*_):
        return "Here is JSON: " + json.dumps(
            {"index": 1, "confidence": 0.83, "reason": "x" * 400}
        ), "groq"

    monkeypatch.setattr("app.ai_providers.chat_text", completion)
    candidates = [{"employee_id": "w-1"}, {"employee_id": "w-2"}]
    result = run(gateway.recommend_worker({"candidates": candidates}, "ru"))
    assert result["recommended_worker_id"] == "w-2"
    assert result["confidence"] == 0.83
    assert result["provider"] == "groq"
    assert len(result["reason"]) == 300


def test_recommend_worker_falls_back_for_bad_json_and_out_of_range_index(monkeypatch):
    monkeypatch.setattr(gateway, "_mode", lambda: "hosted")
    fallback = MockProvider()
    monkeypatch.setattr(gateway, "_mock", fallback)
    outputs = iter([("not json", "groq"), ('{"index": 5}', "groq")])

    async def completion(*_):
        return next(outputs)

    monkeypatch.setattr("app.ai_providers.chat_text", completion)
    assert (
        run(gateway.recommend_worker({"candidates": [{"employee_id": "w-1"}]}, "kk"))["language"]
        == "kk"
    )
    assert (
        run(gateway.recommend_worker({"candidates": [{"employee_id": "w-1"}]}, "ru"))["language"]
        == "ru"
    )


def test_recommend_worker_falls_back_after_provider_exception(monkeypatch):
    monkeypatch.setattr(gateway, "_mode", lambda: "hosted")
    monkeypatch.setattr(gateway, "_mock", MockProvider())

    async def broken(*_):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("app.ai_providers.chat_text", broken)
    assert run(gateway.recommend_worker({"candidates": []}, "kk"))["provider"] == "mock"


def test_explain_anomaly_truncates_provider_output_and_falls_back_on_empty_or_error(monkeypatch):
    monkeypatch.setattr(gateway, "_mode", lambda: "hosted")

    async def long_text(*_):
        return "Ж" * 700, "openrouter"

    monkeypatch.setattr("app.ai_providers.chat_text", long_text)
    assert len(run(gateway.explain_anomaly({}, "kk"))) == 600

    async def no_text(*_):
        return None, "local"

    monkeypatch.setattr("app.ai_providers.chat_text", no_text)
    monkeypatch.setattr(gateway, "_mock", MockProvider())
    assert run(gateway.explain_anomaly({"headline": "Alarm"}, "ru")) == "mock:ru:Alarm"

    async def broken(*_):
        raise RuntimeError("offline")

    monkeypatch.setattr("app.ai_providers.chat_text", broken)
    assert run(gateway.explain_anomaly({"headline": "Alarm"}, "kk")) == "mock:kk:Alarm"

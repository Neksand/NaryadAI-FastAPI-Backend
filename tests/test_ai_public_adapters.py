"""Public AI entrypoints delegate to the gateway and remain deterministic offline."""

import asyncio

import pytest

from app.ai import anomaly, inspection, reports, vision
from app.ai.base import AIProvider
from app.ai.mock import MockAIProvider


def test_anomaly_adapter_forwards_context_and_requested_language(monkeypatch):
    calls = []

    async def explain(context, language):
        calls.append((context, language))
        return "Қайталанатын ақау"

    monkeypatch.setattr("app.ai.gateway.explain_anomaly", explain)

    result = asyncio.run(anomaly.explain("Повторный отказ", [{"count": 4}], "Диагностика", "kk"))

    assert result == "Қайталанатын ақау"
    assert calls == [
        (
            {
                "headline": "Повторный отказ",
                "evidence": [{"count": 4}],
                "recommendation": "Диагностика",
            },
            "kk",
        )
    ]


def test_inspection_adapter_forwards_context_and_language(monkeypatch):
    calls = []

    async def inspect_completion(context, language):
        calls.append((context, language))
        return {"verdict": "PASS", "provider": "mock"}

    monkeypatch.setattr("app.ai.gateway.inspect_completion", inspect_completion)

    result = asyncio.run(inspection.inspect({"work_done_text": "replaced bearing"}, "en"))

    assert result == {"verdict": "PASS", "provider": "mock"}
    assert calls == [({"work_done_text": "replaced bearing"}, "en")]


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("ru", "Смена: выдано 3, выполнено 2, просрочено 1."),
        ("kk", "Ауысым: берілгені 3, орындалғаны 2, кешіккені 1."),
        ("en", "Смена: выдано 3, выполнено 2, просрочено 1."),
    ],
)
def test_shift_summary_is_localized_with_russian_fallback(language, expected):
    assert (
        asyncio.run(reports.summarize_shift({"issued": 3, "done": 2, "overdue": 1}, language))
        == expected
    )


def test_shift_summary_uses_zero_defaults_for_missing_counters():
    assert (
        asyncio.run(reports.summarize_shift({}, "ru"))
        == "Смена: выдано 0, выполнено 0, просрочено 0."
    )


def test_vision_adapter_forwards_image_bytes_and_context(monkeypatch):
    calls = []

    async def compare(before, after, context):
        calls.append((before, after, context))
        return '{"score":4}', "local"

    monkeypatch.setattr(vision, "_compare", compare)

    result = asyncio.run(vision.compare_before_after(b"before", b"after", "fault=F01"))

    assert result == ('{"score":4}', "local")
    assert calls == [(b"before", b"after", "fault=F01")]


def test_provider_protocol_exposes_the_three_supported_operations():
    assert AIProvider.__name__ == "AIProvider"
    assert {"recommend_worker", "inspect_completion", "explain_anomaly"}.issubset(
        set(AIProvider.__dict__)
    )


@pytest.mark.parametrize(
    ("context", "language", "verdict", "recommendation"),
    [
        ({"missing": ["work_done_text"]}, "ru", "FAIL", "REWORK"),
        ({"duplicate": True}, "kk", "FAIL", "REWORK"),
        ({"material_flag": True}, "ru", "REVIEW", "MASTER_REVIEW"),
        ({"stale_exif": True}, "kk", "REVIEW", "MASTER_REVIEW"),
        ({}, "kk", "PASS", "CLOSE"),
    ],
)
def test_mock_completion_verdicts_cover_failure_review_and_pass(
    context, language, verdict, recommendation
):
    result = asyncio.run(MockAIProvider().inspect_completion(context, language))

    assert result["verdict"] == verdict
    assert result["recommendation"] == recommendation
    assert result["provider"] == "mock"
    assert result["explanation"]


def test_mock_worker_recommendation_handles_empty_and_ranked_candidates():
    provider = MockAIProvider()
    empty = asyncio.run(provider.recommend_worker({"candidates": []}, "ru"))
    ranked = asyncio.run(
        provider.recommend_worker(
            {
                "candidates": [
                    {"employee_id": "best"},
                    {"employee_id": "next"},
                    {"employee_id": "third"},
                    {"employee_id": "ignored"},
                ]
            },
            "kk",
        )
    )

    assert empty["recommended_worker_id"] is None
    assert empty["confidence"] == 0.2
    assert ranked["recommended_worker_id"] == "best"
    assert ranked["alternatives"] == ["next", "third"]
    assert ranked["provider"] == "mock"


def test_mock_anomaly_explanation_preserves_context_and_language():
    provider = MockAIProvider()
    context = {"headline": "Рост отказов", "recommendation": "Проверить узел"}

    russian = asyncio.run(provider.explain_anomaly(context, "ru"))
    kazakh = asyncio.run(provider.explain_anomaly(context, "kk"))

    assert russian == "Рост отказов. Рекомендация: Проверить узел"
    assert kazakh == "Рост отказов. Ұсыныс: Проверить узел"

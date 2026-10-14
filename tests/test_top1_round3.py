"""Top-1 round 3: AI mock/gateway/anomaly/recommendation branches. Pure unit."""

import asyncio

import pytest


def run(coro):
    return asyncio.run(coro)


def test_pick_deterministic_range():
    from app.ai.mock import _pick

    assert _pick("a", [1, 2, 3]) == _pick("a", [1, 2, 3])
    assert 0 <= _pick("x", ["a", "b"]) < 2


@pytest.mark.parametrize("lang", ["ru", "kk", "en"])
def test_recommend_empty_lang_fallback(lang):
    from app.ai.mock import MockAIProvider

    out = run(MockAIProvider().recommend_worker({"candidates": []}, lang))
    assert out["recommended_worker_id"] is None
    assert out["provider"] == "mock"
    assert out["confidence"] < 0.5


def test_recommend_picks_first_with_alternatives():
    from app.ai.mock import MockAIProvider

    ctx = {
        "candidates": [
            {"employee_id": "e1"},
            {"employee_id": "e2"},
            {"employee_id": "e3"},
            {"employee_id": "e4"},
        ]
    }
    ru = run(MockAIProvider().recommend_worker(ctx, "ru"))
    assert ru["recommended_worker_id"] == "e1"
    assert ru["alternatives"] == ["e2", "e3"]
    kk = run(MockAIProvider().recommend_worker(ctx, "kk"))
    assert "үміткер" in kk["reason"]


@pytest.mark.parametrize("lang", ["ru", "kk"])
def test_inspect_fail_review_pass(lang):
    from app.ai.mock import MockAIProvider

    m = MockAIProvider()
    fail = run(m.inspect_completion({"missing": ["work_done_text"], "duplicate": True}, lang))
    assert fail["verdict"] == "FAIL" and fail["recommendation"] == "REWORK"
    review = run(m.inspect_completion({"material_flag": True}, lang))
    assert review["verdict"] == "REVIEW" and review["recommendation"] == "MASTER_REVIEW"
    review2 = run(m.inspect_completion({"stale_exif": True}, lang))
    assert review2["verdict"] == "REVIEW"
    ok = run(m.inspect_completion({}, lang))
    assert ok["verdict"] == "PASS" and ok["recommendation"] == "CLOSE"


def test_inspect_fail_only_missing_or_only_duplicate():
    from app.ai.mock import MockAIProvider

    m = MockAIProvider()
    assert run(m.inspect_completion({"missing": ["x"]}, "ru"))["verdict"] == "FAIL"
    out = run(m.inspect_completion({"duplicate": True}, "ru"))
    assert out["issues"] == ["duplicate_photo"]


@pytest.mark.parametrize("lang", ["ru", "kk"])
def test_explain_anomaly_bilingual(lang):
    from app.ai.mock import MockAIProvider

    out = run(MockAIProvider().explain_anomaly({"headline": "H", "recommendation": "R"}, lang))
    assert "H" in out and "R" in out


def test_gateway_delegates_to_mock():
    import app.config as cfg

    cfg.settings.AI_MODE = "mock"
    from app.ai.gateway import inspect_completion, recommend_worker

    bad = run(inspect_completion({"missing": ["a"]}, "ru"))
    assert bad["provider"] == "mock" and bad["verdict"] == "FAIL"
    rec = run(recommend_worker({"candidates": [{"employee_id": "z"}]}, "ru"))
    assert rec["recommended_worker_id"] == "z"


def test_anomaly_explain_wraps():
    from app.ai.anomaly import explain

    out = run(explain("Заголовок", [{"count": 1}], "Проверить", "ru"))
    assert "Заголовок" in out
    out_kk = run(explain("Тақырып", [{"count": 1}], "Тексеру", "kk"))
    assert "Тақырып" in out_kk


def test_worker_recommendation_module():
    from app.ai.worker_recommendation import recommend

    out = run(recommend({"candidates": [{"employee_id": "a"}]}, "ru"))
    assert out["recommended_worker_id"] == "a"
    empty = run(recommend({"candidates": []}, "kk"))
    assert empty["recommended_worker_id"] is None


def test_vision_compare_returns_tuple():
    from app.ai.vision import compare_before_after

    text, provider = run(compare_before_after(b"before", b"after", "fault=F01"))
    assert isinstance(text, (str, type(None))) and isinstance(provider, str)


def test_inspection_module_delegates():
    import app.config as cfg

    cfg.settings.AI_MODE = "mock"
    from app.ai.inspection import inspect

    assert run(inspect({}, "ru"))["verdict"] == "PASS"
    assert run(inspect({"missing": ["x"]}, "kk"))["verdict"] == "FAIL"

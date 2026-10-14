"""Top-1 round 2: economics + reports builders + AI mock branches. Pure unit, no DB."""

import pytest


def _agg(**over):
    base = dict(
        completed_orders=0,
        norm_hours=0,
        completed_without_norm=0,
        downtime_hours=0,
        total_orders=0,
        unplanned_orders=0,
    )
    base.update(over)
    return base


def _call(**kw):
    from app.services.economics import operational_estimate

    base = dict(
        aggregate=_agg(),
        material_rows=[],
        labor_rate_per_hour=None,
        downtime_cost_per_hour=None,
        material_unit_prices={},
        currency="KZT",
    )
    base.update(kw)
    return operational_estimate(**base)


def test_empty_period_zero_costs_complete():
    out = _call()
    assert out["estimated_costs"]["complete"] is True
    assert out["estimated_costs"]["total"] == 0
    assert out["period_metrics"]["unplanned_share"] is None


def test_unplanned_share_math():
    out = _call(aggregate=_agg(total_orders=4, unplanned_orders=1))
    assert out["period_metrics"]["unplanned_share"] == 0.25


def test_labor_complete_only_with_rate_and_norms():
    from app.services.economics import operational_estimate

    agg = _agg(completed_orders=2, norm_hours=3, completed_without_norm=0, total_orders=2)
    out = operational_estimate(
        aggregate=agg,
        material_rows=[],
        labor_rate_per_hour=1000,
        downtime_cost_per_hour=None,
        material_unit_prices={},
        currency="KZT",
    )
    assert out["estimated_costs"]["labor_complete"] is True
    assert out["estimated_costs"]["labor_known_part"] == 3000


@pytest.mark.parametrize("missing", [1, 2])
def test_labor_incomplete_without_norm_or_rate(missing):
    out = _call(
        aggregate=_agg(
            completed_orders=2, norm_hours=2, completed_without_norm=missing, total_orders=2
        ),
        labor_rate_per_hour=1000,
    )
    assert out["estimated_costs"]["labor_complete"] is False
    assert out["estimated_costs"]["complete"] is False
    assert out["estimated_costs"]["total"] is None


def test_materials_partial_and_complete():
    out = _call(
        material_rows=[
            {"id": "m1", "name": "Подшипник", "unit": "шт", "quantity": 2},
            {"id": "m2", "name": "Масло", "unit": "л", "quantity": 1},
        ],
        material_unit_prices={"m1": 500},
    )
    assert out["estimated_costs"]["materials_complete"] is False
    assert out["data_quality"]["materials_without_price"][0]["material_id"] == "m2"
    out2 = _call(
        material_rows=[{"id": "m1", "name": "X", "unit": "шт", "quantity": 2}],
        material_unit_prices={"m1": 500},
    )
    assert out2["estimated_costs"]["materials_total"] == 1000


def test_downtime_branches():
    assert _call(aggregate=_agg(downtime_hours=0))["estimated_costs"]["downtime"] == 0
    out = _call(aggregate=_agg(downtime_hours=2), downtime_cost_per_hour=100)
    assert out["estimated_costs"]["downtime"] == 200
    out2 = _call(aggregate=_agg(downtime_hours=2), downtime_cost_per_hour=None)
    assert out2["estimated_costs"]["downtime"] is None
    assert out2["estimated_costs"]["downtime_complete"] is False


def test_feasibility_verdicts():
    from app.services.economics import feasibility_scenario

    pos = feasibility_scenario(
        investment=1000,
        monthly_benefit=500,
        monthly_operating_cost=100,
        horizon_months=12,
        annual_discount_rate_pct=10,
        residual_value=0,
        currency="KZT",
    )
    assert pos["verdict"] == "positive_npv"
    assert pos["payback_within_horizon"] is True
    assert pos["simple_payback_months"] is not None
    neg = feasibility_scenario(
        investment=100000,
        monthly_benefit=10,
        monthly_operating_cost=5,
        horizon_months=6,
        annual_discount_rate_pct=20,
        residual_value=0,
        currency="KZT",
    )
    assert neg["verdict"] == "negative_npv"
    assert neg["annualized_irr_pct"] is None or isinstance(neg["annualized_irr_pct"], float)


def test_feasibility_zero_net_no_payback():
    from app.services.economics import feasibility_scenario

    out = feasibility_scenario(
        investment=500,
        monthly_benefit=100,
        monthly_operating_cost=100,
        horizon_months=12,
        annual_discount_rate_pct=10,
        residual_value=0,
        currency="KZT",
    )
    assert out["simple_payback_months"] is None
    assert out["monthly_net_benefit"] == 0


def test_xlsx_builder_produces_zip():
    from app.services.reports import _xlsx

    data = _xlsx(["id", "name"], [["1", "Насос"], ["2", "Станок"]])
    assert data[:2] == b"PK"


def test_pdf_builder_requires_font(tmp_path, monkeypatch):
    from app.services.reports import _pdf

    monkeypatch.setenv("PDF_FONT_PATH", str(tmp_path / "missing.ttf"))
    with pytest.raises(RuntimeError, match="PDF_FONT_PATH"):
        _pdf(["a"], [["b"]])


def test_ai_mock_gateway_branches():
    import asyncio

    import app.config as cfg
    from app.ai.gateway import inspect_completion, recommend_worker

    cfg.settings.AI_MODE = "mock"
    for lang in ("ru", "kk"):
        bad = asyncio.run(inspect_completion({"missing": ["x"], "duplicate": True}, lang))
        assert bad["verdict"] in ("FAIL", "PASS")
        rec = asyncio.run(recommend_worker({"candidates": []}, lang))
        assert rec["recommended_worker_id"] is None


@pytest.mark.parametrize("lang", ["ru", "kk"])
def test_report_templates_bilingual(lang):
    from app.notifications import TEMPLATES

    for key in (
        "WORK_ORDER_ASSIGNED",
        "WORK_ORDER_CLOSED",
        "DEADLINE_WARNING",
        "DEADLINE_EXCEEDED",
        "AI_INSPECTION_READY",
    ):
        title, msg = TEMPLATES[key](
            {"number": 1, "score": 5, "verdict": "ok", "minutes_left": 5, "minutes_overdue": 5},
            lang,
        )
        assert title and msg

"""Exact deterministic checks for operational cost and investment scenarios."""

import pytest

from app.services.economics import feasibility_scenario, operational_estimate


def _aggregate(**overrides):
    values = {
        "completed_orders": 3,
        "norm_hours": 6.0,
        "completed_without_norm": 0,
        "downtime_hours": 2.0,
        "total_orders": 4,
        "unplanned_orders": 1,
    }
    values.update(overrides)
    return values


def test_operational_cost_sums_only_explicit_assumptions_and_reports_rounding():
    result = operational_estimate(
        _aggregate(),
        [{"id": "mat-bolt", "name": "Болт", "unit": "шт", "quantity": 2.555}],
        labor_rate_per_hour=100,
        downtime_cost_per_hour=50,
        material_unit_prices={"mat-bolt": 10},
        currency="KZT",
    )

    assert result["estimated_costs"] == {
        "labor_known_part": 600.0,
        "labor_complete": True,
        "materials_known_part": 25.55,
        "materials_total": 25.55,
        "materials_complete": True,
        "downtime": 100.0,
        "downtime_complete": True,
        "total": 725.55,
        "complete": True,
    }
    assert result["period_metrics"]["unplanned_share"] == 0.25
    assert result["materials"][0]["quantity"] == 2.555
    assert result["data_quality"]["normative_minutes_are_not_tracked_labor_time"] is True


def test_operational_cost_never_fills_missing_norms_prices_or_downtime_rates():
    result = operational_estimate(
        _aggregate(completed_orders=2, norm_hours=3, completed_without_norm=1, downtime_hours=4),
        [
            {"id": "priced", "name": "Фильтр", "unit": "шт", "quantity": 2},
            {"id": "missing", "name": "Масло", "unit": "л", "quantity": 1.5},
        ],
        labor_rate_per_hour=200,
        downtime_cost_per_hour=None,
        material_unit_prices={"priced": 11},
        currency="KZT",
    )

    costs = result["estimated_costs"]
    assert costs["labor_known_part"] == 600
    assert costs["labor_complete"] is False
    assert costs["materials_known_part"] == 22
    assert costs["materials_total"] is None
    assert costs["materials_complete"] is False
    assert costs["downtime"] is None
    assert costs["downtime_complete"] is False
    assert costs["total"] is None
    assert costs["complete"] is False
    assert result["data_quality"]["completed_orders_without_norm"] == 1
    assert result["data_quality"]["materials_without_price"] == [
        {"material_id": "missing", "name": "Масло", "unit": "л"}
    ]


def test_empty_period_is_zero_cost_without_division_by_zero():
    result = operational_estimate(
        _aggregate(
            completed_orders=0,
            norm_hours=0,
            completed_without_norm=0,
            downtime_hours=0,
            total_orders=0,
            unplanned_orders=0,
        ),
        [],
        labor_rate_per_hour=None,
        downtime_cost_per_hour=None,
        material_unit_prices={},
        currency="KZT",
    )

    assert result["period_metrics"]["unplanned_share"] is None
    assert result["estimated_costs"]["total"] == 0
    assert result["estimated_costs"]["complete"] is True


def test_feasibility_zero_discount_matches_nominal_roi_and_payback():
    result = feasibility_scenario(
        investment=1000,
        monthly_benefit=150,
        monthly_operating_cost=50,
        horizon_months=12,
        annual_discount_rate_pct=0,
        residual_value=0,
        currency="KZT",
    )

    assert result["verdict"] == "positive_npv"
    assert result["net_profit"] == 200
    assert result["roi_pct"] == 20
    assert result["simple_payback_months"] == 10
    assert result["payback_within_horizon"] is True
    assert result["npv"] == 200
    assert result["annualized_irr_pct"] is not None
    annual_irr = result["annualized_irr_pct"] / 100
    recomputed_npv = sum(100 / ((1 + annual_irr) ** (month / 12)) for month in range(1, 13)) - 1000
    assert recomputed_npv == pytest.approx(0, abs=0.1)


def test_feasibility_negative_cashflow_has_no_payback_or_irr():
    result = feasibility_scenario(
        investment=1000,
        monthly_benefit=50,
        monthly_operating_cost=80,
        horizon_months=12,
        annual_discount_rate_pct=10,
        residual_value=0,
        currency="KZT",
    )

    assert result["verdict"] == "negative_npv"
    assert result["simple_payback_months"] is None
    assert result["payback_within_horizon"] is False
    assert result["annualized_irr_pct"] is None
    assert result["npv"] < 0


def test_feasibility_break_even_verdict_uses_small_currency_tolerance():
    result = feasibility_scenario(
        investment=1000,
        monthly_benefit=125,
        monthly_operating_cost=0,
        horizon_months=8,
        annual_discount_rate_pct=0,
        residual_value=0,
        currency="KZT",
    )

    assert result["verdict"] == "break_even"
    assert result["npv"] == 0
    assert result["roi_pct"] == 0
    assert result["payback_within_horizon"] is True

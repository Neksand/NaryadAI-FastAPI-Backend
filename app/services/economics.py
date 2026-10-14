"""Deterministic economic estimates from maintenance history and explicit assumptions."""

from __future__ import annotations

from typing import Any


def operational_estimate(
    aggregate: dict[str, Any],
    material_rows: list[dict[str, Any]],
    *,
    labor_rate_per_hour: float | None,
    downtime_cost_per_hour: float | None,
    material_unit_prices: dict[str, float],
    currency: str,
) -> dict[str, Any]:
    """Cost the recorded period without pretending missing prices or norms are known."""
    completed = int(aggregate["completed_orders"] or 0)
    norm_hours = float(aggregate["norm_hours"] or 0)
    missing_norm = int(aggregate["completed_without_norm"] or 0)
    downtime_hours = float(aggregate["downtime_hours"] or 0)
    total_orders = int(aggregate["total_orders"] or 0)
    unplanned = int(aggregate["unplanned_orders"] or 0)

    labor_known = (
        norm_hours * labor_rate_per_hour
        if labor_rate_per_hour is not None
        else 0.0
        if completed == 0
        else None
    )
    labor_complete = completed == 0 or (labor_rate_per_hour is not None and missing_norm == 0)

    materials = []
    material_known = 0.0
    missing_prices = []
    for row in material_rows:
        material_id = str(row["id"])
        quantity = float(row["quantity"] or 0)
        unit_price = material_unit_prices.get(material_id)
        cost = quantity * unit_price if unit_price is not None else None
        if cost is None:
            missing_prices.append(
                {"material_id": material_id, "name": row["name"], "unit": row["unit"]}
            )
        else:
            material_known += cost
        materials.append(
            {
                "material_id": material_id,
                "name": row["name"],
                "unit": row["unit"],
                "quantity": round(quantity, 3),
                "unit_price": round(unit_price, 2) if unit_price is not None else None,
                "cost": round(cost, 2) if cost is not None else None,
            }
        )

    material_complete = not missing_prices
    material_cost = material_known if material_complete else None
    if downtime_hours == 0:
        downtime_cost = 0.0
        downtime_complete = True
    elif downtime_cost_per_hour is not None:
        downtime_cost = downtime_hours * downtime_cost_per_hour
        downtime_complete = True
    else:
        downtime_cost = None
        downtime_complete = False

    total_complete = labor_complete and material_complete and downtime_complete
    total = (
        (float(labor_known or 0) + float(material_cost or 0) + float(downtime_cost or 0))
        if total_complete
        else None
    )

    return {
        "currency": currency,
        "period_metrics": {
            "orders": total_orders,
            "completed_orders": completed,
            "unplanned_orders": unplanned,
            "unplanned_share": round(unplanned / total_orders, 4) if total_orders else None,
            "normative_labor_hours": round(norm_hours, 2),
            "downtime_hours": round(downtime_hours, 2),
        },
        "estimated_costs": {
            "labor_known_part": round(labor_known, 2) if labor_known is not None else None,
            "labor_complete": labor_complete,
            "materials_known_part": round(material_known, 2),
            "materials_total": round(material_cost, 2) if material_cost is not None else None,
            "materials_complete": material_complete,
            "downtime": round(downtime_cost, 2) if downtime_cost is not None else None,
            "downtime_complete": downtime_complete,
            "total": round(total, 2) if total is not None else None,
            "complete": total_complete,
        },
        "materials": materials,
        "data_quality": {
            "completed_orders_without_norm": missing_norm,
            "materials_without_price": missing_prices,
            "normative_minutes_are_not_tracked_labor_time": True,
        },
        "assumptions": {
            "labor_rate_per_hour": labor_rate_per_hour,
            "downtime_cost_per_hour": downtime_cost_per_hour,
            "material_prices": "Supplied per material id in the request; unspecified prices are not imputed.",
        },
    }


def _npv(
    annual_rate: float, initial_investment: float, monthly_net: float, horizon: int, residual: float
) -> float:
    value = -initial_investment
    for month in range(1, horizon + 1):
        cashflow = monthly_net + (residual if month == horizon else 0.0)
        value += cashflow / ((1.0 + annual_rate) ** (month / 12.0))
    return value


def _annualized_irr(
    initial_investment: float, monthly_net: float, horizon: int, residual: float
) -> float | None:
    # A unique conventional IRR is available for an initial outflow followed by
    # non-negative, positive recurring inflows. Return null for non-standard flows.
    if monthly_net <= 0:
        return None
    low, high = -0.999999, 1.0
    while high < 100.0 and _npv(high, initial_investment, monthly_net, horizon, residual) > 0:
        high *= 2
    if _npv(high, initial_investment, monthly_net, horizon, residual) > 0:
        return None
    for _ in range(100):
        mid = (low + high) / 2
        if _npv(mid, initial_investment, monthly_net, horizon, residual) > 0:
            low = mid
        else:
            high = mid
    return (low + high) / 2


def feasibility_scenario(
    *,
    investment: float,
    monthly_benefit: float,
    monthly_operating_cost: float,
    horizon_months: int,
    annual_discount_rate_pct: float,
    residual_value: float,
    currency: str,
) -> dict[str, Any]:
    """Evaluate a simple project cash-flow scenario with no AI-generated values."""
    monthly_net = monthly_benefit - monthly_operating_cost
    total_benefits = monthly_benefit * horizon_months + residual_value
    total_operating_cost = monthly_operating_cost * horizon_months
    net_profit = total_benefits - total_operating_cost - investment
    roi_pct = net_profit / investment * 100
    discount_rate = annual_discount_rate_pct / 100
    npv = _npv(discount_rate, investment, monthly_net, horizon_months, residual_value)
    payback = investment / monthly_net if monthly_net > 0 else None
    irr = _annualized_irr(investment, monthly_net, horizon_months, residual_value)
    nominal_cash_recovered = monthly_net * horizon_months + residual_value

    if npv > 0.005:
        verdict = "positive_npv"
    elif npv < -0.005:
        verdict = "negative_npv"
    else:
        verdict = "break_even"

    return {
        "currency": currency,
        "verdict": verdict,
        "investment": round(investment, 2),
        "monthly_net_benefit": round(monthly_net, 2),
        "horizon_months": horizon_months,
        "total_benefits_including_residual": round(total_benefits, 2),
        "total_operating_cost": round(total_operating_cost, 2),
        "net_profit": round(net_profit, 2),
        "roi_pct": round(roi_pct, 2),
        "simple_payback_months": round(payback, 2) if payback is not None else None,
        "payback_within_horizon": nominal_cash_recovered >= investment,
        "npv": round(npv, 2),
        "annualized_irr_pct": round(irr * 100, 2) if irr is not None else None,
        "discount_rate_annual_pct": annual_discount_rate_pct,
        "residual_value": round(residual_value, 2),
        "assumptions": {
            "monthly_benefit_and_operating_cost_are_constant": True,
            "cashflows_arrive_at_month_end": True,
            "taxes_inflation_and_financing_costs_are_excluded": True,
        },
    }

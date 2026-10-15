"""Failure-forecast analytics: own scoring over unplanned work-order aggregates.

Own implementation: per equipment we count unplanned work orders issued in the
last 30 days (recent) and in the 30 days before that (prev), then score
heuristic risk = clamp(0.05, 0.95, 0.15 + 0.08*recent + 0.25*growth + 0.04*criticality)
with growth = min(1, (recent+1)/(prev+1) - 1) and criticality A=3/B=2/C=1.
The legacy probability field is NOT a calibrated failure probability.
"""

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator

from app.db import get_pool
from app.deps import get_current_user, require_role

router = APIRouter()

CRITICALITY_WEIGHTS = {"A": 3.0, "B": 2.0, "C": 1.0}

BASE_RATE = 0.15
RECENT_WEIGHT = 0.08
GROWTH_WEIGHT = 0.25
CRITICALITY_WEIGHT = 0.04
PROB_MIN = 0.05
PROB_MAX = 0.95


def clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def criticality_weight(criticality: str | int | float) -> float:
    if isinstance(criticality, str):
        return CRITICALITY_WEIGHTS.get(criticality.upper(), 1.0)
    return float(criticality)


def forecast_growth(recent: int, prev: int) -> float:
    return min(1.0, (recent + 1) / (prev + 1) - 1)


def score_forecast(recent: int, prev: int, crit: str | int | float) -> dict[str, float]:
    """Pure scoring: returns {"probability": ..., "growth": ...}, both rounded."""
    growth = forecast_growth(recent, prev)
    probability = clamp(
        BASE_RATE
        + RECENT_WEIGHT * recent
        + GROWTH_WEIGHT * growth
        + CRITICALITY_WEIGHT * criticality_weight(crit),
        PROB_MIN,
        PROB_MAX,
    )
    return {"probability": round(probability, 3), "growth": round(growth, 3)}


class ForecastItem(BaseModel):
    equipment_id: str
    equipment_name: str
    risk_index: float = Field(
        description="Heuristic risk index 0..1, not a calibrated failure probability"
    )
    probability: float = Field(
        default=0.0,
        description="Legacy alias of risk_index, kept for contract compatibility",
    )
    score_kind: Literal["heuristic_risk_index"] = "heuristic_risk_index"
    recent: int
    growth: float

    @model_validator(mode="after")
    def _sync_legacy_alias(self) -> "ForecastItem":
        self.probability = self.risk_index
        return self


@router.get("/analytics/failure-forecast", summary="Heuristic maintenance risk per equipment")
async def failure_forecast(
    user: dict[str, Any] = Depends(get_current_user),
) -> list[ForecastItem]:
    require_role(user, "master", "manager", "admin")
    pool = await get_pool()
    area_filter = " AND e.area_id = ANY($1::uuid[])" if user["role"] == "master" else ""
    args = [user["areaIds"]] if user["role"] == "master" else []
    rows = await pool.fetch(
        f"""SELECT e.id, e.name, e.criticality,
              count(*) FILTER (WHERE w.kind='unplanned'
                AND w.issued_at >= now() - interval '30 days')::int AS recent,
              count(*) FILTER (WHERE w.kind='unplanned'
                AND w.issued_at >= now() - interval '60 days'
                AND w.issued_at < now() - interval '30 days')::int AS prev
           FROM equipment e LEFT JOIN work_orders w ON w.equipment_id = e.id
           WHERE e.deleted_at IS NULL{area_filter} GROUP BY e.id, e.name, e.criticality""",
        *args,
    )
    items: list[ForecastItem] = []
    for r in rows:
        d = dict(r)
        recent = int(d["recent"])
        prev = int(d["prev"])
        scored = score_forecast(recent, prev, str(d.get("criticality") or "C"))
        items.append(
            ForecastItem(
                equipment_id=str(d["id"]),
                equipment_name=str(d["name"]),
                risk_index=scored["probability"],
                recent=recent,
                growth=scored["growth"],
            )
        )
    items.sort(key=lambda x: (-x.risk_index, -x.recent, x.equipment_name))
    return items

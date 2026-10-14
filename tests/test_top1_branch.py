"""Top-1 branch push: validators, row mapping, request models. Pure unit."""

import uuid
from datetime import datetime

import pytest


def test_transition_validators_trim_and_passthrough():
    from app.routers.work_orders import PatchWO, ReviewDecision, TransitionForm, TransitionIn

    assert TransitionIn(action="accept", comment="  hi  ").comment == "hi"
    assert TransitionIn(action="accept", comment=None).comment is None
    assert TransitionIn(action="accept", client_at=datetime(2026, 1, 1)).client_at is not None
    assert TransitionForm(work_done_text="  x  ", comment=None).work_done_text == "x"
    assert TransitionForm(work_done_text=None).work_done_text is None
    assert PatchWO(comment="  y  ").comment == "y"
    assert PatchWO(comment=None).comment is None
    assert ReviewDecision(decision="agree_ai", comment="  z ").comment == "z"


def test_row_to_dict_conversions():
    from app.routers.work_orders import _row_to_dict

    oid = uuid.uuid4()
    dt = datetime(2026, 1, 2, 3, 4, 5)
    out = _row_to_dict({"id": oid, "at": dt, "n": 1})
    assert out["id"] == str(oid) and out["at"] == dt.isoformat() and out["n"] == 1


def test_economics_request_naive_becomes_utc():
    from app.routers.analytics import OperationalEconomicsRequest

    r = OperationalEconomicsRequest(**{"from": "2026-01-01T00:00:00", "to": "2026-02-01T00:00:00"})
    assert r.from_.tzinfo is not None and r.to.tzinfo is not None


def test_economics_request_bad_material_and_period():
    import uuid as _uuid

    from pydantic import ValidationError

    from app.routers.analytics import OperationalEconomicsRequest

    with pytest.raises(ValidationError):
        OperationalEconomicsRequest(material_unit_prices={"not-uuid": 1})
    with pytest.raises(ValidationError):
        OperationalEconomicsRequest(material_unit_prices={str(_uuid.uuid4()): float("inf")})
    with pytest.raises(ValidationError):
        OperationalEconomicsRequest(
            **{"from": "2026-02-01T00:00:00+00:00", "to": "2026-01-01T00:00:00+00:00"}
        )
    ok = OperationalEconomicsRequest(material_unit_prices={str(_uuid.uuid4()): 10})
    assert isinstance(ok.material_unit_prices, dict)


def test_feasibility_request_bounds():
    from pydantic import ValidationError

    from app.routers.analytics import FeasibilityScenarioRequest

    with pytest.raises(ValidationError):
        FeasibilityScenarioRequest(
            investment=0,
            monthly_benefit=1,
            monthly_operating_cost=0,
            horizon_months=12,
            annual_discount_rate_pct=10,
            residual_value=0,
            currency="KZT",
        )
    ok = FeasibilityScenarioRequest(
        investment=100,
        monthly_benefit=50,
        monthly_operating_cost=10,
        horizon_months=12,
        annual_discount_rate_pct=5,
        residual_value=0,
        currency="KZT",
    )
    assert ok.investment == 100


def test_deps_master_ai_review_and_empty():
    from app.deps import allowed_actions

    master = {"role": "master", "areaIds": ["a1"]}
    assert allowed_actions(master, {"status": "ai_review", "area_id": "a1"}) == [
        "close",
        "return_to_rework",
    ]
    assert allowed_actions(master, {"status": "done", "area_id": "a1"}) == []
    assert allowed_actions(
        {"role": "worker", "id": "w1", "crewId": None},
        {"status": "issued", "assignee_id": "w1", "crew_id": None},
    ) == ["accept", "queue", "reject"]


def test_cursor_none_and_bad():
    from app.deps import decode_cursor, encode_cursor
    from app.errors import AppError

    assert decode_cursor(None) is None
    cur = encode_cursor(str(uuid.uuid4()), "2026-01-01T00:00:00+00:00")
    assert decode_cursor(cur)["id"]
    with pytest.raises(AppError):
        decode_cursor("bad!!!")


def test_notifications_s_and_t():
    import uuid as _uuid

    from app.notifications import _t
    from app.routers.notifications import _s

    assert _t("ru", "kk", "kk") == "kk" and _t("ru", "kk", "xx") == "ru"
    oid = _uuid.uuid4()
    assert _s({"id": oid})["id"] == str(oid)

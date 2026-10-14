"""Pure unit tests: no DB/Redis/S3. Run: pytest tests/test_unit_text_and_transitions.py -q"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest


def test_trim_edges_only_trims_borders():
    from app.text_utils import normalize_text_dict, trim_edges

    assert trim_edges("  Течь масла  ") == "Течь масла"
    assert trim_edges("\t\n Гул  подшипника \n") == "Гул  подшипника"
    assert trim_edges("внутри  пробелы") == "внутри  пробелы"
    assert trim_edges("") == ""
    body = {"description": "  Течь  ", "comment": " ок ", "priority": "high"}
    out = normalize_text_dict(body)
    assert out["description"] == "Течь"
    assert out["comment"] == "ок"
    assert out["priority"] == "high"
    nested = {"form": {"work_done_text": "  Заменил  ", "comment": " x "}}
    assert normalize_text_dict(nested)["form"] == {"work_done_text": "Заменил", "comment": "x"}


def test_create_wo_trims_and_validates():
    from app.routers.work_orders import CreateWO

    due = datetime.now(UTC) + timedelta(hours=2)
    m = CreateWO(
        kind="planned",
        description="  Проверка контрактов  ",
        equipment_id=uuid.uuid4(),
        assignee_id=uuid.uuid4(),
        priority="normal",
        due_at=due,
        comment="  extra  ",
    )
    assert m.description == "Проверка контрактов"
    assert m.comment == "extra"


def test_transition_maps_cover_lifecycle():
    from app.routers.work_orders import MASTER_TRANSITIONS, TERMINAL_DONE, WORKER_TRANSITIONS

    assert WORKER_TRANSITIONS["accept"] == ["issued", "queued"]
    assert WORKER_TRANSITIONS["complete"] == ["in_progress"]
    assert "close" in MASTER_TRANSITIONS and "cancel" in MASTER_TRANSITIONS
    assert TERMINAL_DONE >= {"done", "ai_review", "closed", "cancelled"}
    # complete недоступен до in_progress — сервер вернёт 409, а не сменит статус
    assert "issued" not in WORKER_TRANSITIONS["start"]


@pytest.mark.parametrize(
    "status,expected",
    [
        ("issued", ["accept", "queue", "reject"]),
        ("queued", ["accept", "reject"]),
        ("accepted", ["start", "reject"]),
        ("in_progress", ["pause", "complete"]),
        ("paused", ["resume"]),
        ("rework", ["start"]),
        ("rejected", []),
        ("done", []),
        ("ai_review", []),
        ("closed", []),
        ("cancelled", []),
    ],
)
def test_worker_action_policy_matches_transition_contract(status, expected):
    from app.deps import allowed_actions
    from app.routers.work_orders import WORKER_TRANSITIONS

    worker = {"role": "worker", "id": "worker-1", "crewId": "crew-1"}
    order = {"status": status, "assignee_id": "worker-1", "crew_id": None}
    actions = allowed_actions(worker, order)

    assert actions == expected
    assert all(status in WORKER_TRANSITIONS[action] for action in actions)


@pytest.mark.parametrize(
    "status,expected",
    [
        ("issued", ["reassign", "cancel", "change_priority"]),
        ("queued", ["reassign", "cancel", "change_priority"]),
        ("accepted", ["reassign", "cancel", "change_priority"]),
        ("paused", ["reassign", "cancel", "change_priority"]),
        ("rejected", ["reassign", "cancel", "change_priority"]),
        ("in_progress", []),
        ("rework", ["close"]),
        ("ai_review", ["close", "return_to_rework"]),
        ("done", []),
        ("closed", []),
        ("cancelled", []),
    ],
)
def test_master_action_policy_matches_transition_contract(status, expected):
    from app.deps import allowed_actions
    from app.routers.work_orders import MASTER_TRANSITIONS

    master = {"role": "master", "areaIds": ["area-1"]}
    order = {"status": status, "area_id": "area-1"}
    actions = allowed_actions(master, order)

    assert actions == expected
    assert all(status in MASTER_TRANSITIONS[action] for action in actions)


@pytest.mark.parametrize("role", ["manager", "admin"])
@pytest.mark.parametrize("status", ["issued", "in_progress", "ai_review", "closed"])
def test_read_only_roles_never_receive_mutation_actions(role, status):
    from app.deps import allowed_actions

    user = {"role": role, "id": "worker-1", "crewId": "crew-1", "areaIds": ["area-1"]}
    order = {"status": status, "assignee_id": "worker-1", "crew_id": "crew-1", "area_id": "area-1"}
    assert allowed_actions(user, order) == []


@pytest.mark.parametrize(
    "user,order",
    [
        (
            {"role": "worker", "id": "worker-2", "crewId": None},
            {"status": "issued", "assignee_id": "worker-1", "crew_id": None},
        ),
        (
            {"role": "worker", "id": "worker-2", "crewId": "crew-2"},
            {"status": "issued", "assignee_id": "worker-1", "crew_id": "crew-1"},
        ),
        ({"role": "master", "areaIds": ["area-2"]}, {"status": "issued", "area_id": "area-1"}),
    ],
)
def test_action_policy_denies_other_worker_crew_and_area(user, order):
    from app.deps import allowed_actions

    assert allowed_actions(user, order) == []


@pytest.mark.parametrize(
    "status",
    [
        "issued",
        "accepted",
        "queued",
        "rejected",
        "in_progress",
        "paused",
        "rework",
        "done",
        "ai_review",
        "closed",
        "cancelled",
    ],
)
@pytest.mark.parametrize("relation", ["assignee", "crew_member", "outsider"])
def test_worker_permissions_cover_every_lifecycle_state_and_assignment_relation(status, relation):
    from app.deps import allowed_actions
    from app.routers.work_orders import WORKER_TRANSITIONS

    worker = {"role": "worker", "id": "worker-1", "crewId": "crew-1"}
    order = {"status": status, "assignee_id": "worker-2", "crew_id": "crew-2"}
    if relation == "assignee":
        order.update(assignee_id="worker-1", crew_id=None)
    elif relation == "crew_member":
        order.update(crew_id="crew-1")

    actions = allowed_actions(worker, order)
    if relation == "outsider":
        assert actions == []
        return

    expected = [action for action, states in WORKER_TRANSITIONS.items() if status in states]
    assert set(actions) == set(expected)
    assert all(status in WORKER_TRANSITIONS[action] for action in actions)


def test_request_hash_stable_after_trim():
    from app.routers.work_orders import _request_hash

    h1 = _request_hash("POST", "/work-orders", {"description": "Течь"})
    h2 = _request_hash("POST", "/work-orders", {"description": "Течь"})
    h3 = _request_hash("POST", "/work-orders", {"description": "Течь "})
    assert h1 == h2
    assert h1 != h3  # напоминание: фронт обязан тримить ДО хэша/ключа


def test_bilingual_catalogue_cols():
    from app.routers.admin import CATALOGUES

    for ctype in ("areas", "equipment", "crews", "fault-codes", "materials", "reasons"):
        assert "name_kk" in CATALOGUES[ctype]["cols"], ctype
        assert "name_kk" in CATALOGUES[ctype]["search"], ctype


def test_mock_ai_deterministic_ru_kk():
    import asyncio

    import app.config as cfg
    from app.ai.gateway import inspect_completion, recommend_worker

    cfg.settings.AI_MODE = "mock"
    bad = asyncio.run(inspect_completion({"missing": ["work_done_text"], "duplicate": True}, "ru"))
    assert bad["verdict"] == "FAIL" and bad["provider"] == "mock"
    bad_kk = asyncio.run(
        inspect_completion({"missing": ["work_done_text"], "duplicate": True}, "kk")
    )
    assert bad_kk["verdict"] == "FAIL" and "Пысықтау" in bad_kk["explanation"]
    good = asyncio.run(inspect_completion({}, "ru"))
    assert good["verdict"] == "PASS"
    rec = asyncio.run(recommend_worker({"candidates": [{"employee_id": "x"}]}, "kk"))
    assert rec["recommended_worker_id"] == "x"
    empty = asyncio.run(recommend_worker({"candidates": []}, "ru"))
    assert empty["recommended_worker_id"] is None and empty["confidence"] < 0.5

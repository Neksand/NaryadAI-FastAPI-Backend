"""The stale-version response includes the version needed for a safe retry."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def env(monkeypatch):
    import app.services.background as background
    from app import db
    from app.main import create_app

    async def idle_loop():
        await asyncio.Future()

    monkeypatch.setattr(background, "outbox_loop", idle_loop)
    monkeypatch.setattr(background, "deadline_loop", idle_loop)
    monkeypatch.setattr(background, "insights_loop", idle_loop)
    db._pool = None

    app = create_app()
    with TestClient(app) as client:
        master = _auth(client, "master1", "3333")
        worker = _auth(client, "worker01", "1001")
        yield {
            "client": client,
            "mh": {"Authorization": f"Bearer {master['access_token']}"},
            "wh": {"Authorization": f"Bearer {worker['access_token']}"},
            "master": master,
            "worker": worker,
        }


def _auth(client: TestClient, login: str, pin: str) -> dict:
    response = client.post("/api/v1/auth/login", json={"login": login, "pin": pin})
    assert response.status_code == 200, response.text
    return response.json()


def _new_order(env) -> str:
    client, mh, wh = env["client"], env["mh"], env["wh"]
    me = client.get("/api/v1/me", headers=wh)
    assert me.status_code == 200, me.text
    equipment = client.get("/api/v1/dict/equipment", headers=mh)
    assert equipment.status_code == 200, equipment.text
    candidates = [
        item for item in equipment.json()["items"] if item["area_id"] in me.json()["area_ids"]
    ]
    assert candidates, "no equipment in worker area"
    response = client.post(
        "/api/v1/work-orders",
        headers={**mh, "Idempotency-Key": str(uuid.uuid4())},
        json={
            "kind": "planned",
            "description": "Проверка конфликта версии",
            "equipment_id": candidates[0]["id"],
            "assignee_id": env["worker"]["user"]["id"],
            "priority": "normal",
            "due_at": (datetime.now(UTC) + timedelta(hours=4)).isoformat(),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _transition(client: TestClient, headers: dict, order_id: str, action: str, version: int):
    return client.post(
        f"/api/v1/work-orders/{order_id}/transitions",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"action": action, "expected_version": version},
    )


def test_stale_version_returns_current_version_and_allows_retry(env):
    client, wh = env["client"], env["wh"]
    order_id = _new_order(env)

    accepted = _transition(client, wh, order_id, "accept", 1)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["version"] == 2

    stale = _transition(client, wh, order_id, "start", 1)
    assert stale.status_code == 409, stale.text
    error = stale.json()["error"]
    assert error["code"] == "version_conflict"
    current_version = error["details"]["current_version"]
    assert current_version == 2

    retry = _transition(client, wh, order_id, "start", current_version)
    assert retry.status_code == 200, retry.text
    assert retry.json()["version"] == current_version + 1

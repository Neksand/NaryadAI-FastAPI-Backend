"""End-to-end demo flow (§11 steps 2-8): master issues -> worker executes ->
AI review -> master closes -> reports. Requires local infra (postgres/redis/moto S3)."""
import io
import time
import uuid

from fastapi.testclient import TestClient


def _jpeg(seed: str = "") -> bytes:
    import random
    from PIL import Image
    rnd = random.Random(seed)
    img = Image.new("RGB", (64, 64), (200, 30, 30))
    px = img.load()
    for _ in range(120):
        px[rnd.randrange(64), rnd.randrange(64)] = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _auth(client: TestClient, login: str, pin: str) -> dict:
    r = client.post("/api/v1/auth/login", json={"login": login, "pin": pin})
    assert r.status_code == 200, r.text
    return r.json()


def test_demo_flow():
    from app.main import create_app
    with TestClient(create_app()) as client:
        master = _auth(client, "master1", "3333")
        mh = {"Authorization": f"Bearer {master['access_token']}"}
        worker = _auth(client, "worker01", "1001")
        wh = {"Authorization": f"Bearer {worker['access_token']}"}

        areas = client.get("/api/v1/dict/areas", headers=mh).json()["items"]
        assert len(areas) >= 4
        equip = client.get("/api/v1/dict/equipment", headers=mh).json()["items"]
        assert len(equip) >= 25
        first_eq = equip[0]
        worker_id = worker["user"]["id"]

        # issue unplanned order to worker01 on equipment's area
        from datetime import datetime, timedelta, timezone
        due = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
        key = str(uuid.uuid4())
        # pick equipment in worker01's area: fetch worker areas via /me
        me = client.get("/api/v1/me", headers=wh).json()
        eq_in_area = [e for e in equip if e["area_id"] in me["area_ids"]]
        assert eq_in_area, "no equipment in worker area"
        eq = eq_in_area[0]
        r = client.post("/api/v1/work-orders", headers={**mh, "Idempotency-Key": key},
                        json={"kind": "unplanned", "description": "Течь масла, гул подшипника",
                              "equipment_id": eq["id"], "assignee_id": worker_id,
                              "priority": "high", "due_at": due})
        assert r.status_code == 201, r.text
        oid = r.json()["id"]

        # worker: accept -> start
        for action in ("accept", "start"):
            r = client.post(f"/api/v1/work-orders/{oid}/transitions",
                            headers={**wh, "Idempotency-Key": str(uuid.uuid4())},
                            json={"action": action})
            assert r.status_code == 200, (action, r.text)

        # after-photo + complete
        r = client.post(f"/api/v1/work-orders/{oid}/photos", headers=wh,
                        files={"file": ("after.jpg", _jpeg(str(uuid.uuid4())), "image/jpeg")}, data={"kind": "after"})
        assert r.status_code == 200, r.text
        faults = client.get("/api/v1/dict/fault-codes", headers=mh).json()["items"]
        r = client.post(f"/api/v1/work-orders/{oid}/transitions",
                        headers={**wh, "Idempotency-Key": str(uuid.uuid4())},
                        json={"action": "complete",
                              "form": {"work_done_text": "Заменён подшипник, течь устранена",
                                       "fault_code_id": faults[0]["id"], "materials": []}})
        assert r.status_code == 200, r.text
        assert r.json()["to"] == "ai_review"

        # wait for background ai_review (outbox loop)
        review = None
        for _ in range(30):
            time.sleep(1)
            r = client.get(f"/api/v1/work-orders/{oid}/ai-review", headers=mh)
            if r.status_code == 200:
                review = r.json()
                break
        assert review is not None, "ai_review never appeared"
        assert review["verdict"] in ("accepted", "accepted_with_remarks", "needs_rework", "needs_master_review")
        assert review.get("photo_score") in (1, 2, 3, 4, 5)

        # master closes (override when AI demands rework, agree otherwise)
        if review["verdict"] == "needs_rework":
            r = client.post(f"/api/v1/work-orders/{oid}/transitions",
                            headers={**mh, "Idempotency-Key": str(uuid.uuid4())},
                            json={"action": "close", "decision": "override",
                                  "score": 70, "comment": "Принято с замечаниями после проверки"})
            assert r.status_code == 200, r.text
            assert r.json()["to"] == "closed"
        else:
            r = client.post(f"/api/v1/work-orders/{oid}/review/decision",
                            headers={**mh, "Idempotency-Key": str(uuid.uuid4())},
                            json={"decision": "agree_ai"})
            assert r.status_code == 200, r.text

        # reports + analytics + board
        assert client.get("/api/v1/shift/board", headers=mh).status_code == 200
        assert client.get("/api/v1/analytics/dashboard", headers=mh).status_code == 200
        assert client.get(f"/api/v1/work-orders/{oid}/report?audience=master", headers=mh).status_code == 200
        r = client.post("/api/v1/analytics/insights/generate", headers=mh)
        assert r.status_code == 200, r.text
        r = client.post("/api/v1/reports/export", headers={**mh, "Idempotency-Key": str(uuid.uuid4())},
                        json={"type": "shift", "format": "xlsx", "filters": {}})
        assert r.status_code in (200, 202), r.text

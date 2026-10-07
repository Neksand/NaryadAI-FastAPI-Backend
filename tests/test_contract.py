"""Contract tests per spec §41: authz, transitions, notifications, WS, mock AI,
localization, uploads, analytics. Requires local infra."""
import uuid

from fastapi.testclient import TestClient


def _client():
    from app.main import create_app
    return TestClient(create_app())


def _login(c: TestClient, login: str, pin: str) -> dict:
    r = c.post("/api/v1/auth/login", json={"login": login, "pin": pin})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["user"]["language"] in ("ru", "kk")
    return body


def test_auth_and_roles():
    with _client() as c:
        # invalid PIN
        r = c.post("/api/v1/auth/login", json={"login": "master1", "pin": "0000"})
        assert r.status_code == 401
        assert r.json()["error"]["translation_key"] == "error.unauthorized"
        # no token
        assert c.get("/api/v1/me").status_code == 401
        m = _login(c, "master1", "3333")
        w = _login(c, "worker01", "1001")
        mh = {"Authorization": f"Bearer {m['access_token']}"}
        wh = {"Authorization": f"Bearer {w['access_token']}"}
        # worker cannot do master-only operation
        r = c.post("/api/v1/dict/areas", headers=wh, json={"name": "X", "code": "X1"})
        assert r.status_code == 403
        # worker cannot read чужой order list beyond own (master-scoped endpoint forbidden for worker)
        assert c.get("/api/v1/equipment/00000000-0000-0000-0000-000000000000/history", headers=wh).status_code in (403, 404)
        # me returns language
        assert c.get("/api/v1/auth/me", headers=mh).json()["language"] == "ru"


def test_language_preferences():
    with _client() as c:
        w = _login(c, "worker02", "1002")
        wh = {"Authorization": f"Bearer {w['access_token']}"}
        r = c.patch("/api/v1/users/me/preferences", headers=wh, json={"language": "kk"})
        assert r.status_code == 200 and r.json()["data"]["language"] == "kk"
        assert c.get("/api/v1/auth/me", headers=wh).json()["language"] == "kk"
        r = c.patch("/api/v1/users/me/preferences", headers=wh, json={"language": "de"})
        assert r.status_code == 422
        # restore
        c.patch("/api/v1/users/me/preferences", headers=wh, json={"language": "ru"})


def test_transition_validation_and_actions():
    with _client() as c:
        m = _login(c, "master1", "3333")
        w = _login(c, "worker03", "1003")
        mh = {"Authorization": f"Bearer {m['access_token']}"}
        wh = {"Authorization": f"Bearer {w['access_token']}"}
        me = c.get("/api/v1/me", headers=wh).json()
        eq = [e for e in c.get("/api/v1/dict/equipment", headers=mh).json()["items"]
              if e["area_id"] in me["area_ids"]][0]
        from datetime import datetime, timedelta, timezone
        due = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
        r = c.post("/api/v1/work-orders", headers={**mh, "Idempotency-Key": str(uuid.uuid4())},
                   json={"kind": "planned", "description": "Проверка контрактов",
                         "equipment_id": eq["id"], "assignee_id": w["user"]["id"],
                         "priority": "normal", "due_at": due})
        assert r.status_code == 201, r.text
        oid = r.json()["id"]
        # invalid: start before accept
        r = c.post(f"/api/v1/work-orders/{oid}/start", headers=wh, json={})
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "invalid_transition" and err["translation_key"] == "error.invalid_work_order_state"
        # valid path via per-action endpoints
        assert c.post(f"/api/v1/work-orders/{oid}/accept", headers=wh, json={}).status_code == 200
        assert c.post(f"/api/v1/work-orders/{oid}/start", headers=wh, json={}).status_code == 200
        assert c.post(f"/api/v1/work-orders/{oid}/pause", headers=wh,
                      json={"reason_code": "waiting_spare"}).status_code == 200
        # resume is a separate action endpoint
        assert c.post(f"/api/v1/work-orders/{oid}/resume", headers=wh, json={}).status_code == 200
        # my/active/history
        assert c.get("/api/v1/work-orders/my", headers=wh).status_code == 200
        assert c.get("/api/v1/work-orders/my/active", headers=wh).status_code == 200
        assert c.get("/api/v1/work-orders/my/history", headers=wh).status_code == 200
        # every transition created an event
        ev = c.get(f"/api/v1/work-orders/{oid}/events", headers=wh).json()["items"]
        assert len([e for e in ev if e["action"] in ("accept", "start", "pause", "resume")]) == 4


def test_notifications_persisted_in_app():
    with _client() as c:
        m = _login(c, "master1", "3333")
        mh = {"Authorization": f"Bearer {m['access_token']}"}
        w = _login(c, "worker04", "1004")
        wh = {"Authorization": f"Bearer {w['access_token']}"}
        me = c.get("/api/v1/me", headers=wh).json()
        eq = [e for e in c.get("/api/v1/dict/equipment", headers=mh).json()["items"]
              if e["area_id"] in me["area_ids"]][0]
        from datetime import datetime, timedelta, timezone
        due = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
        r = c.post("/api/v1/work-orders", headers={**mh, "Idempotency-Key": str(uuid.uuid4())},
                   json={"kind": "planned", "description": "Уведомления в приложении",
                         "equipment_id": eq["id"], "assignee_id": w["user"]["id"],
                         "priority": "normal", "due_at": due})
        assert r.status_code == 201, r.text
        import time
        notes = []
        for _ in range(20):
            time.sleep(1)
            notes = c.get("/api/v1/notifications?unread_only=true", headers=wh).json()["data"]
            if notes:
                break
        assert notes, "no persistent notification for assignee"
        assert notes[0]["type"] == "WORK_ORDER_ASSIGNED"
        assert "наряд" in notes[0]["message"].lower() or "наряд" in notes[0]["title"].lower()
        nid = notes[0]["id"]
        assert c.post(f"/api/v1/notifications/{nid}/read", headers=wh).status_code == 200


def test_mock_ai_localized():
    import asyncio
    from app.ai.gateway import inspect_completion, recommend_worker
    import app.config as cfg
    cfg.settings.AI_MODE = "mock"
    bad = asyncio.run(inspect_completion({"missing": ["work_done_text"], "duplicate": True}, "ru"))
    assert bad["verdict"] == "FAIL" and bad["provider"] == "mock"
    bad_kk = asyncio.run(inspect_completion({"missing": ["work_done_text"], "duplicate": True}, "kk"))
    assert bad_kk["verdict"] == "FAIL" and "Пысықтау" in bad_kk["explanation"]
    rec = asyncio.run(recommend_worker({"candidates": [{"employee_id": "x"}]}, "kk"))
    assert rec["recommended_worker_id"] == "x"


def test_upload_validation():
    with _client() as c:
        m = _login(c, "master1", "3333")
        mh = {"Authorization": f"Bearer {m['access_token']}"}
        r = c.post("/api/v1/photos", headers=mh, files={"file": ("x.txt", b"not an image", "text/plain")},
                   data={"kind": "before"})
        assert r.status_code == 422


def test_analytics_contract():
    with _client() as c:
        m = _login(c, "manager1", "2222")
        mh = {"Authorization": f"Bearer {m['access_token']}"}
        for path in ("/api/v1/analytics/overview", "/api/v1/analytics/work-orders",
                     "/api/v1/analytics/workers", "/api/v1/analytics/equipment",
                     "/api/v1/analytics/downtime", "/api/v1/analytics/ai-insights",
                     "/api/v1/sites", "/api/v1/teams", "/api/v1/equipment",
                     "/api/v1/materials", "/api/v1/fault-codes", "/api/v1/workers"):
            r = c.get(path, headers=mh)
            assert r.status_code == 200, path
        assert c.get("/api/v1/health", headers=mh).status_code in (200, 404)
        assert len(c.get("/api/v1/materials", headers=mh).json()["data"]) >= 40


def test_analytics_ratings_quality_faults_trends():
    with _client() as c:
        m = _login(c, "manager1", "2222")
        mh = {"Authorization": f"Bearer {m['access_token']}"}
        r = c.get("/api/v1/analytics/ratings?group_by=employee", headers=mh).json()["data"]
        assert len(r) > 5
        top = r[0]
        assert 0 <= top["rating"] <= 100 and "formula" in top
        assert any(x["low_data"] is False for x in r)
        q = c.get("/api/v1/analytics/quality", headers=mh).json()["data"]
        assert q["reviews"] > 100 and 0 <= (q["ai_pass_rate"] or 0) <= 1
        assert q["rework_rate"] is not None
        f = c.get("/api/v1/analytics/faults", headers=mh).json()["data"]
        assert f and any(x["code"] == "М-02" and x["n"] >= 50 for x in f)
        t = c.get("/api/v1/analytics/trends?days=7", headers=mh).json()["data"]
        assert len(t) == 7 and "created" in t[0]
        g = c.get("/api/v1/analytics/ratings?group_by=crew", headers=mh).json()["data"]
        assert len(g) >= 1


def test_photos_list_delete_and_ai_inspect():
    import io
    with _client() as c:
        m = _login(c, "master1", "3333")
        mh = {"Authorization": f"Bearer {m['access_token']}"}
        w = _login(c, "worker05", "1005")
        wh = {"Authorization": f"Bearer {w['access_token']}"}
        me = c.get("/api/v1/me", headers=wh).json()
        eq = [e for e in c.get("/api/v1/dict/equipment", headers=mh).json()["items"]
              if e["area_id"] in me["area_ids"]][0]
        from datetime import datetime, timedelta, timezone
        due = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
        r = c.post("/api/v1/work-orders", headers={**mh, "Idempotency-Key": str(uuid.uuid4())},
                   json={"kind": "planned", "description": "Фото-контракт",
                         "equipment_id": eq["id"], "assignee_id": w["user"]["id"],
                         "priority": "normal", "due_at": due})
        oid = r.json()["id"]
        # standalone pending photo then delete
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (32, 32), (10, 200, 10)).save(buf, format="PNG")
        r = c.post("/api/v1/photos", headers=mh,
                   files={"file": ("b.png", buf.getvalue(), "image/png")}, data={"kind": "before"})
        pid = r.json()["id"]
        assert c.delete(f"/api/v1/photos/{pid}", headers=mh).json()["data"]["deleted"] is True
        # recommend-worker + inspect contracts
        rec = c.post(f"/api/v1/ai/work-orders/{oid}/recommend-worker", headers=mh).json()["data"]
        assert "recommended_worker_id" in rec and "confidence" in rec
        insp = c.post(f"/api/v1/ai/work-orders/{oid}/inspect", headers=mh).json()["data"]
        assert insp["status"] in ("COMPLETED", "FAILED")
        got = c.get(f"/api/v1/ai/work-orders/{oid}/inspection", headers=mh).json()["data"]
        assert got["job"] is not None


def test_websocket_subscribe_and_push():
    with _client() as c:
        m = _login(c, "master1", "3333")
        from urllib.parse import quote
        with c.websocket_connect(f"/api/ws?token={quote(m['access_token'])}") as ws:
            ws.send_json({"op": "subscribe", "channels": ["shift:current", f"user:{m['user']['id']}", "manager"]})
            msg = ws.receive_json()
            assert msg["type"] == "SUBSCRIBED"
            assert "shift:current" in msg["channels"]
            ws.send_json({"op": "ping"})
            assert ws.receive_json()["type"] == "PONG"

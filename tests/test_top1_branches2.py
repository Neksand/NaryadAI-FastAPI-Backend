"""Top-1 branches2: pure-unit cover for analytics/work_orders/photos/background/deps.

No DB/Redis/S3. Sync tests only (async via asyncio.run).
"""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest


def run(coro):
    return asyncio.run(coro)


def _uid():
    return str(uuid.uuid4())


# --- analytics.operational_economics (lines 114-118, master scope + period) ---


class _EcoPool:
    def __init__(self, mats=None):
        self.mats = mats if mats is not None else []

    async def fetchrow(self, query, *args):
        return {
            "total_orders": 2,
            "completed_orders": 1,
            "unplanned_orders": 0,
            "norm_hours": 1.5,
            "completed_without_norm": 0,
            "downtime_hours": 0.0,
        }

    async def fetch(self, query, *args):
        return list(self.mats)


def test_operational_economics_master_with_period(monkeypatch):
    import app.routers.analytics as an

    async def fake_pool():
        return _EcoPool()

    monkeypatch.setattr(an, "get_pool", fake_pool)
    body = an.OperationalEconomicsRequest(
        **{"from": "2026-01-01T00:00:00+00:00", "to": "2026-02-01T00:00:00+00:00"}
    )
    user = {"id": _uid(), "role": "master", "areaIds": ["a1"]}
    out = run(an.operational_economics(body, user))
    assert out["data"]["period"]["from"].startswith("2026-01-01")
    assert out["data"]["period"]["to"].startswith("2026-02-01")


def test_operational_economics_manager_without_period(monkeypatch):
    import app.routers.analytics as an

    async def fake_pool():
        return _EcoPool(
            mats=[{"id": "m1", "name": "X", "unit": "pcs", "quantity": 2.0}],
        )

    monkeypatch.setattr(an, "get_pool", fake_pool)
    body = an.OperationalEconomicsRequest()
    user = {"id": _uid(), "role": "manager", "areaIds": []}
    out = run(an.operational_economics(body, user))
    assert out["data"]["period"] == {"from": None, "to": None}


# --- analytics.areas (lines 210-214) ---


def test_areas_lists_serialized_ids(monkeypatch):
    import app.routers.analytics as an

    aid = uuid.uuid4()

    class _Pool:
        async def fetch(self, query, *args):
            return [{"id": aid, "name": "A", "orders": 3}]

        async def fetchrow(self, query, *args):
            raise AssertionError("unexpected fetchrow")

    async def fake_pool():
        return _Pool()

    monkeypatch.setattr(an, "get_pool", fake_pool)
    out = run(an.areas({"id": _uid(), "role": "manager", "areaIds": []}))
    assert out["items"] == [{"id": str(aid), "name": "A", "orders": 3}]


# --- analytics.insights (lines 281-296, status validation branches) ---


def test_insights_rejects_bad_status():
    import app.routers.analytics as an
    from app.errors import AppError

    with pytest.raises(AppError) as exc:
        run(an.insights(status="bogus", user={"id": _uid(), "role": "manager"}))
    assert exc.value.status_code == 422


def test_insights_none_status_returns_rows(monkeypatch):
    import app.routers.analytics as an

    iid = uuid.uuid4()

    class _Pool:
        async def fetch(self, query, *args):
            assert args == (None,)
            return [{"id": iid, "status": "new"}]

    async def fake_pool():
        return _Pool()

    monkeypatch.setattr(an, "get_pool", fake_pool)
    out = run(an.insights(status=None, user={"id": _uid(), "role": "manager"}))
    assert out["items"][0]["id"] == str(iid)


def test_insights_valid_status_passes_filter(monkeypatch):
    import app.routers.analytics as an

    class _Pool:
        async def fetch(self, query, *args):
            assert args == ("seen",)
            return []

    async def fake_pool():
        return _Pool()

    monkeypatch.setattr(an, "get_pool", fake_pool)
    out = run(an.insights(status="seen", user={"id": _uid(), "role": "admin"}))
    assert out == {"items": []}


# --- analytics.generate (lines 366-375, pool acquire + transaction) ---


def test_generate_returns_headlines(monkeypatch):
    import app.routers.analytics as an
    import app.services.insights as ins

    class _Tx:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *args):
            return False

    class _Conn:
        def transaction(self):
            return _Tx()

    class _Acq:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *args):
            return False

    class _Pool:
        def acquire(self):
            return _Acq()

    async def fake_pool():
        return _Pool()

    async def fake_gen(conn):
        return ["headline-1", "headline-2"]

    monkeypatch.setattr(an, "get_pool", fake_pool)
    monkeypatch.setattr(ins, "generate_insights", fake_gen)
    out = run(an.generate({"id": _uid(), "role": "manager"}))
    assert out == {"generated": 2, "headlines": ["headline-1", "headline-2"]}


# --- analytics.ratings (group branches + calc loop 425-452) ---


class _RatingsPool:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, query, *args):
        return list(self.rows)


def _ratings_pool(monkeypatch, rows):
    import app.routers.analytics as an

    async def fake_pool():
        return _RatingsPool(rows)

    monkeypatch.setattr(an, "get_pool", fake_pool)


def test_ratings_rejects_bad_group():
    import app.routers.analytics as an
    from app.errors import AppError

    with pytest.raises(AppError):
        run(an.ratings(group_by="team", user={"id": _uid(), "role": "manager"}))


def test_ratings_worker_crew_forbidden():
    import app.routers.analytics as an
    from app.errors import AppError

    user = {"id": _uid(), "role": "worker", "crewId": None}
    with pytest.raises(AppError) as exc:
        run(an.ratings(group_by="crew", user=user))
    assert exc.value.status_code == 403


def test_ratings_employee_loop_none_and_full(monkeypatch):
    import app.routers.analytics as an

    _ratings_pool(
        monkeypatch,
        [
            {
                "gid": "e1",
                "name": "A",
                "closed": 0,
                "total": 2,
                "quality": None,
                "on_time": None,
                "rework": None,
                "rejected": None,
            },
            {
                "gid": "e2",
                "name": "B",
                "closed": 40,
                "total": 45,
                "quality": 80.0,
                "on_time": 1.0,
                "rework": 0.0,
                "rejected": 0.0,
            },
        ],
    )
    out = run(an.ratings(group_by="employee", user={"id": _uid(), "role": "manager"}))
    assert out["group_by"] == "employee"
    assert out["data"][0]["id"] == "e2"
    assert out["data"][0]["low_data"] is False
    assert out["data"][1]["low_data"] is True
    assert out["data"][1]["quality"] == 0


def test_ratings_crew_group_for_worker_scope(monkeypatch):
    import app.routers.analytics as an

    _ratings_pool(
        monkeypatch,
        [
            {
                "gid": "c1",
                "name": "Crew",
                "closed": 3,
                "total": 5,
                "quality": 50.0,
                "on_time": 0.5,
                "rework": 2.0,
                "rejected": 3.0,
            },
        ],
    )
    user = {"id": _uid(), "role": "worker", "crewId": "crew-9"}
    out = run(an.ratings(group_by="employee", user=user))
    assert out["data"][0]["rework_rate"] == 1
    assert out["data"][0]["rejected"] == 1


def test_ratings_crew_group_by(monkeypatch):
    import app.routers.analytics as an

    _ratings_pool(
        monkeypatch,
        [
            {
                "gid": "c1",
                "name": "Crew",
                "closed": 6,
                "total": 6,
                "quality": 90.0,
                "on_time": 1.0,
                "rework": 0.0,
                "rejected": 0.0,
            },
        ],
    )
    out = run(an.ratings(group_by="crew", user={"id": _uid(), "role": "admin"}))
    assert out["group_by"] == "crew"
    assert out["data"][0]["low_data"] is False


# --- work_orders models: TransitionIn.client_at, PatchWO/ActionIn/CreateWO trims ---


def test_transition_in_full_fields_and_client_at():
    import app.routers.work_orders as wo

    body = wo.TransitionIn(
        action="complete",
        decision="agree_ai",
        score=80,
        reason_code="R1",
        comment="  hey  ",
        assignee_id=uuid.uuid4(),
        priority="high",
        client_at=datetime(2026, 1, 1, tzinfo=UTC),
        photo_ids=[uuid.uuid4()],
        form=wo.TransitionForm(work_done_text="  done  ", fault_code_id=uuid.uuid4()),
    )
    assert body.comment == "hey"
    assert body.client_at is not None
    assert body.form is not None and body.form.work_done_text == "done"


def test_create_and_action_trims_passthrough():
    import app.routers.work_orders as wo

    assert wo.CreateWO._trim("  a  ") == "a"
    assert wo.CreateWO._trim(123) == 123
    assert wo.TransitionForm._trim(None) is None
    assert wo.TransitionIn._trim("  b ") == "b"
    assert wo.PatchWO._trim("  c ") == "c"
    assert wo.ReviewDecision._trim(7) == 7
    assert wo.ActionIn._trim("  d  ") == "d"
    assert wo.ActionIn._trim(None) is None


def test_patch_and_review_models():
    import app.routers.work_orders as wo

    p = wo.PatchWO(priority="high", due_at=datetime(2026, 5, 1, tzinfo=UTC), comment=" x ")
    assert p.comment == "x" and p.priority == "high"
    assert wo.PatchWO().comment is None
    r = wo.ReviewDecision(decision="override", score=10, comment=" ok ")
    assert r.comment == "ok"
    a = wo.ActionIn(reason_code="R", comment=None)
    assert a.reason_code == "R"


# --- work_orders._load_full (line 183 not-found + full) ---


class _FullPool:
    def __init__(self, row):
        self.row = row

    async def fetchrow(self, query, *args):
        if "FROM work_orders w JOIN equipment" in query:
            return self.row
        if "FROM ai_reviews" in query:
            return None
        raise AssertionError(query)

    async def fetch(self, query, *args):
        if "FROM photos" in query:
            return []
        if "material_writeoffs" in query:
            return []
        raise AssertionError(query)


def test_load_full_missing_raises(monkeypatch):
    import app.routers.work_orders as wo
    from app.errors import AppError

    async def fake_pool():
        return _FullPool(None)

    monkeypatch.setattr(wo, "get_pool", fake_pool)
    with pytest.raises(AppError) as exc:
        run(wo._load_full(_uid()))
    assert exc.value.status_code == 404


def test_load_full_ok_with_conversions(monkeypatch):
    import app.routers.work_orders as wo

    oid, eid, aid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    async def fake_pool():
        return _FullPool(
            {
                "id": oid,
                "equipment_id": eid,
                "area_id": aid,
                "issued_at": datetime(2026, 1, 2, 3, 4, 5),
                "number": 9,
            }
        )

    monkeypatch.setattr(wo, "get_pool", fake_pool)
    out = run(wo._load_full(str(oid)))
    assert out["id"] == str(oid)
    assert out["issued_at"] == datetime(2026, 1, 2, 3, 4, 5).isoformat()
    assert out["photos"] == [] and out["materials"] == [] and out["ai_review"] is None


def test_request_hash_stable():
    import app.routers.work_orders as wo

    assert wo._request_hash("POST", "/work-orders", {"a": 1}) == wo._request_hash(
        "POST", "/work-orders", {"a": 1}
    )


# --- work_orders.list_orders (lines 246-287 + pagination) ---


class _ListPool:
    def __init__(self, rows):
        self.rows = rows
        self.last_sql = ""
        self.last_args = ()

    async def fetch(self, query, *args):
        self.last_sql = query
        self.last_args = args
        return list(self.rows)


def _wo_row(**over):
    base = {
        "id": uuid.uuid4(),
        "number": 1,
        "issued_at": datetime(2026, 1, 2, 3, 4, 5),
        "kind": "planned",
        "description": "d",
        "area_id": uuid.uuid4(),
        "equipment_id": uuid.uuid4(),
        "assignee_id": None,
        "crew_id": None,
        "priority": "normal",
        "due_at": datetime(2026, 2, 1),
        "status": "issued",
        "norm_minutes": None,
        "done_at": None,
        "closed_at": None,
    }
    base.update(over)
    return base


def _list_pool(monkeypatch, rows):
    import app.routers.work_orders as wo

    pool = _ListPool(rows)

    async def fake_pool():
        return pool

    monkeypatch.setattr(wo, "get_pool", fake_pool)
    return pool


def test_list_orders_bad_priority():
    import app.routers.work_orders as wo
    from app.errors import AppError

    with pytest.raises(AppError):
        run(wo.list_orders(request=None, priority="bogus", user={"role": "admin"}))


def test_list_orders_bad_status():
    import app.routers.work_orders as wo
    from app.errors import AppError

    with pytest.raises(AppError):
        run(wo.list_orders(request=None, status="bogus", user={"role": "admin"}))


def test_list_orders_bad_kind():
    import app.routers.work_orders as wo
    from app.errors import AppError

    with pytest.raises(AppError):
        run(wo.list_orders(request=None, kind="bogus", user={"role": "admin"}))


def test_list_orders_bad_cursor():
    import app.routers.work_orders as wo
    from app.errors import AppError

    with pytest.raises(AppError):
        run(wo.list_orders(request=None, cursor="!!!bad!!!", user={"role": "admin"}))


def test_list_orders_worker_scope(monkeypatch):
    import app.routers.work_orders as wo

    uid = _uid()
    pool = _list_pool(monkeypatch, [_wo_row(assignee_id=uid)])
    out = run(
        wo.list_orders(request=None, limit=50, user={"role": "worker", "id": uid, "crewId": None})
    )
    assert "w.assignee_id" in pool.last_sql
    assert out["items"][0]["allowed_actions"] == ["accept", "queue", "reject"]
    assert out["next_cursor"] is None


def test_list_orders_master_empty_areas(monkeypatch):
    import app.routers.work_orders as wo

    pool = _list_pool(monkeypatch, [])
    out = run(wo.list_orders(request=None, limit=50, user={"role": "master", "areaIds": []}))
    assert "false" in pool.last_sql
    assert out == {"items": [], "next_cursor": None}


def test_list_orders_master_filters_cursor_pagination(monkeypatch):
    import app.routers.work_orders as wo
    from app.deps import encode_cursor

    area = _uid()
    rows = [_wo_row(), _wo_row()]
    pool = _list_pool(monkeypatch, rows)
    cur = encode_cursor(_uid(), "2026-01-01T00:00:00+00:00")
    out = run(
        wo.list_orders(
            request=None,
            limit=1,
            cursor=cur,
            area_id=area,
            equipment_id=_uid(),
            assignee_id=_uid(),
            crew_id=_uid(),
            priority="high",
            status="issued",
            overdue=True,
            from_="2026-01-01",
            to="2026-03-01",
            kind="planned",
            user={"role": "master", "areaIds": [area]},
        )
    )
    assert len(out["items"]) == 1
    assert isinstance(out["next_cursor"], str)
    assert "w.area_id = ANY" in pool.last_sql


def test_list_orders_next_cursor_str_issued_at(monkeypatch):
    import app.routers.work_orders as wo

    rows = [_wo_row(issued_at="2026-01-02T03:04:05"), _wo_row()]
    _list_pool(monkeypatch, rows)
    out = run(wo.list_orders(request=None, limit=1, user={"role": "admin"}))
    assert isinstance(out["next_cursor"], str)


# --- work_orders.create/transition validation (no DB) ---


def test_create_order_due_in_past():
    import app.routers.work_orders as wo
    from app.errors import AppError

    body = wo.CreateWO(
        kind="planned",
        description="test order",
        equipment_id=uuid.uuid4(),
        assignee_id=uuid.uuid4(),
        priority="normal",
        due_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(AppError):
        run(body and wo.create_order(body, request=None, user={"role": "master"}))


def test_create_order_needs_exactly_one_executor():
    import app.routers.work_orders as wo
    from app.errors import AppError

    both = wo.CreateWO(
        kind="planned",
        description="test order",
        equipment_id=uuid.uuid4(),
        assignee_id=uuid.uuid4(),
        crew_id=uuid.uuid4(),
        priority="normal",
        due_at=datetime(2030, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(AppError):
        run(wo.create_order(both, request=None, user={"role": "master"}))
    neither = wo.CreateWO(
        kind="planned",
        description="test order",
        equipment_id=uuid.uuid4(),
        priority="normal",
        due_at=datetime(2030, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(AppError):
        run(wo.create_order(neither, request=None, user={"role": "master"}))


class _StubRequest:
    headers = {}


def _mock_order_access(monkeypatch):
    import app.routers.work_orders as wo

    async def fake_access(user, order_id):
        return {}

    monkeypatch.setattr(wo, "assert_can_read_order", fake_access)


def test_transition_worker_validations(monkeypatch):
    import app.routers.work_orders as wo
    from app.errors import AppError

    _mock_order_access(monkeypatch)
    user = {"role": "worker", "id": _uid(), "crewId": None}
    with pytest.raises(AppError):
        run(wo.transition(uuid.uuid4(), wo.TransitionIn(action="reject"), _StubRequest(), user))
    with pytest.raises(AppError):
        run(wo.transition(uuid.uuid4(), wo.TransitionIn(action="pause"), _StubRequest(), user))
    with pytest.raises(AppError):
        run(wo.transition(uuid.uuid4(), wo.TransitionIn(action="complete"), _StubRequest(), user))


def test_transition_master_validations(monkeypatch):
    import app.routers.work_orders as wo
    from app.errors import AppError

    _mock_order_access(monkeypatch)
    user = {"role": "master", "id": _uid(), "areaIds": []}
    with pytest.raises(AppError):
        run(wo.transition(uuid.uuid4(), wo.TransitionIn(action="reassign"), _StubRequest(), user))
    with pytest.raises(AppError):
        run(
            wo.transition(
                uuid.uuid4(), wo.TransitionIn(action="change_priority"), _StubRequest(), user
            )
        )
    with pytest.raises(AppError):
        run(wo.transition(uuid.uuid4(), wo.TransitionIn(action="close"), _StubRequest(), user))
    with pytest.raises(AppError):
        run(wo.transition(uuid.uuid4(), wo.TransitionIn(action="cancel"), _StubRequest(), user))


# --- photos._exif_taken_at success (lines 56-57) ---


def test_exif_taken_at_primary_tag(monkeypatch):
    import PIL.Image

    from app.routers.photos import _exif_taken_at

    class _Img:
        def getexif(self):
            return {36867: "2026:01:02 03:04:05"}

    monkeypatch.setattr(PIL.Image, "open", lambda *args, **kwargs: _Img())
    assert _exif_taken_at(b"fake") == datetime(2026, 1, 2, 3, 4, 5)


def test_exif_taken_at_fallback_tag(monkeypatch):
    import PIL.Image

    from app.routers.photos import _exif_taken_at

    class _Img:
        def getexif(self):
            return {306: "2025:05:06 07:08:09"}

    monkeypatch.setattr(PIL.Image, "open", lambda *args, **kwargs: _Img())
    assert _exif_taken_at(b"fake") == datetime(2025, 5, 6, 7, 8, 9)


def test_exif_taken_at_empty_returns_none(monkeypatch):
    import PIL.Image

    from app.routers.photos import _exif_taken_at

    class _Img:
        def getexif(self):
            return {}

    monkeypatch.setattr(PIL.Image, "open", lambda *args, **kwargs: _Img())
    assert _exif_taken_at(b"fake") is None


# --- photos._parse_client_taken_at (lines 86, 112, zone bounds) ---


def test_parse_taken_at_empty_returns_none():
    from app.routers.photos import _parse_client_taken_at

    assert _parse_client_taken_at(None) is None
    assert _parse_client_taken_at("") is None


def test_parse_taken_at_out_of_range():
    from app.errors import AppError
    from app.routers.photos import _parse_client_taken_at

    with pytest.raises(AppError):
        _parse_client_taken_at("1800-01-01T00:00:00Z")
    future = datetime.now(UTC).year + 2
    with pytest.raises(AppError):
        _parse_client_taken_at(f"{future}-01-01T00:00:00Z")


def test_parse_taken_at_zone_bounds():
    from app.errors import AppError
    from app.routers.photos import _parse_client_taken_at

    ok = _parse_client_taken_at("2026-10-01T10:00:00+14:00")
    assert ok is not None
    with pytest.raises(AppError):
        _parse_client_taken_at("2026-10-01T10:00:00+14:01")
    naive = _parse_client_taken_at("2026-10-01T10:00:00")
    assert naive is not None and naive.tzinfo is not None


# --- photos._create_photo (413, standalone, forbidden 141-143, dup, limit, cleanup) ---

_JPEG = bytes([0xFF, 0xD8, 0xFF]) + b"0" * 64


class _PhotoPool:
    def __init__(self, dup=None, near=None, count=0, insert_id=None, fail_insert=False):
        self.dup = dup
        self.near = near
        self.count = count
        self.insert_id = insert_id or str(uuid.uuid4())
        self.fail_insert = fail_insert

    async def fetchrow(self, query, *args):
        if "AND sha256" in query:
            return self.dup
        if "phash IS NOT NULL" in query:
            return self.near
        if "INSERT INTO photos" in query:
            if self.fail_insert:
                raise RuntimeError("db down")
            return {"id": self.insert_id}
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        return self.count

    async def execute(self, query, *args):
        return None


def _photo_env(monkeypatch, pool):
    import app.routers.photos as ph

    async def fake_pool():
        return pool

    monkeypatch.setattr(ph, "get_pool", fake_pool)
    monkeypatch.setattr(ph, "put_bytes", lambda key, data, mime: None)
    monkeypatch.setattr(ph, "signed_url", lambda key, ttl: f"https://cdn.test/{key}")
    return ph


def test_create_photo_empty_data_413(monkeypatch):
    import app.routers.photos as ph

    _photo_env(monkeypatch, _PhotoPool())
    from app.errors import AppError

    with pytest.raises(AppError) as exc:
        run(ph._create_photo({"id": _uid(), "role": "master"}, b"", "image/jpeg", "before", None))
    assert exc.value.status_code == 413


def test_create_photo_oversized_413(monkeypatch):
    import app.config as cfg
    import app.routers.photos as ph

    _photo_env(monkeypatch, _PhotoPool())
    monkeypatch.setattr(cfg.settings, "MAX_UPLOAD_BYTES", 4)
    from app.errors import AppError

    with pytest.raises(AppError) as exc:
        run(ph._create_photo({"id": _uid(), "role": "master"}, _JPEG, "image/jpeg", "before", None))
    assert exc.value.status_code == 413


def test_create_photo_standalone_master_ok(monkeypatch):
    import app.routers.photos as ph

    mod = _photo_env(monkeypatch, _PhotoPool())
    monkeypatch.setattr(mod, "_dhash", lambda data: None)
    out = run(
        ph._create_photo({"id": _uid(), "role": "master"}, _JPEG, "image/jpeg", "before", None)
    )
    assert out["replayed"] is False and out["url"].startswith("https://")


def test_create_photo_standalone_wrong_kind(monkeypatch):
    import app.routers.photos as ph

    _photo_env(monkeypatch, _PhotoPool())
    from app.errors import AppError

    with pytest.raises(AppError):
        run(ph._create_photo({"id": _uid(), "role": "master"}, _JPEG, "image/jpeg", "after", None))


def test_create_photo_worker_forbidden(monkeypatch):
    import app.routers.photos as ph

    mod = _photo_env(monkeypatch, _PhotoPool())

    async def fake_access(user, order_id):
        return {"assignee_id": "someone-else", "crew_id": None}

    monkeypatch.setattr(mod, "assert_can_read_order", fake_access)
    from app.errors import AppError

    user = {"id": _uid(), "role": "worker", "crewId": None}
    with pytest.raises(AppError) as exc:
        run(ph._create_photo(user, _JPEG, "image/jpeg", "after", _uid()))
    assert exc.value.status_code == 403


def test_create_photo_dup_replay(monkeypatch):
    import app.routers.photos as ph

    did = uuid.uuid4()
    _photo_env(monkeypatch, _PhotoPool(dup={"id": did, "object_key": "k1"}))
    out = run(
        ph._create_photo({"id": _uid(), "role": "master"}, _JPEG, "image/jpeg", "before", None)
    )
    assert out == {
        "id": str(did),
        "url": "https://cdn.test/k1",
        "expires_in": 300,
        "replayed": True,
    }


def test_create_photo_near_dup_still_accepts(monkeypatch):
    import app.routers.photos as ph

    mod = _photo_env(monkeypatch, _PhotoPool(near={"id": _uid(), "phash": "f" * 15 + "e"}))
    monkeypatch.setattr(mod, "_dhash", lambda data: "f" * 16)
    out = run(
        ph._create_photo({"id": _uid(), "role": "master"}, _JPEG, "image/jpeg", "before", None)
    )
    assert out["replayed"] is False


def test_create_photo_before_limit(monkeypatch):
    import app.routers.photos as ph

    mod = _photo_env(monkeypatch, _PhotoPool(count=5))

    async def fake_access(user, order_id):
        return {"assignee_id": user["id"], "crew_id": None}

    monkeypatch.setattr(mod, "assert_can_read_order", fake_access)
    from app.errors import AppError

    with pytest.raises(AppError):
        run(
            ph._create_photo(
                {"id": _uid(), "role": "master"}, _JPEG, "image/jpeg", "before", _uid()
            )
        )


def test_create_photo_insert_failure_cleans_s3(monkeypatch):
    import app.routers.photos as ph
    import app.storage as st

    mod = _photo_env(monkeypatch, _PhotoPool(fail_insert=True))
    monkeypatch.setattr(mod, "_dhash", lambda data: None)
    monkeypatch.setattr(
        st, "get_s3", lambda: type("S", (), {"delete_object": lambda self, **k: None})()
    )
    with pytest.raises(RuntimeError, match="db down"):
        run(ph._create_photo({"id": _uid(), "role": "master"}, _JPEG, "image/jpeg", "before", None))


# --- photos endpoints (wrappers + list) ---


class _Upload:
    content_type = "image/jpeg"

    async def read(self):
        return _JPEG


def test_upload_wrappers_delegate(monkeypatch):
    import app.routers.photos as ph

    seen = {}

    async def fake_create(user, data, mime, kind, order_id, taken_at=None):
        seen.update(kind=kind, order_id=order_id, n=len(data))
        return {"id": "p1"}

    monkeypatch.setattr(ph, "_create_photo", fake_create)
    user = {"id": _uid(), "role": "master"}
    out1 = run(ph.upload_standalone(kind="before", file=_Upload(), taken_at=None, user=user))
    assert out1 == {"id": "p1"} and seen["order_id"] is None
    oid = uuid.uuid4()
    out2 = run(
        ph.upload_to_order(order_id=oid, kind="after", file=_Upload(), taken_at=None, user=user)
    )
    assert out2 == {"id": "p1"} and seen["order_id"] == str(oid)


def test_list_order_photos(monkeypatch):
    import app.routers.photos as ph

    pid, aid = uuid.uuid4(), _uid()

    class _Pool:
        async def fetch(self, query, *args):
            return [
                {
                    "id": pid,
                    "kind": "after",
                    "file_key": "k1",
                    "content_type": "image/jpeg",
                    "size_bytes": 10,
                    "taken_at": None,
                    "author_id": aid,
                }
            ]

    async def fake_pool():
        return _Pool()

    async def fake_access(user, order_id):
        return {}

    monkeypatch.setattr(ph, "get_pool", fake_pool)
    monkeypatch.setattr(ph, "assert_can_read_order", fake_access)
    monkeypatch.setattr(ph, "signed_url", lambda key, ttl: f"https://cdn.test/{key}")
    out = run(ph.list_order_photos(uuid.uuid4(), {"id": aid, "role": "worker"}))
    assert out["data"][0]["id"] == str(pid)
    assert out["data"][0]["url"] == "https://cdn.test/k1"


# --- background._notify_for_event (lines 37-107 branches) ---


class _BgConn:
    def __init__(self, order=None, raise_fetch=False):
        self.order = order
        self.raise_fetch = raise_fetch

    async def fetchrow(self, query, *args):
        if self.raise_fetch:
            raise RuntimeError("db gone")
        return self.order


def _bg_env(monkeypatch, order=None, raise_fetch=False, fail_notify=False, fail_cast=False):
    import app.notifications as nt
    import app.routers.realtime as rt

    calls = {"notify": [], "cast": []}

    async def fake_notify(conn, recipients, ntype, data, etype, oid):
        calls["notify"].append((recipients, ntype, etype, oid))
        if fail_notify:
            raise RuntimeError("notify down")
        return [{"id": "n1"}]

    async def fake_cast(channels, wtype, payload):
        calls["cast"].append((channels, wtype))
        if fail_cast:
            raise RuntimeError("ws down")

    monkeypatch.setattr(nt, "notify", fake_notify)
    monkeypatch.setattr(rt, "broadcast", fake_cast)
    return _BgConn(order, raise_fetch), calls


def _bg_order(**over):
    base = {
        "id": _uid(),
        "number": 7,
        "master_id": "m1",
        "assignee_id": "w1",
        "crew_id": None,
    }
    base.update(over)
    return base


def test_notify_order_created(monkeypatch):
    import app.services.background as bg

    conn, calls = _bg_env(monkeypatch, order=_bg_order())
    oid = _uid()
    ws, rows = run(
        bg._notify_for_event(conn, "order.created", {"id": oid, "number": 5}, ["user:w1"])
    )
    assert ws == "WORK_ORDER_CREATED"
    assert calls["notify"][0][1] == "WORK_ORDER_ASSIGNED"
    assert rows == [{"id": "n1"}]
    assert any(c[1] == "NOTIFICATION_CREATED" for c in calls["cast"])


def test_notify_transition_accept_to_master(monkeypatch):
    import app.services.background as bg

    conn, calls = _bg_env(monkeypatch, order=_bg_order())
    ws, _ = run(
        bg._notify_for_event(conn, "order.transition", {"id": _uid(), "action": "accept"}, ["x"])
    )
    assert ws == "WORK_ORDER_ACCEPTED"
    assert calls["notify"][0][0] == ["m1"]


def test_notify_transition_close_adds_assignee(monkeypatch):
    import app.services.background as bg

    conn, calls = _bg_env(monkeypatch, order=_bg_order())
    ws, _ = run(
        bg._notify_for_event(conn, "order.transition", {"id": _uid(), "action": "close"}, ["x"])
    )
    assert ws == "WORK_ORDER_CLOSED"
    assert calls["notify"][0][0] == ["m1", "w1"]


def test_notify_transition_unknown_action_no_notify(monkeypatch):
    import app.services.background as bg

    conn, calls = _bg_env(monkeypatch, order=_bg_order())
    ws, rows = run(
        bg._notify_for_event(conn, "order.transition", {"id": _uid(), "action": "queue"}, ["x"])
    )
    assert ws == "WORK_ORDER_UPDATED"
    assert calls["notify"] == [] and rows == []


def test_notify_no_oid_skips_notify(monkeypatch):
    import app.services.background as bg

    conn, calls = _bg_env(monkeypatch)
    ws, rows = run(bg._notify_for_event(conn, "order.transition", {"action": "accept"}, ["x"]))
    assert ws == "WORK_ORDER_ACCEPTED"
    assert rows == [] and calls["notify"] == []


def test_notify_fetch_error_uses_payload_number(monkeypatch):
    import app.services.background as bg

    conn, calls = _bg_env(monkeypatch, raise_fetch=True)
    ws, _ = run(
        bg._notify_for_event(conn, "order.closed", {"id": _uid(), "number": 42}, ["user:w1"])
    )
    assert ws == "WORK_ORDER_CLOSED"
    assert calls["notify"][0][1] == "WORK_ORDER_CLOSED"


def test_notify_event_kind_mapping(monkeypatch):
    import app.services.background as bg

    cases = {
        "order.ai_review_ready": "AI_INSPECTION_READY",
        "order.overdue": "DEADLINE_EXCEEDED",
        "order.escalated": "DEADLINE_EXCEEDED",
        "order.overdue_warning": "DEADLINE_WARNING",
        "order.rejected": "WORK_ORDER_REJECTED",
        "order.reassigned": "WORK_ORDER_ASSIGNED",
        "employee.status_changed": "WORKER_STATUS_CHANGED",
        "something.else": "SOMETHING_ELSE",
    }
    for event, expected in cases.items():
        conn, _ = _bg_env(monkeypatch)
        ws, _ = run(bg._notify_for_event(conn, event, {"id": _uid()}, ["user:w1"]))
        assert ws == expected, event


def test_notify_failure_paths(monkeypatch):
    import app.services.background as bg

    conn, calls = _bg_env(monkeypatch, order=_bg_order(), fail_notify=True)
    ws, rows = run(bg._notify_for_event(conn, "order.closed", {"id": _uid()}, ["user:w1"]))
    assert ws == "WORK_ORDER_CLOSED" and rows == []
    conn2, _ = _bg_env(monkeypatch, order=_bg_order(), fail_cast=True)
    ws2, _ = run(bg._notify_for_event(conn2, "order.closed", {"id": _uid()}, ["user:w1"]))
    assert ws2 == "WORK_ORDER_CLOSED"


# --- deps.get_current_user (lines 19-20, 28-29, 45-54) ---


class _StubReq:
    def __init__(self, headers=None, query=None):
        self.headers = headers or {}
        self.query_params = query or {}


class _AuthPool:
    def __init__(self, emp, sess):
        self.emp = emp
        self.sess = sess
        self.n = 0

    async def fetchrow(self, query, *args):
        self.n += 1
        return self.emp if self.n == 1 else self.sess


def _auth_env(monkeypatch, claims, emp, sess):
    import app.deps as dp

    async def fake_pool():
        return _AuthPool(emp, sess)

    monkeypatch.setattr(dp, "get_pool", fake_pool)
    monkeypatch.setattr(dp, "decode_access_token", lambda token: claims)
    return dp


def _emp_row(uid, auth_version=2):
    return {
        "id": uid,
        "role": "worker",
        "crew_id": None,
        "full_name": "W",
        "auth_version": auth_version,
        "area_ids": [],
    }


def test_get_current_user_query_token_success(monkeypatch):
    uid, sid = _uid(), _uid()
    dp = _auth_env(
        monkeypatch,
        {"sub": uid, "sid": sid, "role": "worker", "authv": 2},
        _emp_row(uid),
        {"id": sid},
    )
    out = run(dp.get_current_user(_StubReq(headers={}, query={"token": "abc"})))
    assert out["id"] == uid and out["sessionId"] == sid and out["crewId"] is None


def test_get_current_user_missing_claims(monkeypatch):
    import app.errors as er

    dp = _auth_env(monkeypatch, {"role": "worker"}, None, None)
    with pytest.raises(er.AppError):
        run(dp.get_current_user(_StubReq(headers={"authorization": "Bearer x"})))


def test_get_current_user_no_employee(monkeypatch):
    import app.errors as er

    dp = _auth_env(
        monkeypatch,
        {"sub": _uid(), "sid": _uid(), "role": "worker", "authv": 1},
        None,
        None,
    )
    with pytest.raises(er.AppError):
        run(dp.get_current_user(_StubReq(headers={"authorization": "Bearer x"})))


def test_get_current_user_no_session(monkeypatch):
    import app.errors as er

    uid = _uid()
    dp = _auth_env(
        monkeypatch,
        {"sub": uid, "sid": _uid(), "role": "worker", "authv": 2},
        _emp_row(uid),
        None,
    )
    with pytest.raises(er.AppError):
        run(dp.get_current_user(_StubReq(headers={"authorization": "Bearer x"})))


def test_get_current_user_auth_version_mismatch(monkeypatch):
    import app.errors as er

    uid, sid = _uid(), _uid()
    dp = _auth_env(
        monkeypatch,
        {"sub": uid, "sid": sid, "role": "worker", "authv": 1},
        _emp_row(uid, auth_version=3),
        {"id": sid},
    )
    with pytest.raises(er.AppError):
        run(dp.get_current_user(_StubReq(headers={"authorization": "Bearer x"})))


# --- deps.assert_can_read_order (lines 91-101) ---


def _order_env(monkeypatch, row):
    import app.deps as dp

    class _Pool:
        async def fetchrow(self, query, *args):
            return row

    async def fake_pool():
        return _Pool()

    monkeypatch.setattr(dp, "get_pool", fake_pool)
    return dp


def test_assert_order_missing_and_admin(monkeypatch):
    import app.errors as er

    dp = _order_env(monkeypatch, None)
    with pytest.raises(er.AppError):
        run(dp.assert_can_read_order({"role": "admin"}, _uid()))
    dp2 = _order_env(monkeypatch, {"id": "o1", "area_id": "a9"})
    out = run(dp2.assert_can_read_order({"role": "manager"}, "o1"))
    assert out["id"] == "o1"


def test_assert_order_master_scope(monkeypatch):
    import app.errors as er

    dp = _order_env(monkeypatch, {"id": "o1", "area_id": "a1"})
    out = run(dp.assert_can_read_order({"role": "master", "areaIds": ["a1"]}, "o1"))
    assert out["area_id"] == "a1"
    with pytest.raises(er.AppError):
        run(dp.assert_can_read_order({"role": "master", "areaIds": ["other"]}, "o1"))


def test_assert_order_worker_branches(monkeypatch):
    import app.errors as er

    uid = _uid()
    dp = _order_env(monkeypatch, {"id": "o1", "assignee_id": uid, "crew_id": None})
    assert run(dp.assert_can_read_order({"role": "worker", "id": uid, "crewId": None}, "o1"))
    dp2 = _order_env(monkeypatch, {"id": "o1", "assignee_id": "x", "crew_id": "c9"})
    assert run(dp2.assert_can_read_order({"role": "worker", "id": uid, "crewId": "c9"}, "o1"))
    with pytest.raises(er.AppError):
        run(dp2.assert_can_read_order({"role": "worker", "id": uid, "crewId": None}, "o1"))


def test_assert_order_unknown_role_forbidden(monkeypatch):
    import app.errors as er

    dp = _order_env(monkeypatch, {"id": "o1", "area_id": "a1"})
    with pytest.raises(er.AppError) as exc:
        run(dp.assert_can_read_order({"role": "ghost", "areaIds": []}, "o1"))
    assert exc.value.status_code == 403

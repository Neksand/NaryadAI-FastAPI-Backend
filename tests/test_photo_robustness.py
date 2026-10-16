"""Photo-path hardening: retry storms, dedup races, S3 orphans, limit-before-write."""

import asyncio
import io
import uuid

import pytest
from PIL import Image

from app import deps
from app.errors import AppError
from app.routers import photos


def png_bytes(color=(90, 140, 210)) -> bytes:
    image = Image.new("RGB", (16, 12), color)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class FakePool:
    """In-memory photos table with atomic INSERT ... ON CONFLICT DO NOTHING."""

    def __init__(self, *, order=None, count=0, fail_insert=False, fail_commit=False):
        self.rows: dict[tuple[str, str, str, str | None], dict] = {}
        self.order = order
        self.count = count
        self.fail_insert = fail_insert
        self.fail_commit = fail_commit
        self.inserts = 0
        self._order_lock = asyncio.Lock()

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    def transaction(self):
        return _FakeTransaction(self)

    async def fetchrow(self, query, *args):
        await asyncio.sleep(0)  # let concurrent coroutines interleave like real I/O
        if "FOR UPDATE" in query:
            await self._order_lock.acquire()
            return self.order
        if "INSERT INTO photos" in query:
            if self.fail_insert:
                raise RuntimeError("database unavailable")
            assert "ON CONFLICT DO NOTHING" in query, "insert must be race-safe"
            order_id, kind, key, _mime, _size, sha, author, _ph, _taken = args
            dkey = (str(author), sha, kind, str(order_id) if order_id else None)
            if dkey in self.rows:
                return None
            row_id = uuid.uuid4()
            self.rows[dkey] = {
                "id": row_id,
                "object_key": key,
                "work_order_id": str(order_id) if order_id else None,
                "kind": kind,
            }
            self.inserts += 1
            return {"id": row_id}
        if "FROM work_orders" in query:
            return self.order
        if "sha256" in query:
            if len(args) == 4:
                dkey = (str(args[0]), args[1], args[2], str(args[3]))
            else:
                dkey = (str(args[0]), args[1], args[2], None)
            found = self.rows.get(dkey)
            return dict(found) if found else None
        if "phash IS NOT NULL" in query:
            return None
        raise AssertionError(f"unexpected query: {query}")

    async def fetchval(self, query, *args):
        await asyncio.sleep(0)
        if "count(*)" in query and args:
            order_id, kind = str(args[0]), args[1]
            existing = sum(
                row["work_order_id"] == order_id and row["kind"] == kind
                for row in self.rows.values()
            )
            return self.count + existing
        return self.count

    async def execute(self, query, *args):
        await asyncio.sleep(0)


class _FakeTransaction:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        if self.pool._order_lock.locked():
            self.pool._order_lock.release()
        if self.pool.fail_commit:
            self.pool.fail_commit = False
            raise RuntimeError("commit acknowledgement lost")
        return None


class FakeS3:
    def __init__(self, store):
        self.store = store
        self.deleted = []

    def delete_object(self, **kwargs):
        self.deleted.append(kwargs)
        self.store.pop(kwargs["Key"], None)


@pytest.fixture
def s3doubles(monkeypatch):
    store: dict = {}
    s3 = FakeS3(store)
    monkeypatch.setattr(
        photos, "put_bytes", lambda key, data, mime: store.__setitem__(key, (data, mime))
    )
    monkeypatch.setattr(photos, "signed_url", lambda key, ttl: f"signed:{key}:{ttl}")
    monkeypatch.setattr("app.storage.get_s3", lambda: s3)
    return store, s3


def _pool(monkeypatch, pool):
    async def get_pool():
        return pool

    monkeypatch.setattr(photos, "get_pool", get_pool)
    monkeypatch.setattr(deps, "get_pool", get_pool)
    return pool


def _master():
    return {"id": str(uuid.uuid4()), "role": "master", "areaIds": [], "crewId": None}


@pytest.mark.parametrize("with_order", [False, True])
def test_concurrent_duplicate_upload_single_row_no_orphan(monkeypatch, s3doubles, with_order):
    async def _race():
        return await asyncio.gather(
            photos._create_photo(user, payload, "image/png", kind, oid),
            photos._create_photo(user, payload, "image/png", kind, oid),
        )

    store, s3 = s3doubles
    payload = png_bytes()
    if with_order:
        order_id = str(uuid.uuid4())
        user_id = str(uuid.uuid4())
        order = {"id": order_id, "assignee_id": user_id, "crew_id": None, "area_id": uuid.uuid4()}
        user = {"id": user_id, "role": "worker", "areaIds": [], "crewId": None}
        oid: str | None = order_id
        kind = "after"
    else:
        order, oid, kind, user = None, None, "before", _master()
    pool = _pool(monkeypatch, FakePool(order=order))
    first, second = asyncio.run(_race())
    assert len(pool.rows) == 1
    assert pool.inserts == 1
    assert {first["replayed"], second["replayed"]} == {True, False}
    assert first["id"] == second["id"]
    assert len(store) == 1
    if with_order:
        # The order lock lets the second request replay before starting a PUT.
        assert s3.deleted == []
    else:
        # Pending photos still rely on the unique index to resolve the race.
        assert len(s3.deleted) == 1
        assert s3.deleted[0]["Key"] not in store


def test_failed_insert_cleans_s3_orphan(monkeypatch, s3doubles):
    store, s3 = s3doubles
    _pool(monkeypatch, FakePool(fail_insert=True))
    with pytest.raises(RuntimeError):
        asyncio.run(photos._create_photo(_master(), png_bytes(), "image/png", "before", None))
    assert store == {}
    assert len(s3.deleted) == 1


@pytest.mark.parametrize(("kind", "count"), [("before", 5), ("after", 10)])
def test_order_limit_rejected_before_s3_write(monkeypatch, s3doubles, kind, count):
    store, s3 = s3doubles
    order_id, user_id, area_id = str(uuid.uuid4()), str(uuid.uuid4()), uuid.uuid4()
    order = {"id": order_id, "assignee_id": user_id, "crew_id": None, "area_id": area_id}
    role = "master" if kind == "before" else "worker"
    user = {"id": user_id, "role": role, "areaIds": [str(area_id)], "crewId": None}
    pool = _pool(monkeypatch, FakePool(order=order, count=count))
    with pytest.raises(AppError) as raised:
        asyncio.run(photos._create_photo(user, png_bytes(), "image/png", kind, order_id))
    assert raised.value.status_code == 422
    assert store == {}
    assert s3.deleted == []
    assert pool.inserts == 0 and pool.rows == {}


def test_concurrent_different_photos_cannot_exceed_order_kind_limit(monkeypatch, s3doubles):
    async def _race():
        return await asyncio.gather(
            photos._create_photo(user, first_payload, "image/png", "after", order_id),
            photos._create_photo(user, second_payload, "image/png", "after", order_id),
            return_exceptions=True,
        )

    store, s3 = s3doubles
    order_id, user_id = str(uuid.uuid4()), str(uuid.uuid4())
    area_id = uuid.uuid4()
    order = {"id": order_id, "assignee_id": user_id, "crew_id": None, "area_id": area_id}
    user = {"id": user_id, "role": "worker", "areaIds": [], "crewId": None}
    first_payload = png_bytes((90, 140, 210))
    second_payload = png_bytes((210, 90, 140))
    pool = _pool(monkeypatch, FakePool(order=order, count=9))

    first, second = asyncio.run(_race())

    successes = [result for result in (first, second) if isinstance(result, dict)]
    failures = [result for result in (first, second) if isinstance(result, AppError)]
    assert len(successes) == 1
    assert len(failures) == 1 and failures[0].status_code == 422
    assert pool.inserts == 1 and len(pool.rows) == 1
    assert len(store) == 1 and s3.deleted == []


@pytest.mark.parametrize("with_order", [False, True])
def test_retry_same_file_replays_without_new_row(monkeypatch, s3doubles, with_order):
    store, s3 = s3doubles
    payload = png_bytes()
    if with_order:
        order_id = str(uuid.uuid4())
        user_id = str(uuid.uuid4())
        order = {"id": order_id, "assignee_id": user_id, "crew_id": None, "area_id": uuid.uuid4()}
        user = {"id": user_id, "role": "worker", "areaIds": [], "crewId": None}
        oid: str | None = order_id
        kind = "after"
    else:
        order, oid, kind, user = None, None, "before", _master()
    pool = _pool(monkeypatch, FakePool(order=order))
    first = asyncio.run(photos._create_photo(user, payload, "image/png", kind, oid))
    assert first["replayed"] is False
    second = asyncio.run(photos._create_photo(user, payload, "image/png", kind, oid))
    assert second == {**first, "replayed": True}
    assert pool.inserts == 1 and len(pool.rows) == 1
    assert len(store) == 1 and s3.deleted == []


def test_failed_s3_put_cleans_uncertain_object(monkeypatch, s3doubles):
    store, s3 = s3doubles
    _pool(monkeypatch, FakePool())

    def failed_put(key, data, mime):
        store[key] = (data, mime)
        raise RuntimeError("S3 response lost after object write")

    monkeypatch.setattr(photos, "put_bytes", failed_put)
    with pytest.raises(RuntimeError, match="S3 response lost"):
        asyncio.run(photos._create_photo(_master(), png_bytes(), "image/png", "before", None))

    assert store == {}
    assert len(s3.deleted) == 1


def test_failed_db_insert_cleans_uploaded_object(monkeypatch, s3doubles):
    store, s3 = s3doubles
    _pool(monkeypatch, FakePool(fail_insert=True))

    with pytest.raises(RuntimeError, match="database unavailable"):
        asyncio.run(photos._create_photo(_master(), png_bytes(), "image/png", "before", None))

    assert store == {}
    assert len(s3.deleted) == 1


def test_commit_exit_failure_after_insert_retains_s3_object(monkeypatch, s3doubles):
    store, s3 = s3doubles
    pool = _pool(monkeypatch, FakePool(fail_commit=True))

    with pytest.raises(RuntimeError, match="commit acknowledgement lost"):
        asyncio.run(photos._create_photo(_master(), png_bytes(), "image/png", "before", None))

    assert len(pool.rows) == 1
    assert len(store) == 1
    assert s3.deleted == []


def test_upload_reader_caps_bytes_and_rejects_before_storage(monkeypatch, s3doubles):
    import app.config as config

    store, s3 = s3doubles
    monkeypatch.setattr(config.settings, "MAX_UPLOAD_BYTES", 4)

    class OversizedUpload:
        content_type = "image/png"
        read_size = None

        async def read(self, size=-1):
            self.read_size = size
            return b"x" * size

    upload = OversizedUpload()
    monkeypatch.setattr(
        photos,
        "_create_photo",
        lambda *_args, **_kwargs: pytest.fail("oversized content reached photo creation"),
    )
    with pytest.raises(AppError) as raised:
        asyncio.run(
            photos.upload_standalone(kind="before", file=upload, taken_at=None, user=_master())
        )

    assert raised.value.status_code == 413
    assert upload.read_size == 5
    assert store == {} and s3.deleted == []

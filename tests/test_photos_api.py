"""Photo API tests for actual bytes, ownership, limits, duplicates, and cleanup."""

import io
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import deps
from app.errors import AppError
from app.routers import photos


def image_bytes(format="PNG", *, exif=False):
    image = Image.new("RGB", (16, 12), (90, 140, 210))
    output = io.BytesIO()
    if exif and format == "JPEG":
        metadata = Image.Exif()
        metadata[36867] = "2026:10:13 11:22:33"
        image.save(output, format=format, exif=metadata)
    else:
        image.save(output, format=format)
    return output.getvalue()


_PHOTO_INSTANT = datetime(2026, 10, 13, 12, 34, 56, tzinfo=UTC)
_OFFSET_TIMESTAMP_CASES = [
    (_PHOTO_INSTANT.astimezone(timezone(timedelta(hours=hours))).isoformat(), _PHOTO_INSTANT)
    for hours in range(-12, 15)
]


class Pool:
    def __init__(
        self,
        *,
        order=None,
        duplicate=None,
        near=None,
        photo=None,
        photos=None,
        count=0,
        fail_insert=False,
    ):
        self.order = order
        self.duplicate = duplicate
        self.near = near
        self.photo = photo
        self.photos = photos or []
        self.count = count
        self.fail_insert = fail_insert
        self.fetch_calls = []
        self.fetchrow_calls = []
        self.fetchval_calls = []
        self.execute_calls = []

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        return self.photos

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        if "FROM work_orders" in query:
            return self.order
        if "sha256=$2" in query:
            return self.duplicate
        if "phash IS NOT NULL" in query:
            return self.near
        if "INSERT INTO photos" in query:
            if self.fail_insert:
                raise RuntimeError("database unavailable")
            return {"id": uuid.uuid4()}
        return self.photo

    async def fetchval(self, query, *args):
        self.fetchval_calls.append((query, args))
        return self.count

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))


class S3:
    def __init__(self, fail=False):
        self.fail = fail
        self.deleted = []

    def delete_object(self, **kwargs):
        self.deleted.append(kwargs)
        if self.fail:
            raise RuntimeError("storage unavailable")


@pytest.fixture
def api(monkeypatch):
    clients = []
    puts = []
    s3 = S3()
    monkeypatch.setattr(photos, "put_bytes", lambda key, data, mime: puts.append((key, data, mime)))
    monkeypatch.setattr(photos, "signed_url", lambda key, ttl: f"signed:{key}:{ttl}")
    monkeypatch.setattr("app.storage.get_s3", lambda: s3)

    def create(pool=None, role="master", **fields):
        pool = pool or Pool()

        async def get_pool():
            return pool

        monkeypatch.setattr(photos, "get_pool", get_pool)
        monkeypatch.setattr(deps, "get_pool", get_pool)
        from app.main import create_app

        app = create_app()
        user = {"id": str(uuid.uuid4()), "role": role, "areaIds": [], "crewId": None, **fields}
        app.dependency_overrides[deps.get_current_user] = lambda: user
        client = TestClient(app)
        clients.append(client)
        return client, pool, user

    yield create, puts, s3
    for client in clients:
        client.close()


def test_image_helpers_verify_signatures_compute_hashes_and_read_exif():
    png = image_bytes()
    jpg = image_bytes("JPEG", exif=True)
    assert photos._dhash(png) and len(photos._dhash(png)) == 16
    assert photos._dhash(b"not an image") is None
    assert photos.hamming("0f", "00") == 4
    assert photos.hamming("invalid", "00") == 64
    photos._verify(png, "image/png")
    photos._verify(jpg, "image/jpeg")
    assert photos._exif_taken_at(jpg) == datetime(2026, 10, 13, 11, 22, 33)
    assert photos._exif_taken_at(png) is None
    with pytest.raises(Exception):
        photos._verify(png, "text/plain")
    with pytest.raises(Exception):
        photos._verify(b"bad", "image/png")


def test_standalone_upload_enforces_master_before_kind_and_file_size(api, monkeypatch):
    create, _, _ = api
    payload = image_bytes()
    denied = create(Pool(), role="manager")[0].post(
        "/api/v1/photos",
        data={"kind": "before"},
        files={"file": ("x.png", payload, "image/png")},
    )
    assert denied.status_code == 403
    wrong_kind = create(Pool(), role="master")[0].post(
        "/api/v1/photos",
        data={"kind": "after"},
        files={"file": ("x.png", payload, "image/png")},
    )
    assert wrong_kind.status_code == 422
    monkeypatch.setattr(photos.settings, "MAX_UPLOAD_BYTES", 10)
    too_large = create(Pool(), role="master")[0].post(
        "/api/v1/photos",
        data={"kind": "before"},
        files={"file": ("x.png", payload, "image/png")},
    )
    assert too_large.status_code == 413


def test_standalone_upload_returns_existing_duplicate_without_reupload(api):
    create, puts, _ = api
    duplicate = {"id": uuid.uuid4(), "object_key": "work-orders/pending/existing"}
    client = create(Pool(duplicate=duplicate), role="master")[0]
    response = client.post(
        "/api/v1/photos",
        data={"kind": "before"},
        files={"file": ("before.png", image_bytes(), "image/png")},
    )
    assert response.status_code == 200
    assert response.json() == {
        "id": str(duplicate["id"]),
        "url": "signed:work-orders/pending/existing:300",
        "expires_in": 300,
        "replayed": True,
    }
    assert puts == []


def test_near_duplicate_standalone_photo_is_accepted_and_stored(api):
    create, puts, _ = api
    payload = image_bytes()
    phash = photos._dhash(payload)
    pool = Pool(near={"id": uuid.uuid4(), "phash": phash})
    response = create(pool, role="master")[0].post(
        "/api/v1/photos",
        data={"kind": "before"},
        files={"file": ("before.png", payload, "image/png")},
    )
    assert response.status_code == 200 and response.json()["replayed"] is False
    assert puts[0][1:] == (payload, "image/png")
    assert any("phash IS NOT NULL" in query for query, _ in pool.fetchrow_calls)


def test_client_exif_timestamp_survives_compression_and_is_validated(api):
    create, _, _ = api
    client, pool, _ = create(Pool(), role="master")
    response = client.post(
        "/api/v1/photos",
        data={"kind": "before", "taken_at": "2026-10-13T11:22:33Z"},
        files={"file": ("before.png", image_bytes(), "image/png")},
    )
    assert response.status_code == 200
    insert = next(args for query, args in pool.fetchrow_calls if "INSERT INTO photos" in query)
    assert insert[8] == datetime(2026, 10, 13, 11, 22, 33, tzinfo=UTC)

    invalid = create(Pool(), role="master")[0].post(
        "/api/v1/photos",
        data={"kind": "before", "taken_at": "2026-99-99T25:61:61Z"},
        files={"file": ("before.png", image_bytes(), "image/png")},
    )
    assert invalid.status_code == 422


@pytest.mark.parametrize(("raw", "expected"), _OFFSET_TIMESTAMP_CASES)
def test_client_capture_times_normalize_every_supported_utc_offset(raw, expected):
    parsed = photos._parse_client_taken_at(raw)
    assert parsed == expected
    assert parsed.tzinfo is UTC


@pytest.mark.parametrize(
    "raw",
    [
        "not-a-date",
        "2026-13-01T00:00:00Z",
        "2026-02-30T12:00:00Z",
        "2026-01-01T24:00:00Z",
        "2026-01-01T12:60:00Z",
        "2026-01-01T12:00:60Z",
        "2026/01/01 12:00:00",
        "2026-W01-1T12:00:00",
        "0000-01-01T00:00:00Z",
        "1969-12-31T23:59:59Z",
        "2026-01-01T00:00:00+25:00",
        "2026-01-01T00:00:00+00:99",
        "2026-01-01T00:00:00Z;DROP TABLE photos",
        "NULL",
        "' OR 1=1 --",
        "2026-01-01",
        "2026-01-01 00:00:00+bogus",
        "2026-01-01T00:00:00+14:01",
        "2026-01-01T00:00:00-14:01",
        f"{datetime.now(UTC).year + 2}-01-01T00:00:00Z",
    ],
)
def test_client_capture_times_reject_invalid_dates_and_out_of_range_years(raw):
    with pytest.raises(AppError) as raised:
        photos._parse_client_taken_at(raw)
    assert raised.value.status_code == 422
    assert raised.value.details[0]["path"] == "taken_at"


@pytest.mark.parametrize("raw", [None, ""])
def test_missing_optional_client_capture_time_stays_absent(raw):
    assert photos._parse_client_taken_at(raw) is None


@pytest.mark.parametrize(("kind", "count"), [("before", 5), ("after", 10)])
def test_order_photo_upload_enforces_per_kind_count_limit(api, kind, count):
    create, _, _ = api
    order_id, area_id = uuid.uuid4(), uuid.uuid4()
    user_id = str(uuid.uuid4())
    order = {"id": order_id, "assignee_id": user_id, "crew_id": None, "area_id": area_id}
    role = "master" if kind == "before" else "worker"
    client = create(Pool(order=order, count=count), role=role, id=user_id, areaIds=[str(area_id)])[
        0
    ]
    response = client.post(
        f"/api/v1/work-orders/{order_id}/photos",
        data={"kind": kind},
        files={"file": ("photo.png", image_bytes(), "image/png")},
    )
    assert response.status_code == 422


def test_order_upload_checks_assignee_and_success_links_photo_to_order(api):
    create, _, _ = api
    order_id, user_id = uuid.uuid4(), str(uuid.uuid4())
    order = {"id": order_id, "assignee_id": user_id, "crew_id": None, "area_id": uuid.uuid4()}
    client, pool, _ = create(Pool(order=order), role="worker", id=user_id)
    before = client.post(
        f"/api/v1/work-orders/{order_id}/photos",
        data={"kind": "before"},
        files={"file": ("photo.png", image_bytes(), "image/png")},
    )
    assert before.status_code == 403
    after = client.post(
        f"/api/v1/work-orders/{order_id}/photos",
        data={"kind": "after"},
        files={"file": ("photo.png", image_bytes(), "image/png")},
    )
    assert after.status_code == 200
    insert = next(args for query, args in pool.fetchrow_calls if "INSERT INTO photos" in query)
    assert insert[0] == str(order_id) and insert[1] == "after"


def test_order_photo_list_serializes_urls_and_ids(api):
    create, _, _ = api
    order_id, photo_id, user_id, author_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        str(uuid.uuid4()),
        uuid.uuid4(),
    )
    order = {"id": order_id, "assignee_id": user_id, "crew_id": None, "area_id": uuid.uuid4()}
    photo = {
        "id": photo_id,
        "kind": "after",
        "file_key": "work-orders/p/after",
        "content_type": "image/png",
        "size_bytes": 64,
        "taken_at": None,
        "author_id": author_id,
    }
    client = create(Pool(order=order, photos=[photo]), role="worker", id=user_id)[0]
    result = client.get(f"/api/v1/work-orders/{order_id}/photos")
    assert result.status_code == 200
    row = result.json()["data"][0]
    assert row["id"] == str(photo_id) and row["author_id"] == str(author_id)
    assert row["url"] == "signed:work-orders/p/after:300" and row["expires_in"] == 300


def test_delete_photo_enforces_pending_ownership_and_tolerates_storage_errors(api):
    create, _, s3 = api
    photo_id, user_id = uuid.uuid4(), str(uuid.uuid4())
    linked = {
        "id": photo_id,
        "work_order_id": uuid.uuid4(),
        "author_id": user_id,
        "object_key": "linked",
    }
    assert (
        create(Pool(photo=linked), role="worker", id=user_id)[0]
        .delete(f"/api/v1/photos/{photo_id}")
        .status_code
        == 403
    )
    pending = {
        "id": photo_id,
        "work_order_id": None,
        "author_id": str(uuid.uuid4()),
        "object_key": "pending",
    }
    assert (
        create(Pool(photo=pending), role="worker", id=user_id)[0]
        .delete(f"/api/v1/photos/{photo_id}")
        .status_code
        == 403
    )
    owner = {"id": photo_id, "work_order_id": None, "author_id": user_id, "object_key": "pending"}
    s3.fail = True
    client, pool, _ = create(Pool(photo=owner), role="worker", id=user_id)
    response = client.delete(f"/api/v1/photos/{photo_id}")
    assert response.status_code == 200 and response.json()["data"]["deleted"] is True
    assert "DELETE FROM photos" in pool.execute_calls[0][0]


def test_photo_url_is_private_for_pending_rows_and_signed_for_owner(api):
    create, _, _ = api
    photo_id, owner_id = uuid.uuid4(), str(uuid.uuid4())
    row = {
        "id": photo_id,
        "work_order_id": None,
        "author_id": owner_id,
        "object_key": "pending/photo",
    }
    owner = create(Pool(photo=row), role="worker", id=owner_id)[0]
    assert owner.get(f"/api/v1/photos/{photo_id}/url").json() == {
        "url": "signed:pending/photo:300",
        "expires_in": 300,
    }
    stranger = create(Pool(photo=row), role="worker")[0].get(f"/api/v1/photos/{photo_id}/url")
    assert stranger.status_code == 404
    admin = create(Pool(photo=row), role="admin")[0].get(f"/api/v1/photos/{photo_id}/url")
    assert admin.status_code == 200

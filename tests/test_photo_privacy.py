"""EXIF privacy: JPEG uploads are stripped of GPS/model, taken_at kept in DB."""

import io
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import deps
from app.routers import photos

_TAKEN_AT_RAW = "2026:10:13 11:22:33"
_TAKEN_AT = datetime(2026, 10, 13, 11, 22, 33)


def jpeg_with_exif() -> bytes:
    image = Image.new("RGB", (16, 12), (90, 140, 210))
    exif = Image.Exif()
    exif[271] = "TestMake"
    exif[272] = "TestModel"
    exif[36867] = _TAKEN_AT_RAW
    gps_ifd = exif.get_ifd(0x8825)
    gps_ifd[0] = bytes([2, 3, 0, 0])
    gps_ifd[1] = "N"
    gps_ifd[2] = (55.0, 37.0)
    gps_ifd[3] = "E"
    output = io.BytesIO()
    image.save(output, format="JPEG", exif=exif)
    return output.getvalue()


def png_bytes() -> bytes:
    image = Image.new("RGB", (16, 12), (90, 140, 210))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class _Pool:
    def __init__(self):
        self.inserts = []

    async def fetchrow(self, query, *args):
        if "INSERT INTO photos" in query:
            self.inserts.append(args)
            return {"id": uuid.uuid4()}
        return None

    async def fetchval(self, query, *args):
        return 0

    async def execute(self, query, *args):
        pass


@pytest.fixture
def api(monkeypatch):
    puts = []
    pool = _Pool()
    monkeypatch.setattr(photos, "put_bytes", lambda key, data, mime: puts.append((key, data, mime)))
    monkeypatch.setattr(photos, "signed_url", lambda key, ttl: f"signed:{key}:{ttl}")

    async def get_pool():
        return pool

    monkeypatch.setattr(photos, "get_pool", get_pool)
    monkeypatch.setattr(deps, "get_pool", get_pool)
    from app.main import create_app

    app = create_app()
    user = {"id": str(uuid.uuid4()), "role": "master", "areaIds": [], "crewId": None}
    app.dependency_overrides[deps.get_current_user] = lambda: user
    client = TestClient(app)
    yield client, puts, pool
    client.close()


def test_jpeg_exif_stripped_but_taken_at_readable():
    original = jpeg_with_exif()
    assert photos._exif_taken_at(original) == _TAKEN_AT

    stripped = photos._strip_jpeg_exif(original)
    assert stripped[:3] == original[:3]

    with Image.open(io.BytesIO(stripped)) as img:
        assert img.size == (16, 12)
        exif = img.getexif()
        assert exif.get(271) is None
        assert exif.get(272) is None
        assert exif.get(36867) is None
        assert exif.get(34853) is None
        assert dict(exif.get_ifd(0x8825)) == {}


def test_jpeg_upload_stores_stripped_bytes_and_taken_at(api):
    client, puts, pool = api
    response = client.post(
        "/api/v1/photos",
        data={"kind": "before"},
        files={"file": ("photo.jpg", jpeg_with_exif(), "image/jpeg")},
    )
    assert response.status_code == 200
    assert response.json()["replayed"] is False
    assert len(puts) == 1
    _, stored, mime = puts[0]
    assert mime == "image/jpeg"
    with Image.open(io.BytesIO(stored)) as img:
        exif = img.getexif()
        assert exif.get(271) is None
        assert exif.get(272) is None
        assert exif.get(34853) is None
    assert pool.inserts[0][8] is None  # No EXIF offset: do not invent a UTC instant.


def test_png_upload_passes_through_unchanged(api):
    client, puts, _ = api
    payload = png_bytes()
    response = client.post(
        "/api/v1/photos",
        data={"kind": "before"},
        files={"file": ("photo.png", payload, "image/png")},
    )
    assert response.status_code == 200
    assert puts[0][1:] == (payload, "image/png")
    assert photos._strip_jpeg_exif(payload) == payload


def test_invalid_bytes_fall_back_as_before():
    garbage = b"not an image"
    assert photos._strip_jpeg_exif(garbage) == garbage
    assert photos._exif_taken_at(garbage) is None
    with pytest.raises(Exception):
        photos._verify(garbage, "image/png")


def test_exif_offset_original_is_converted_to_utc():
    image = Image.new("RGB", (16, 12), (90, 140, 210))
    exif = Image.Exif()
    exif[36867] = "2026:10:13 11:22:33"
    exif[36881] = "+05:00"
    output = io.BytesIO()
    image.save(output, format="JPEG", exif=exif)
    assert photos._exif_taken_at(output.getvalue()) == datetime(2026, 10, 13, 6, 22, 33, tzinfo=UTC)


def test_naive_exif_uses_explicit_client_offset_instead_of_assuming_utc(api):
    client, _, pool = api
    response = client.post(
        "/api/v1/photos",
        data={"kind": "before", "taken_at": "2026-10-13T11:22:33+05:00"},
        files={"file": ("photo.jpg", jpeg_with_exif(), "image/jpeg")},
    )
    assert response.status_code == 200
    assert pool.inserts[0][8] == datetime(2026, 10, 13, 6, 22, 33, tzinfo=UTC)


@pytest.mark.parametrize(
    ("mime", "payload"),
    [
        ("image/jpeg", b"\xff\xd8\xffnot-an-image"),
        ("image/png", b"\x89PNG\r\n\x1a\nnot-an-image"),
        ("image/webp", b"RIFF\x00\x00\x00\x00WEBPnot-an-image"),
    ],
)
def test_magic_header_alone_is_rejected_before_any_storage_write(api, mime, payload):
    client, puts, pool = api
    response = client.post(
        "/api/v1/photos", data={"kind": "before"}, files={"file": ("photo", payload, mime)}
    )
    assert response.status_code == 422
    assert puts == []
    assert pool.inserts == []


def test_image_with_excessive_dimensions_is_rejected_without_decoding_pixels(api):
    import struct
    import zlib

    client, puts, pool = api
    payload = bytearray(png_bytes())
    payload[16:24] = struct.pack(">II", 10000, 10000)
    payload[29:33] = struct.pack(">I", zlib.crc32(payload[12:29]))
    response = client.post(
        "/api/v1/photos",
        data={"kind": "before"},
        files={"file": ("oversized.png", bytes(payload), "image/png")},
    )
    assert response.status_code == 422
    assert puts == [] and pool.inserts == []

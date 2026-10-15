import contextlib
import hashlib
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.config import settings
from app.db import get_pool
from app.deps import assert_can_read_order, get_current_user, require_role
from app.errors import AppError, forbidden, not_found, validation_error
from app.storage import put_bytes, signed_url

router = APIRouter()

ACCEPTED = {"image/jpeg", "image/png", "image/webp"}
CLIENT_TAKEN_AT = re.compile(
    r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})T"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?:\.\d{1,9})?(?P<zone>Z|[+-]\d{2}:\d{2})?"
)


def _dhash(data: bytes) -> str | None:
    """64-bit difference hash (Pillow only, no extra deps)."""
    try:
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(data)).convert("L").resize((9, 8))
        px = list(img.tobytes())
        bits = "".join(
            "1" if px[r * 9 + c] > px[r * 9 + c + 1] else "0" for r in range(8) for c in range(8)
        )
        return hex(int(bits, 2))[2:].zfill(16)
    except Exception:
        return None


def hamming(a: str, b: str) -> int:
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return 64


def _exif_taken_at(data: bytes) -> datetime | None:
    """EXIF DateTimeOriginal for 'photo taken at closing time' checks."""
    try:
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(data))
        exif = img.getexif()
        raw = exif.get(36867) or exif.get(306)
        if not raw:
            return None
        from datetime import datetime

        parsed = datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")
        offset = exif.get(36881) or exif.get(36880)  # OffsetTimeOriginal / OffsetTime
        if offset and re.fullmatch(r"[+-]\d{2}:\d{2}", str(offset)):
            return datetime.fromisoformat(parsed.isoformat() + str(offset)).astimezone(UTC)
        return parsed
    except Exception:
        return None


def _strip_jpeg_exif(data: bytes) -> bytes:
    """Rebuild JPEG from pixels, dropping all EXIF (GPS, model, ...).

    Capture time is preserved separately in DB via _exif_taken_at,
    which must be read from the original bytes BEFORE calling this.
    Non-JPEG or undecodable input is returned unchanged.
    """
    try:
        import io

        from PIL import Image, ImageOps

        img = Image.open(io.BytesIO(data))
        if img.format != "JPEG":
            return data
        fixed = ImageOps.exif_transpose(img)
        mode = fixed.mode if fixed.mode in ("1", "L", "RGB", "CMYK") else "RGB"
        flat = fixed.convert(mode)
        fresh = Image.new(flat.mode, flat.size)
        fresh.paste(flat)
        out = io.BytesIO()
        fresh.save(out, format="JPEG")
        return out.getvalue()
    except Exception:
        return data


def _verify(data: bytes, mime: str) -> None:
    if mime not in ACCEPTED:
        raise validation_error([{"path": "file", "message": "Поддерживаются JPEG, PNG и WebP"}])
    ok = (
        (
            mime == "image/jpeg"
            and len(data) >= 3
            and data[0] == 0xFF
            and data[1] == 0xD8
            and data[2] == 0xFF
        )
        or (mime == "image/png" and data[:8] == bytes([137, 80, 78, 71, 13, 10, 26, 10]))
        or (mime == "image/webp" and data[0:4] == b"RIFF" and data[8:12] == b"WEBP")
    )
    if not ok:
        raise validation_error([{"path": "file", "message": "Файл не похож на заявленный формат"}])
    import io
    import warnings

    from PIL import Image

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if (
                    image.format
                    != {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}[mime]
                ):
                    raise ValueError("Image format mismatch")
                if image.width * image.height > 25_000_000:
                    raise ValueError("Image dimensions exceed limit")
                image.verify()
            # verify() checks container integrity; load() also rejects truncated
            # pixel streams. This happens before any storage or database write.
            with Image.open(io.BytesIO(data)) as image:
                image.load()
    except (
        OSError,
        ValueError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise validation_error(
            [
                {
                    "path": "file",
                    "message": "Повреждённое изображение или размер больше 25 мегапикселей",
                }
            ]
        ) from exc


def _parse_client_taken_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        match = CLIENT_TAKEN_AT.fullmatch(value)
        if not match or len(value) > 64:
            raise ValueError("expected full ISO timestamp")
        if (
            int(match.group("hour")) > 23
            or int(match.group("minute")) > 59
            or int(match.group("second")) > 59
        ):
            raise ValueError("invalid time")
        zone = match.group("zone")
        if zone and zone not in ("Z", "z"):
            offset_hours, offset_minutes = int(zone[1:3]), int(zone[4:6])
            if (
                offset_hours > 14
                or offset_minutes > 59
                or (offset_hours == 14 and offset_minutes != 0)
            ):
                raise ValueError("invalid timezone offset")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        parsed = parsed.astimezone(UTC)
        if parsed.year < 1970 or parsed.year > datetime.now(UTC).year + 1:
            raise ValueError("timestamp out of range")
        return parsed
    except (TypeError, ValueError) as e:
        raise validation_error(
            [{"path": "taken_at", "message": "Некорректное время съёмки"}]
        ) from e


async def _create_photo(
    user: dict[str, Any],
    data: bytes,
    mime: str,
    kind: str,
    order_id: str | None,
    client_taken_at: str | None = None,
) -> dict[str, Any]:
    if not data or len(data) > settings.MAX_UPLOAD_BYTES:
        raise AppError(413, "file_too_large", "Размер файла превышает допустимый")
    _verify(data, mime)
    pool = await get_pool()
    if order_id is None:
        require_role(user, "master")
        if kind != "before":
            raise validation_error([{"path": "kind", "message": "Без наряда только kind=before"}])
    else:
        order = await assert_can_read_order(user, order_id)
        if kind == "before":
            require_role(user, "master")
        else:
            require_role(user, "worker")
            if str(order.get("assignee_id") or "") != user["id"] and not (
                user["crewId"] and str(order.get("crew_id") or "") == user["crewId"]
            ):
                raise forbidden()
    exif_time = _exif_taken_at(data)
    client_time = _parse_client_taken_at(client_taken_at)
    # DateTimeOriginal alone is a camera's wall clock, not UTC. Without an EXIF
    # offset prefer the explicit client timestamp, otherwise freshness is unknown.
    taken_at = exif_time if exif_time is not None and exif_time.tzinfo is not None else client_time
    if mime == "image/jpeg":
        data = _strip_jpeg_exif(data)
    digest = hashlib.sha256(data).hexdigest()
    phash = _dhash(data)
    dup = await pool.fetchrow(
        "SELECT id, object_key FROM photos WHERE author_id=$1::uuid AND sha256=$2 AND kind=$3 AND work_order_id IS NULL LIMIT 1",
        user["id"],
        digest,
        kind,
    )
    if dup:
        return {
            "id": str(dup["id"]),
            "url": signed_url(dup["object_key"], 300),
            "expires_in": 300,
            "replayed": True,
        }
    if phash and not order_id:
        # near-duplicate of an old photo (re-shot at a different angle): warn, still accept
        near = await pool.fetchrow(
            "SELECT id, phash FROM photos WHERE author_id=$1::uuid AND kind=$2 AND phash IS NOT NULL LIMIT 50",
            user["id"],
            kind,
        )
        _near_dup = bool(near and near["phash"] and hamming(phash, near["phash"]) <= 6)
    else:
        _near_dup = False
    if order_id:
        cnt = await pool.fetchval(
            "SELECT count(*) FROM photos WHERE work_order_id=$1::uuid AND kind=$2", order_id, kind
        )
        if (kind == "before" and int(cnt) >= 5) or (kind == "after" and int(cnt) >= 10):
            raise validation_error([{"path": "file", "message": "Превышен лимит фото"}])
    key = f"work-orders/{order_id or 'pending'}/{uuid.uuid4()}"
    put_bytes(key, data, mime)
    try:
        row = await pool.fetchrow(
            "INSERT INTO photos(work_order_id, kind, object_key, content_type, size_bytes, sha256, author_id, phash, exif_taken_at) VALUES ($1::uuid,$2,$3,$4,$5,$6,$7::uuid,$8,$9) RETURNING id",
            order_id,
            kind,
            key,
            mime,
            len(data),
            digest,
            user["id"],
            phash,
            taken_at,
        )
    except Exception:
        from app.config import settings as s
        from app.storage import get_s3

        with contextlib.suppress(Exception):
            get_s3().delete_object(Bucket=s.S3_BUCKET, Key=key)
        raise
    return {"id": str(row["id"]), "url": signed_url(key, 300), "expires_in": 300, "replayed": False}


@router.post("/photos")
async def upload_standalone(
    kind: str = Form(...),
    file: UploadFile = File(...),
    taken_at: str | None = Form(default=None),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    return await _create_photo(
        user, await file.read(), file.content_type or "", kind, None, taken_at
    )


@router.post("/work-orders/{order_id}/photos")
async def upload_to_order(
    order_id: uuid.UUID,
    kind: str = Form(...),
    file: UploadFile = File(...),
    taken_at: str | None = Form(default=None),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    return await _create_photo(
        user, await file.read(), file.content_type or "", kind, str(order_id), taken_at
    )


@router.get("/work-orders/{order_id}/photos", summary="List order photos")
async def list_order_photos(
    order_id: uuid.UUID,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    await assert_can_read_order(user, str(order_id))
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT id, kind, object_key AS file_key, content_type, size_bytes, taken_at, author_id FROM photos WHERE work_order_id=$1::uuid ORDER BY created_at",
        str(order_id),
    )
    out = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        d["author_id"] = str(d["author_id"])
        d["url"] = signed_url(d.pop("file_key"), 300)
        d["expires_in"] = 300
        out.append(d)
    return {"data": out}


@router.delete("/photos/{photo_id}", summary="Delete a pending photo")
async def delete_photo(
    photo_id: uuid.UUID,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Only unlinked (pending) photos can be removed, by author or master/admin."""
    from app.config import settings as _s
    from app.storage import get_s3

    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM photos WHERE id=$1::uuid", str(photo_id))
    if not row:
        raise not_found("Фото не найдено")
    if row["work_order_id"] is not None:
        raise forbidden("Фото уже привязано к наряду")
    if str(row["author_id"]) != user["id"] and user["role"] not in ("master", "admin"):
        raise forbidden()
    await pool.execute("DELETE FROM photos WHERE id=$1::uuid", str(photo_id))
    with contextlib.suppress(Exception):
        get_s3().delete_object(Bucket=_s.S3_BUCKET, Key=row["object_key"])
    return {"data": {"id": str(photo_id), "deleted": True}}


@router.get("/photos/{photo_id}/url")
async def photo_url(
    photo_id: uuid.UUID,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM photos WHERE id=$1::uuid", str(photo_id))
    if not row:
        raise not_found("Фото не найдено")
    if row["work_order_id"]:
        await assert_can_read_order(user, str(row["work_order_id"]))
    elif str(row["author_id"]) != user["id"] and user["role"] != "admin":
        raise not_found("Фото не найдено")
    return {"url": signed_url(row["object_key"], 300), "expires_in": 300}

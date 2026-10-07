import hashlib
import uuid

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.config import settings
from app.db import get_pool
from app.deps import assert_can_read_order, get_current_user, require_role
from app.errors import AppError, forbidden, not_found, validation_error
from app.storage import put_bytes, signed_url

router = APIRouter()

ACCEPTED = {"image/jpeg", "image/png", "image/webp"}


def _dhash(data: bytes) -> str | None:
    """64-bit difference hash (Pillow only, no extra deps)."""
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(data)).convert("L").resize((9, 8))
        px = list(img.tobytes())
        bits = "".join("1" if px[r * 9 + c] > px[r * 9 + c + 1] else "0" for r in range(8) for c in range(8))
        return hex(int(bits, 2))[2:].zfill(16)
    except Exception:
        return None


def hamming(a: str, b: str) -> int:
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return 64


def _exif_taken_at(data: bytes):
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
        return datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")
    except Exception:
        return None


def _verify(data: bytes, mime: str):
    if mime not in ACCEPTED:
        raise validation_error([{"path": "file", "message": "Поддерживаются JPEG, PNG и WebP"}])
    ok = (mime == "image/jpeg" and len(data) >= 3 and data[0] == 0xFF and data[1] == 0xD8 and data[2] == 0xFF) \
        or (mime == "image/png" and data[:8] == bytes([137, 80, 78, 71, 13, 10, 26, 10])) \
        or (mime == "image/webp" and data[0:4] == b"RIFF" and data[8:12] == b"WEBP")
    if not ok:
        raise validation_error([{"path": "file", "message": "Файл не похож на заявленный формат"}])


async def _create_photo(user: dict, data: bytes, mime: str, kind: str, order_id: str | None) -> dict:
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
            if str(order.get("assignee_id") or "") != user["id"] and not (user["crewId"] and str(order.get("crew_id") or "") == user["crewId"]):
                raise forbidden()
    digest = hashlib.sha256(data).hexdigest()
    phash = _dhash(data)
    taken_at = _exif_taken_at(data)
    dup = await pool.fetchrow(
        "SELECT id, object_key FROM photos WHERE author_id=$1::uuid AND sha256=$2 AND kind=$3 AND work_order_id IS NULL LIMIT 1",
        user["id"], digest, kind)
    if dup:
        return {"id": str(dup["id"]), "url": signed_url(dup["object_key"], 300), "expires_in": 300, "replayed": True}
    if phash and not order_id:
        # near-duplicate of an old photo (re-shot at a different angle): warn, still accept
        near = await pool.fetchrow(
            "SELECT id, phash FROM photos WHERE author_id=$1::uuid AND kind=$2 AND phash IS NOT NULL LIMIT 50",
            user["id"], kind)
        _near_dup = bool(near and near["phash"] and hamming(phash, near["phash"]) <= 6)
    else:
        _near_dup = False
    if order_id:
        cnt = await pool.fetchval("SELECT count(*) FROM photos WHERE work_order_id=$1::uuid AND kind=$2", order_id, kind)
        if (kind == "before" and int(cnt) >= 5) or (kind == "after" and int(cnt) >= 10):
            raise validation_error([{"path": "file", "message": "Превышен лимит фото"}])
    key = f"work-orders/{order_id or 'pending'}/{uuid.uuid4()}"
    put_bytes(key, data, mime)
    try:
        row = await pool.fetchrow(
            "INSERT INTO photos(work_order_id, kind, object_key, content_type, size_bytes, sha256, author_id, phash, exif_taken_at) VALUES ($1::uuid,$2,$3,$4,$5,$6,$7::uuid,$8,$9) RETURNING id",
            order_id, kind, key, mime, len(data), digest, user["id"], phash, taken_at)
    except Exception:
        from app.storage import get_s3
        from app.config import settings as s
        try:
            get_s3().delete_object(Bucket=s.S3_BUCKET, Key=key)
        except Exception:
            pass
        raise
    return {"id": str(row["id"]), "url": signed_url(key, 300), "expires_in": 300, "replayed": False}


@router.post("/photos")
async def upload_standalone(kind: str = Form(...), file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    return await _create_photo(user, await file.read(), file.content_type or "", kind, None)


@router.post("/work-orders/{order_id}/photos")
async def upload_to_order(order_id: uuid.UUID, kind: str = Form(...), file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    return await _create_photo(user, await file.read(), file.content_type or "", kind, str(order_id))


@router.get("/work-orders/{order_id}/photos", summary="List order photos")
async def list_order_photos(order_id: uuid.UUID, user: dict = Depends(get_current_user)):
    await assert_can_read_order(user, str(order_id))
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT id, kind, object_key AS file_key, content_type, size_bytes, taken_at, author_id FROM photos WHERE work_order_id=$1::uuid ORDER BY created_at",
        str(order_id))
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
async def delete_photo(photo_id: uuid.UUID, user: dict = Depends(get_current_user)):
    """Only unlinked (pending) photos can be removed, by author or master/admin."""
    from app.storage import get_s3
    from app.config import settings as _s
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM photos WHERE id=$1::uuid", str(photo_id))
    if not row:
        raise not_found("Фото не найдено")
    if row["work_order_id"] is not None:
        raise forbidden("Фото уже привязано к наряду")
    if str(row["author_id"]) != user["id"] and user["role"] not in ("master", "admin"):
        raise forbidden()
    await pool.execute("DELETE FROM photos WHERE id=$1::uuid", str(photo_id))
    try:
        get_s3().delete_object(Bucket=_s.S3_BUCKET, Key=row["object_key"])
    except Exception:
        pass
    return {"data": {"id": str(photo_id), "deleted": True}}


@router.get("/photos/{photo_id}/url")
async def photo_url(photo_id: uuid.UUID, user: dict = Depends(get_current_user)):
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM photos WHERE id=$1::uuid", str(photo_id))
    if not row:
        raise not_found("Фото не найдено")
    if row["work_order_id"]:
        await assert_can_read_order(user, str(row["work_order_id"]))
    elif str(row["author_id"]) != user["id"] and user["role"] != "admin":
        raise not_found("Фото не найдено")
    return {"url": signed_url(row["object_key"], 300), "expires_in": 300}

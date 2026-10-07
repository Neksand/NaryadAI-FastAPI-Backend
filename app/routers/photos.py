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
    dup = await pool.fetchrow(
        "SELECT id, object_key FROM photos WHERE author_id=$1::uuid AND sha256=$2 AND kind=$3 AND work_order_id IS NULL LIMIT 1",
        user["id"], digest, kind)
    if dup:
        return {"id": str(dup["id"]), "url": signed_url(dup["object_key"], 300), "expires_in": 300, "replayed": True}
    if order_id:
        cnt = await pool.fetchval("SELECT count(*) FROM photos WHERE work_order_id=$1::uuid AND kind=$2", order_id, kind)
        if (kind == "before" and int(cnt) >= 5) or (kind == "after" and int(cnt) >= 10):
            raise validation_error([{"path": "file", "message": "Превышен лимит фото"}])
    key = f"work-orders/{order_id or 'pending'}/{uuid.uuid4()}"
    put_bytes(key, data, mime)
    try:
        row = await pool.fetchrow(
            "INSERT INTO photos(work_order_id, kind, object_key, content_type, size_bytes, sha256, author_id) VALUES ($1::uuid,$2,$3,$4,$5,$6,$7::uuid) RETURNING id",
            order_id, kind, key, mime, len(data), digest, user["id"])
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

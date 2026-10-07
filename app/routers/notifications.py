import uuid

from fastapi import APIRouter, Depends, Query

from app.db import get_pool
from app.deps import get_current_user

router = APIRouter()


def _s(r) -> dict:
    d = dict(r)
    for k, v in list(d.items()):
        if isinstance(v, uuid.UUID):
            d[k] = str(v)
    return d


@router.get("/notifications", summary="My notifications (newest first)")
async def my_notifications(unread_only: bool = False, limit: int = Query(50, ge=1, le=200),
                           user: dict = Depends(get_current_user)):
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT * FROM notifications WHERE recipient_id=$1::uuid
           AND ($2::bool = false OR is_read = false)
           ORDER BY created_at DESC LIMIT $3""", user["id"], unread_only, limit)
    total_unread = await pool.fetchval("SELECT count(*)::int FROM notifications WHERE recipient_id=$1::uuid AND is_read=false", user["id"])
    return {"data": [_s(r) for r in rows], "unread": total_unread}


@router.post("/notifications/{nid}/read", summary="Mark one notification read")
async def mark_read(nid: uuid.UUID, user: dict = Depends(get_current_user)):
    pool = await get_pool()
    await pool.execute("UPDATE notifications SET is_read=true WHERE id=$1::uuid AND recipient_id=$2::uuid", str(nid), user["id"])
    return {"data": {"id": str(nid), "is_read": True}}


@router.post("/notifications/read-all", summary="Mark all my notifications read")
async def mark_all(user: dict = Depends(get_current_user)):
    pool = await get_pool()
    n = await pool.fetchval(
        "WITH u AS (UPDATE notifications SET is_read=true WHERE recipient_id=$1::uuid AND is_read=false RETURNING id) SELECT count(*)::int FROM u",
        user["id"])
    return {"data": {"marked": int(n or 0)}}

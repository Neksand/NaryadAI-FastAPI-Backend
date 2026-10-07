import re
import uuid

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel

from app.db import get_pool
from app.deps import assert_can_read_order, get_current_user, require_role
from app.errors import validation_error

router = APIRouter()


class AssigneeIn(BaseModel):
    area_id: uuid.UUID
    equipment_id: uuid.UUID | None = None


class FaultIn(BaseModel):
    description: str
    equipment_id: uuid.UUID | None = None


@router.post("/ai/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    work_order_id: uuid.UUID | None = Form(default=None),
    user: dict = Depends(get_current_user),
):
    """Voice note upload + transcription (Groq Whisper when GROQ_API_KEY is set)."""
    require_role(user, "worker", "master")
    from app.ai_providers import transcribe_audio

    data = await file.read()
    if not data or len(data) > 25 * 1024 * 1024:
        from app.errors import validation_error
        raise validation_error([{"path": "file", "message": "Аудио до 25 МБ"}])
    text, provider = await transcribe_audio(data, file.filename or "voice.ogg")
    pool = await get_pool()
    key = f"voice-notes/{work_order_id or 'pending'}/{uuid.uuid4()}"
    from app.storage import put_bytes
    put_bytes(key, data, file.content_type or "audio/ogg")
    row = await pool.fetchrow(
        "INSERT INTO voice_notes(work_order_id, object_key, transcript, provider, author_id) VALUES ($1::uuid,$2,$3,$4,$5::uuid) RETURNING id",
        str(work_order_id) if work_order_id else None, key, text, provider, user["id"])
    return {"id": str(row["id"]), "transcript": text, "provider": provider,
            "pending": text is None}


@router.post("/ai/work-orders/{oid}/recommend-worker", summary="Recommend worker for order (master decides)")
async def recommend_worker(oid: uuid.UUID, user: dict = Depends(get_current_user)):
    """AI suggestion only — assignment is always done by the master."""
    require_role(user, "master", "manager", "admin")
    pool = await get_pool()
    order = await pool.fetchrow("SELECT area_id, equipment_id FROM work_orders WHERE id=$1::uuid", str(oid))
    if not order:
        from app.errors import not_found
        raise not_found("Наряд не найден")
    body = AssigneeIn(area_id=order["area_id"], equipment_id=order["equipment_id"])
    base = await suggest_assignee(body, user)
    from app.ai.worker_recommendation import recommend as gw_recommend
    gw = await gw_recommend({"candidates": base["suggestions"]}, user.get("lang", "ru"))
    return {"data": {**gw, "suggestions": base["suggestions"]}}


@router.post("/ai/work-orders/{oid}/inspect", summary="Run AI inspection job for order")
async def inspect_order(oid: uuid.UUID, user: dict = Depends(get_current_user)):
    """Creates an ai_jobs row (PENDING->PROCESSING->COMPLETED/FAILED), never blocks the order."""
    require_role(user, "master", "manager", "admin", "worker")
    pool = await get_pool()
    order = await pool.fetchrow("SELECT status FROM work_orders WHERE id=$1::uuid", str(oid))
    if not order:
        from app.errors import not_found
        raise not_found("Наряд не найден")
    job = await pool.fetchrow(
        "INSERT INTO ai_jobs(work_order_id, status, provider) VALUES ($1::uuid,'PROCESSING','gateway') RETURNING id",
        str(oid))
    try:
        from app.db import get_pool as _gp
        p2 = await _gp()
        async with p2.acquire() as conn:
            async with conn.transaction():
                from app.services.ai_review import run_ai_review
                res = await run_ai_review(conn, str(oid))
        await pool.execute("UPDATE ai_jobs SET status='COMPLETED', result=$2::jsonb, updated_at=now() WHERE id=$1",
                           job["id"], __import__("json").dumps(res, default=str))
        return {"data": {"job_id": str(job["id"]), "status": "COMPLETED", "result": res}}
    except Exception as e:  # noqa: BLE001 - AI failure never breaks the order
        await pool.execute("UPDATE ai_jobs SET status='FAILED', error=$2, updated_at=now() WHERE id=$1", job["id"], str(e)[:500])
        return {"data": {"job_id": str(job["id"]), "status": "FAILED", "error": "AI_UNAVAILABLE"}}


@router.get("/ai/work-orders/{oid}/inspection", summary="Latest AI inspection result")
async def get_inspection(oid: uuid.UUID, user: dict = Depends(get_current_user)):
    from app.deps import assert_can_read_order
    await assert_can_read_order(user, str(oid))
    pool = await get_pool()
    job = await pool.fetchrow("SELECT * FROM ai_jobs WHERE work_order_id=$1::uuid ORDER BY created_at DESC LIMIT 1", str(oid))
    review = await pool.fetchrow("SELECT * FROM ai_reviews WHERE work_order_id=$1::uuid ORDER BY attempt DESC LIMIT 1", str(oid))
    def _s(r):
        import uuid as _u
        d = dict(r)
        for k, v in list(d.items()):
            if isinstance(v, _u.UUID):
                d[k] = str(v)
        return d
    return {"data": {"job": _s(job) if job else None, "review": _s(review) if review else None}}


@router.get("/ai/insights", summary="AI insights (alias)")
async def ai_insights():
    return {"data": {"hint": "Use GET /api/v1/analytics/insights and POST /api/v1/analytics/insights/generate"}}


@router.post("/ai/suggest-assignee")
async def suggest_assignee(body: AssigneeIn, user: dict = Depends(get_current_user)):
    require_role(user, "master")
    if str(body.area_id) not in user["areaIds"]:
        from app.errors import forbidden
        raise forbidden()
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT e.id, e.full_name, e.specialty, e.grade, e.current_status,
                  (SELECT count(*)::int FROM work_orders q WHERE (q.assignee_id=e.id OR q.crew_id=e.crew_id)
                   AND q.status IN ('issued','accepted','queued','paused','rework')) AS queue_count
           FROM employees e JOIN employee_areas ea ON ea.employee_id=e.id
           WHERE e.role='worker' AND e.enabled=true AND e.deleted_at IS NULL AND ea.area_id=$1::uuid
           ORDER BY CASE e.current_status WHEN 'free' THEN 0 WHEN 'queue' THEN 1 WHEN 'busy' THEN 2 ELSE 3 END LIMIT 10""",
        str(body.area_id))
    out = []
    for i, r in enumerate(rows[:3]):
        base = {"free": 0.45, "queue": 0.28, "busy": 0.10}.get(r["current_status"], 0.2)
        score = min(0.99, base + 0.08 + max(0, 0.20 - 0.04 * int(r["queue_count"])))
        out.append({"employee_id": str(r["id"]), "full_name": r["full_name"], "score": round(score, 3),
                    "confidence": max(0.5, 0.82 - 0.1 * i)})
    return {"suggestions": out}


@router.post("/ai/suggest-fault-code")
async def suggest_fault(body: FaultIn, user: dict = Depends(get_current_user)):
    require_role(user, "worker", "master")
    pool = await get_pool()
    codes = await pool.fetch("SELECT id, code, fault_group, name FROM fault_codes WHERE deleted_at IS NULL LIMIT 200")
    words = set(re.findall(r"[\w\-]{3,}", body.description.lower()))
    scored = []
    for c in codes:
        hay = f"{c['code']} {c['name']} {c['fault_group']}".lower()
        m = sum(1 for w in words if w in hay)
        if m:
            scored.append((m, c))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return {"fault_code_id": None, "confidence": 0.2, "alternatives": []}
    best = scored[0][1]
    return {"fault_code_id": str(best["id"]), "code": best["code"],
            "confidence": min(0.95, 0.45 + 0.12 * scored[0][0]),
            "alternatives": [{"id": str(c["id"]), "code": c["code"]} for _, c in scored[1:4]]}

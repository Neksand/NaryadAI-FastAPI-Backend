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

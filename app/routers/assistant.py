"""Rule-based RU/KK question-answering endpoint over operational aggregates."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.db import get_pool
from app.deps import get_current_user, require_role

router = APIRouter()

OPEN_STATUSES = "('done','ai_review','closed','rejected','cancelled')"


class AssistantFilters(BaseModel):
    area_id: uuid.UUID | None = None


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    filters: AssistantFilters | None = None


class Citation(BaseModel):
    work_order_id: str


class ChatResponse(BaseModel):
    answer_text: str
    citations: list[Citation]
    confidence: float


def _language(request: Request) -> str:
    raw = request.headers.get("accept-language", "ru")
    return "kk" if raw.strip().lower().startswith("kk") else "ru"


def _detect_intent(question: str) -> str:
    q = question.lower()
    if "просроч" in q or "мерзім" in q or "мерзим" in q:
        return "overdue"
    if "свобод" in q or "бос" in q:
        return "free"
    if "смен" in q or "ауысым" in q:
        return "shift"
    if "рейтинг" in q:
        return "rating"
    if "аномали" in q:
        return "anomalies"
    return "fallback"


def _area_clause(
    user: dict[str, Any], area_id: uuid.UUID | None, alias: str = "w"
) -> tuple[str, list[Any]]:
    if area_id is not None:
        return f"AND {alias}.area_id = $1::uuid", [str(area_id)]
    if user["role"] == "master":
        return f"AND {alias}.area_id = ANY($1::uuid[])", [user["areaIds"]]
    return "", []


def _fallback_text(lang: str) -> str:
    if lang == "kk":
        return (
            "Сұрағыңызды түсінбедім. Мерзімдер, ауысым, рейтинг немесе аномалиялар туралы сұраңыз."
        )
    return "Не понял вопрос. Спросите про сроки, смену, рейтинг или аномалии."


@router.post("/assistant/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> ChatResponse:
    require_role(user, "master", "manager", "admin")
    lang = _language(request)
    intent = _detect_intent(body.question)
    area_id = body.filters.area_id if body.filters else None
    pool = await get_pool()

    if intent == "overdue":
        clause, params = _area_clause(user, area_id)
        raw = await pool.fetch(
            f"""SELECT id, number FROM work_orders w
                WHERE due_at < now() AND status NOT IN {OPEN_STATUSES}
                {clause} ORDER BY due_at LIMIT 5""",
            *params,
        )
        rows = [dict(r) for r in raw]
        citations = [Citation(work_order_id=str(r["id"])) for r in rows]
        if not rows:
            text = "Мерзімі өткен нарядтар жоқ." if lang == "kk" else "Просроченных нарядов нет."
        else:
            nums = "; ".join(f"№{r['number']}" for r in rows)
            text = (
                f"Мерзімі өткен нарядтар: {len(rows)}. Топ: {nums}."
                if lang == "kk"
                else f"Просроченных нарядов: {len(rows)}. Топ просроченных: {nums}."
            )
        return ChatResponse(answer_text=text, citations=citations, confidence=1.0)

    if intent == "free":
        raw = await pool.fetch(
            """SELECT id, full_name FROM employees
               WHERE current_status='free' AND enabled = true AND deleted_at IS NULL
               ORDER BY full_name LIMIT 10"""
        )
        rows = [dict(r) for r in raw]
        names = [str(r.get("full_name") or r["id"]) for r in rows]
        if not rows:
            text = (
                "Қазір бос орындаушылар жоқ."
                if lang == "kk"
                else "Свободных исполнителей сейчас нет."
            )
        else:
            text = (
                f"Бос орындаушылар ({len(rows)}): {', '.join(names)}."
                if lang == "kk"
                else f"Свободные исполнители ({len(rows)}): {', '.join(names)}."
            )
        return ChatResponse(answer_text=text, citations=[], confidence=1.0)

    if intent == "shift":
        clause, params = _area_clause(user, area_id)
        row = await pool.fetchrow(
            f"""SELECT count(*) FILTER (WHERE status='issued')::int AS issued,
                       count(*) FILTER (WHERE status='closed')::int AS closed,
                       count(*) FILTER (WHERE due_at < now()
                         AND status NOT IN {OPEN_STATUSES})::int AS overdue
                FROM work_orders w WHERE true {clause}""",
            *params,
        )
        agg = dict(row) if row else {"issued": 0, "closed": 0, "overdue": 0}
        text = (
            f"Ауысым: берілген {agg['issued']}, жабылған {agg['closed']}, "
            f"мерзімі өткен {agg['overdue']}."
            if lang == "kk"
            else f"Смена: выдано {agg['issued']}, закрыто {agg['closed']}, "
            f"просрочено {agg['overdue']}."
        )
        return ChatResponse(answer_text=text, citations=[], confidence=1.0)

    if intent == "rating":
        clause, params = _area_clause(user, area_id)
        raw = await pool.fetch(
            f"""SELECT w.assignee_id AS id, MAX(e.full_name) AS name,
                       count(*) FILTER (WHERE w.status='closed')::int AS closed
                FROM work_orders w LEFT JOIN employees e ON e.id = w.assignee_id
                WHERE w.assignee_id IS NOT NULL {clause}
                GROUP BY w.assignee_id ORDER BY closed DESC LIMIT 3""",
            *params,
        )
        rows = [dict(r) for r in raw]
        if not rows:
            text = (
                "Рейтинг үшін деректер жоқ: жабылған нарядтар жоқ."
                if lang == "kk"
                else "Пока нет данных для рейтинга: закрытых нарядов нет."
            )
            return ChatResponse(answer_text=text, citations=[], confidence=1.0)
        parts = []
        for i, r in enumerate(rows, start=1):
            name = str(r.get("name") or r["id"])
            parts.append(f"{i}. {name} — {r['closed']}")
        text = (
            f"Жабылған нарядтар бойынша топ-3: {'; '.join(parts)}."
            if lang == "kk"
            else f"Топ-3 по закрытым нарядам: {'; '.join(parts)}."
        )
        return ChatResponse(answer_text=text, citations=[], confidence=1.0)

    if intent == "anomalies":
        raw = await pool.fetch(
            """SELECT headline, order_ids FROM insights
               ORDER BY created_at DESC LIMIT 5"""
        )
        rows = [dict(r) for r in raw]
        if not rows:
            text = "Жаңа аномалиялар жоқ." if lang == "kk" else "Свежих аномалий нет."
            return ChatResponse(answer_text=text, citations=[], confidence=1.0)
        headlines = [str(r.get("headline") or "—") for r in rows]
        seen: list[str] = []
        for r in rows:
            for oid in r.get("order_ids") or []:
                s = str(oid)
                if s not in seen:
                    seen.append(s)
                if len(seen) >= 10:
                    break
        citations = [Citation(work_order_id=s) for s in seen]
        text = (
            f"Соңғы аномалиялар ({len(rows)}): {'; '.join(headlines)}."
            if lang == "kk"
            else f"Свежие аномалии ({len(rows)}): {'; '.join(headlines)}."
        )
        return ChatResponse(answer_text=text, citations=citations, confidence=1.0)

    return ChatResponse(answer_text=_fallback_text(lang), citations=[], confidence=0.0)

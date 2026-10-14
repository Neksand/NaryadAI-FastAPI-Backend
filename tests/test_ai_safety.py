"""Safety post-rule: маркеры обхода защиты/заземления → доработка (FAIL), балл 1.

Место правила зафиксировано: app/services/ai_review.py (SAFETY_MARKERS +
_find_safety_marker), а не app/ai/mock.py — там нет LLM-ветки и текстовых
полей (только флаги duplicate/missing/material_flag/stale_exif).
Здесь вердикт needs_rework = FAIL/доработка, score 1 — независимо от модели.
"""

import asyncio
import json
from datetime import UTC, datetime

import pytest

from app import ai_providers
from app.services import ai_review


def run(coro):
    return asyncio.run(coro)


class SafetyConn:
    """Минимальный SQL-aware фейк под run_ai_review (с полем comment)."""

    def __init__(self, *, order=None, photos=None, materials=None, fault=None):
        self.order = {
            "id": "order-1",
            "status": "ai_review",
            "kind": "planned",
            "done_at": datetime(2026, 1, 1, 12, tzinfo=UTC),
            "fault_code_id": "fault-1",
            "description": "Течь масла в насосе",
            "work_done_text": "Устранили течь масла в насосе",
            "comment": "",
        }
        if order:
            self.order.update(order)
        self.photos = (
            photos
            if photos is not None
            else [
                {
                    "kind": "after",
                    "sha256": "after-hash",
                    "phash": None,
                    "exif_taken_at": None,
                    "object_key": "after.jpg",
                }
            ]
        )
        self.materials = materials or []
        self.fault = fault if fault is not None else {"code": "F-01", "name": "Утечка масла"}
        self.executions = []

    async def fetchrow(self, query, *args):
        if "FROM work_orders" in query:
            return self.order
        if "FROM fault_codes" in query:
            return self.fault
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def fetch(self, query, *args):
        if "SELECT sha256, phash, kind, exif_taken_at, object_key" in query:
            return self.photos
        if "SELECT work_order_id, phash FROM photos" in query:
            return []
        if "FROM material_writeoffs" in query:
            return self.materials
        raise AssertionError(f"Unexpected fetch query: {query}")

    async def fetchval(self, query, *args):
        if "SELECT work_order_id FROM photos WHERE sha256" in query:
            return None
        if "COALESCE(max(attempt),0)+1" in query:
            return 1
        raise AssertionError(f"Unexpected fetchval query: {query}")

    async def execute(self, query, *args):
        self.executions.append((query, args))
        if "UPDATE work_orders SET status='rework'" in query:
            self.order["status"] = "rework"

    def inserted_review(self):
        query, args = next((q, a) for q, a in self.executions if "INSERT INTO ai_reviews" in q)
        return {
            "verdict": args[2],
            "score": args[3],
            "checks": json.loads(args[6]),
            "explanation": args[7],
        }


@pytest.fixture(autouse=True)
def disable_external_ai(monkeypatch):
    async def no_provider(*_args):
        return None, "local"

    monkeypatch.setattr(ai_providers, "chat_text", no_provider)


@pytest.mark.parametrize("marker", ai_review.SAFETY_MARKERS)
def test_each_safety_marker_in_work_text_forces_rework_with_score_one(marker):
    conn = SafetyConn(order={"work_done_text": f"Насос отремонтирован, {marker}, работы завершены"})

    result = run(ai_review.run_ai_review(conn, "order-1"))

    review = conn.inserted_review()
    assert result == {"verdict": "needs_rework", "score": 1, "attempt": 1}
    assert review["score"] == 1
    assert review["checks"]["safety"] == {"pass": False, "confidence": 0.95, "marker": marker}
    assert conn.order["status"] == "rework"
    assert any("VALUES ($1::uuid,'ai','returned'" in q for q, _ in conn.executions)


def test_safety_marker_in_description_is_caught():
    conn = SafetyConn(order={"description": "Двигатель гудит, отключил защиту чтобы запустить"})

    result = run(ai_review.run_ai_review(conn, "order-1"))

    assert result["verdict"] == "needs_rework"
    assert result["score"] == 1
    assert conn.inserted_review()["checks"]["safety"]["pass"] is False


def test_safety_marker_in_comment_is_caught():
    conn = SafetyConn(order={"comment": "Работы выполнены без заземления, прошу принять"})

    result = run(ai_review.run_ai_review(conn, "order-1"))

    review = conn.inserted_review()
    assert result == {"verdict": "needs_rework", "score": 1, "attempt": 1}
    assert review["checks"]["safety"]["marker"] == "без заземления"


def test_safety_overrides_positive_llm_verdict(monkeypatch):
    async def llm_pass(*_args):
        return True, 0.99, "groq"

    monkeypatch.setattr(ai_review, "_llm_relevance", llm_pass)
    conn = SafetyConn(order={"work_done_text": "Всё работает, обошёл концевик для проверки"})

    result = run(ai_review.run_ai_review(conn, "order-1"))

    assert result["verdict"] == "needs_rework"
    assert result["score"] == 1


def test_safety_case_and_yo_insensitive():
    conn = SafetyConn(order={"work_done_text": "Для пуска ОБОШЕЛ КОНЦЕВИК временно"})

    result = run(ai_review.run_ai_review(conn, "order-1"))

    assert result["verdict"] == "needs_rework"
    assert result["score"] == 1


def test_clean_report_passes_without_safety_flag():
    conn = SafetyConn()

    result = run(ai_review.run_ai_review(conn, "order-1"))

    review = conn.inserted_review()
    assert result == {"verdict": "accepted", "score": 90, "attempt": 1}
    assert review["checks"]["safety"] == {"pass": True, "confidence": 0.95, "marker": None}
    assert conn.order["status"] == "ai_review"

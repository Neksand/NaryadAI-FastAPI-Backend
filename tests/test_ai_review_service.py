"""Deterministic decision tests for the maintenance AI-review rules."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from app import ai_providers, storage
from app.services import ai_review


def run(coro):
    return asyncio.run(coro)


class ReviewConn:
    """Small SQL-aware fake: exercise review decisions without PostgreSQL or AI calls."""

    def __init__(
        self,
        *,
        order=None,
        photos=None,
        exact_duplicate=None,
        nearby_photos=None,
        materials=None,
        fault=None,
        attempt=1,
    ):
        self.order = {
            "id": "order-1",
            "status": "ai_review",
            "kind": "planned",
            "done_at": datetime(2026, 1, 1, 12, tzinfo=UTC),
            "fault_code_id": "fault-1",
            "description": "Течь масла в насосе",
            "work_done_text": "Устранили течь масла в насосе",
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
        self.exact_duplicate = exact_duplicate
        self.nearby_photos = nearby_photos or []
        self.materials = materials or []
        self.fault = fault if fault is not None else {"code": "F-01", "name": "Утечка масла"}
        self.attempt = attempt
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
            return self.nearby_photos
        if "FROM material_writeoffs" in query:
            return self.materials
        raise AssertionError(f"Unexpected fetch query: {query}")

    async def fetchval(self, query, *args):
        if "SELECT work_order_id FROM photos WHERE sha256" in query:
            return self.exact_duplicate
        if "COALESCE(max(attempt),0)+1" in query:
            return self.attempt
        raise AssertionError(f"Unexpected fetchval query: {query}")

    async def execute(self, query, *args):
        self.executions.append((query, args))
        if "UPDATE work_orders SET status='rework'" in query:
            self.order["status"] = "rework"

    def inserted_review(self):
        query, args = next((q, a) for q, a in self.executions if "INSERT INTO ai_reviews" in q)
        return {
            "work_order_id": args[0],
            "attempt": args[1],
            "verdict": args[2],
            "score": args[3],
            "photo_score": args[4],
            "confidence": args[5],
            "checks": json.loads(args[6]),
            "explanation": args[7],
            "input_hash": args[8],
        }


@pytest.fixture(autouse=True)
def disable_external_ai(monkeypatch):
    async def no_provider(*_args):
        return None, "local"

    monkeypatch.setattr(ai_providers, "chat_text", no_provider)


def test_llm_relevance_parses_json_wrapped_in_provider_explanation(monkeypatch):
    async def provider(*_args):
        return 'Оценка: {"match": false, "confidence": 0.93}.', "groq"

    monkeypatch.setattr(ai_providers, "chat_text", provider)

    result = run(ai_review._llm_relevance("problem", "work", "F-01"))

    assert result == (False, 0.93, "groq")


@pytest.mark.parametrize(
    "payload",
    [
        '{"match":"false", "confidence":0.9}',
        '{"match":true, "confidence":true}',
        '{"match":false, "confidence":1.01}',
        "provider returned no JSON",
    ],
)
def test_invalid_llm_types_or_confidence_fall_back_to_local_rules(monkeypatch, payload):
    async def provider(*_args):
        return payload, "groq"

    monkeypatch.setattr(ai_providers, "chat_text", provider)

    assert run(ai_review._llm_relevance("problem", "work", "F-01")) == (None, None, "groq")


def test_hamming_distance_counts_changed_bits_and_fails_closed_on_bad_hashes():
    assert ai_review._hamming("0000", "003f") == 6
    assert ai_review._hamming("0", "0") == 0
    assert ai_review._hamming("not-hex", "0") == 64
    assert ai_review._hamming(None, "0") == 64


@pytest.mark.parametrize(
    ("problem", "work", "fault", "expected"),
    [
        ("Течь масла в насосе", "Устранили течь масла", "Утечка масла", True),
        ("Перегрев двигателя", "Заменили освещение", "Электрика", False),
        ("", "Заменили деталь", "", False),
    ],
)
def test_keyword_relevance_uses_meaningful_overlap(problem, work, fault, expected):
    match, confidence = ai_review._keyword_relevance(problem, work, fault)
    assert match is expected
    assert 0.0 <= confidence <= 0.9


@pytest.mark.parametrize("status", ["issued", "in_progress", "closed"])
def test_review_skips_orders_outside_ai_review_state(status):
    conn = ReviewConn(order={"status": status})

    assert run(ai_review.run_ai_review(conn, "order-1")) == {"skipped": True}
    assert conn.executions == []


def test_local_rules_accept_complete_relevant_work_and_hash_the_review_input():
    conn = ReviewConn(attempt=3)

    result = run(ai_review.run_ai_review(conn, "order-1"))

    review = conn.inserted_review()
    assert result == {"verdict": "accepted", "score": 90, "attempt": 3}
    assert review["attempt"] == 3
    assert review["photo_score"] == 4
    assert review["checks"]["completeness"]["pass"] is True
    assert review["checks"]["relevance"]["pass"] is True
    assert review["checks"]["relevance"]["provider"] == "local"
    assert len(review["input_hash"]) == 64
    assert conn.order["status"] == "ai_review"


def test_unplanned_work_without_after_photo_is_returned_with_missing_field_evidence():
    conn = ReviewConn(order={"kind": "unplanned"}, photos=[])

    result = run(ai_review.run_ai_review(conn, "order-1"))

    review = conn.inserted_review()
    assert result["verdict"] == "needs_rework"
    assert review["score"] == 70
    assert review["photo_score"] == 1
    assert review["checks"]["photos"]["pass"] is False
    assert conn.order["status"] == "ai_review"
    assert not any("UPDATE work_orders" in q for q, _ in conn.executions)


def test_exact_duplicate_photo_is_returned_even_when_text_is_complete():
    conn = ReviewConn(exact_duplicate="another-order")

    result = run(ai_review.run_ai_review(conn, "order-1"))

    review = conn.inserted_review()
    assert result == {"verdict": "needs_rework", "score": 60, "attempt": 1}
    assert review["photo_score"] == 1
    assert review["checks"]["photos"]["pass"] is False
    assert conn.order["status"] == "ai_review"


@pytest.mark.parametrize(("other_hash", "is_duplicate"), [("003f", True), ("007f", False)])
def test_near_duplicate_boundary_is_exactly_six_changed_bits(other_hash, is_duplicate):
    photo = {
        "kind": "after",
        "sha256": "new",
        "phash": "0000",
        "exif_taken_at": None,
        "object_key": "after.jpg",
    }
    conn = ReviewConn(
        photos=[photo], nearby_photos=[{"work_order_id": "other", "phash": other_hash}]
    )

    result = run(ai_review.run_ai_review(conn, "order-1"))

    assert (result["verdict"] == "needs_rework") is is_duplicate
    assert conn.inserted_review()["photo_score"] == (1 if is_duplicate else 4)


@pytest.mark.parametrize(("seconds_past_limit", "is_stale"), [(0, False), (1, True)])
def test_exif_freshness_uses_a_strict_twelve_hour_cutoff(seconds_past_limit, is_stale):
    done_at = datetime(2026, 1, 1, 12, tzinfo=UTC)
    photo = {
        "kind": "after",
        "sha256": "after",
        "phash": None,
        "exif_taken_at": done_at - timedelta(hours=12, seconds=seconds_past_limit),
        "object_key": "after.jpg",
    }
    conn = ReviewConn(order={"done_at": done_at}, photos=[photo])

    result = run(ai_review.run_ai_review(conn, "order-1"))

    review = conn.inserted_review()
    assert review["checks"]["photos"]["stale_exif"] is is_stale
    assert review["photo_score"] == (2 if is_stale else 4)
    assert result["verdict"] == ("accepted_with_remarks" if is_stale else "accepted")


@pytest.mark.parametrize(("quantity", "flagged"), [(2.0, False), (2.01, True)])
def test_material_anomaly_threshold_is_strictly_above_twice_typical_use(quantity, flagged):
    conn = ReviewConn(materials=[{"quantity": quantity, "typical_usage_per_order": 1.0}])

    result = run(ai_review.run_ai_review(conn, "order-1"))

    review = conn.inserted_review()
    assert review["checks"]["materials"]["pass"] is not flagged
    assert result["verdict"] == ("accepted_with_remarks" if flagged else "accepted")


def test_confident_text_disagreement_requests_master_decision_instead_of_auto_return(monkeypatch):
    async def disagreement(*_args):
        return False, 0.9, "groq"

    monkeypatch.setattr(ai_review, "_llm_relevance", disagreement)
    conn = ReviewConn()

    result = run(ai_review.run_ai_review(conn, "order-1"))

    review = conn.inserted_review()
    assert result["verdict"] == "needs_master_review"
    assert result["score"] is None
    assert review["checks"]["relevance"] == {"pass": False, "confidence": 0.9, "provider": "groq"}
    assert conn.order["status"] == "ai_review"


def test_valid_vision_evidence_and_provider_note_are_preserved(monkeypatch):
    async def compare(*_args):
        return (
            '{"note":"Дефект устранён", "score": 5, "confidence": 0.9, "fixed": true}',
            "estimin3n",
        )

    class FakeS3:
        def get_object(self, **_kwargs):
            return {"Body": type("Body", (), {"read": lambda self: b"image"})()}

    monkeypatch.setattr(ai_providers, "compare_photos", compare)
    monkeypatch.setattr(storage, "get_s3", lambda: FakeS3())
    photos = [
        {
            "kind": "before",
            "sha256": "before",
            "phash": None,
            "exif_taken_at": None,
            "object_key": "before.jpg",
        },
        {
            "kind": "after",
            "sha256": "after",
            "phash": None,
            "exif_taken_at": None,
            "object_key": "after.jpg",
        },
    ]
    conn = ReviewConn(photos=photos)

    run(ai_review.run_ai_review(conn, "order-1"))

    review = conn.inserted_review()
    assert review["photo_score"] == 5
    assert review["checks"]["photos"]["vision"] == "Дефект устранён"
    assert "зрение Estimin3n" in review["explanation"]


def test_unrelated_local_text_never_receives_accepted_score():
    conn = ReviewConn(
        order={"description": "Перегрев двигателя", "work_done_text": "Покрасил ограждение"}
    )
    result = run(ai_review.run_ai_review(conn, "order-1"))
    assert result["verdict"] == "needs_master_review"
    assert result["score"] is None
    assert conn.order["status"] == "ai_review"
    assert "не проверено моделью" in conn.inserted_review()["explanation"]


@pytest.mark.parametrize(
    "response",
    [
        '{"score": 9, "fixed": true, "confidence": 0.99}',
        '{"score": true, "fixed": true, "confidence": 0.99}',
        '{"score": 5, "fixed": true, "confidence": 0.2}',
        '{"score": 5, "fixed": true}',
        '{"score": 1, "fixed": false, "confidence": 0.9}',
        "not JSON",
        "[1,2,3]",
    ],
)
def test_uncertain_invalid_or_negative_vision_requires_human_review(monkeypatch, response):
    async def compare(*_args):
        return response, "gemini-vision"

    class FakeS3:
        def get_object(self, **_kwargs):
            return {"Body": type("Body", (), {"read": lambda self: b"image"})()}

    monkeypatch.setattr(ai_providers, "compare_photos", compare)
    monkeypatch.setattr(storage, "get_s3", lambda: FakeS3())
    photos = [
        {
            "kind": kind,
            "sha256": kind,
            "phash": None,
            "exif_taken_at": None,
            "object_key": f"{kind}.jpg",
        }
        for kind in ("before", "after")
    ]
    conn = ReviewConn(photos=photos)
    result = run(ai_review.run_ai_review(conn, "order-1"))
    assert result["verdict"] == "needs_master_review"
    assert result["score"] is None
    assert conn.order["status"] == "ai_review"


def test_identical_before_and_after_photo_is_detected_within_same_order(monkeypatch):
    monkeypatch.setattr(storage, "get_s3", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    photos = [
        {
            "kind": kind,
            "sha256": "same-image",
            "phash": None,
            "exif_taken_at": None,
            "object_key": f"{kind}.jpg",
        }
        for kind in ("before", "after")
    ]
    conn = ReviewConn(photos=photos)
    result = run(ai_review.run_ai_review(conn, "order-1"))
    assert result["verdict"] == "needs_rework"
    assert conn.inserted_review()["checks"]["photos"]["pass"] is False

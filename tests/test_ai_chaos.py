"""Хаос-тест AI-провайдеров: ревью всегда завершается, мастер закрывает, outbox жив.

Что проверяется:
(а) unit-хаос на fake-conn (паттерн tests/test_ai_safety.py): текстовый и вижн
    провайдеры случайно деградируют (monkeypatch, несколько сидов) — ревью
    всегда завершается с verdict, строка ai_reviews и событие outbox записаны;
(б) вижн-провайдер взрывается исключением — run_ai_review содержит это
    внутренним try/except (vision-блок app/services/ai_review.py);
(в) scratch-БД naryadai_chaos: падающий run_ai_review уходит в dead-letter
    needs_master_review, мастер закрывает через override тем же SQL, что роут
    close, а цикл outbox после этого публикует последующие события.

Честная граница: исключение из *текстового* провайдера НЕ гасится внутри
run_ai_review (прямой await _llm_relevance, app/services/ai_review.py:210-212)
и содержится уровнем выше — background._handle_ai_review_event ловит его,
растит attempts и пишет dead-letter (app/services/background.py:241-263).
Поэтому text_raise покрыт здесь только сценарием (в), а не unit-хаосом.

Требуется scratch-БД naryadai_chaos (НЕ naryadai / naryadai_stand /
naryadai_p0 / naryadai_ver / naryadai_ins / naryadai_ins2):
    DATABASE_URL=postgresql://naryadai:naryadai@localhost:15432/naryadai_chaos
Без неё DB-сценарий пропускается, unit-хаос выполняется всегда.
"""

import asyncio
import json
import os
import random
import uuid
from datetime import UTC, datetime

import asyncpg
import pytest

from app import ai_providers
from app.services import ai_review as ai_review_module
from app.services import background

ALLOWED_VERDICTS = frozenset(
    {"accepted", "accepted_with_remarks", "needs_rework", "needs_master_review"}
)

CHAOS_SEEDS = (7, 42, 123, 2026)
DRAWS_PER_SEED = 8


def run(coro):
    return asyncio.run(coro)


class ChaosConn:
    """SQL-aware фейк под run_ai_review (паттерн SafetyConn из test_ai_safety.py)."""

    def __init__(self):
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
        self.photos = [
            {
                "kind": "before",
                "sha256": "before-hash",
                "phash": None,
                "exif_taken_at": None,
                "object_key": "before.jpg",
            },
            {
                "kind": "after",
                "sha256": "after-hash",
                "phash": None,
                "exif_taken_at": None,
                "object_key": "after.jpg",
            },
        ]
        self.materials = []
        self.fault = {"code": "F-01", "name": "Утечка масла"}
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

    def review_inserts(self):
        return [(q, a) for q, a in self.executions if "INSERT INTO ai_reviews" in q]

    def outbox_inserts(self):
        return [(q, a) for q, a in self.executions if "INSERT INTO outbox_events" in q]


class _FakeBody:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data


class _FakeS3:
    def get_object(self, Bucket: str, Key: str) -> dict:
        return {"Body": _FakeBody(b"fake-image-bytes")}


def _apply_draw(monkeypatch: pytest.MonkeyPatch, rng: random.Random) -> dict:
    """Один случайный сценарий деградации; возвращает описание для диагностики."""
    import app.storage as storage_module

    monkeypatch.setattr(storage_module, "get_s3", lambda: _FakeS3())
    text_mode = rng.choice(["text_none", "text_garbage", "text_ok"])
    vision_mode = rng.choice(
        ["vision_none", "vision_raise", "vision_garbage", "vision_low", "vision_ok"]
    )

    async def fake_chat_text(system: str, user: str):
        if text_mode == "text_none":
            return None, "local"
        if text_mode == "text_garbage":
            return "@@@ not json", "chaos-text"
        return '{"match": true, "confidence": 0.9}', "chaos-text"

    async def fake_compare(before: bytes, after: bytes, context: str):
        if vision_mode == "vision_none":
            return None, "local"
        if vision_mode == "vision_raise":
            raise RuntimeError("chaos vision exploded")
        if vision_mode == "vision_garbage":
            return "not a json at all", "chaos-vision"
        if vision_mode == "vision_low":
            return (
                '{"score": 1, "confidence": 0.9, "fixed": false, "note": "chaos defect visible"}',
                "chaos-vision",
            )
        return (
            '{"score": 5, "confidence": 0.95, "fixed": true, "note": "chaos ok"}',
            "chaos-vision",
        )

    monkeypatch.setattr(ai_providers, "chat_text", fake_chat_text)
    monkeypatch.setattr(ai_providers, "compare_photos", fake_compare)
    return {"text": text_mode, "vision": vision_mode}


@pytest.mark.parametrize("seed", CHAOS_SEEDS)
def test_chaos_providers_review_always_finishes_with_verdict(
    monkeypatch: pytest.MonkeyPatch, seed: int
) -> None:
    """Случайные деградации провайдеров: verdict присутствует, outbox-событие записано."""
    rng = random.Random(seed)
    for _ in range(DRAWS_PER_SEED):
        draw = _apply_draw(monkeypatch, rng)
        conn = ChaosConn()
        result = run(ai_review_module.run_ai_review(conn, "order-1"))
        assert result.get("verdict") in ALLOWED_VERDICTS, f"no verdict, draw={draw}"
        assert "score" in result and isinstance(result.get("attempt"), int)
        assert len(conn.review_inserts()) == 1, f"review row missing, draw={draw}"
        outbox = conn.outbox_inserts()
        assert len(outbox) == 1, f"outbox event missing, draw={draw}"
        assert "order.ai_review_ready" in outbox[0][0]
        payload = json.loads(outbox[0][1][1])
        assert payload["verdict"] == result["verdict"]


def test_chaos_vision_explosion_is_contained(monkeypatch: pytest.MonkeyPatch) -> None:
    """Взрыв вижн-провайдера исключением не роняет ревью: verdict присутствует."""

    async def dead_text(system: str, user: str):
        return None, "local"

    async def exploding_vision(before: bytes, after: bytes, context: str):
        raise RuntimeError("vision provider exploded mid-review")

    import app.storage as storage_module

    monkeypatch.setattr(storage_module, "get_s3", lambda: _FakeS3())
    monkeypatch.setattr(ai_providers, "chat_text", dead_text)
    monkeypatch.setattr(ai_providers, "compare_photos", exploding_vision)
    conn = ChaosConn()
    result = run(ai_review_module.run_ai_review(conn, "order-1"))
    assert result.get("verdict") in ALLOWED_VERDICTS
    assert len(conn.review_inserts()) == 1
    assert len(conn.outbox_inserts()) == 1


def _scratch_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if "naryadai_chaos" not in url:
        pytest.skip("requires scratch DB naryadai_chaos (refusing to touch other databases)")
    return url


async def _seed_review_order(conn: asyncpg.Connection, tag: str) -> dict:
    uid = uuid.uuid4().hex[:8]
    area_id = await conn.fetchval(
        "INSERT INTO areas(name, code) VALUES ($1,$2) RETURNING id",
        f"Chaos area {tag} {uid}",
        f"chaos-{tag}-{uid}",
    )
    master_id = await conn.fetchval(
        "INSERT INTO employees(login, pin_hash, full_name, role) VALUES ($1,'x',$2,'master')"
        " RETURNING id",
        f"chaos-master-{tag}-{uid}",
        f"Chaos Master {tag}",
    )
    worker_id = await conn.fetchval(
        "INSERT INTO employees(login, pin_hash, full_name, role) VALUES ($1,'x',$2,'worker')"
        " RETURNING id",
        f"chaos-worker-{tag}-{uid}",
        f"Chaos Worker {tag}",
    )
    equip_id = await conn.fetchval(
        "INSERT INTO equipment(name, inventory_no, area_id, type, criticality)"
        " VALUES ($1,$2,$3,'pump','C') RETURNING id",
        f"Chaos pump {tag} {uid}",
        f"chaos-inv-{tag}-{uid}",
        area_id,
    )
    fault_id = await conn.fetchval(
        "INSERT INTO fault_codes(code, fault_group, name) VALUES ($1,'Э',$2) RETURNING id",
        f"CHAOS-{tag}-{uid}",
        "Утечка масла",
    )
    number = await conn.fetchval("SELECT COALESCE(max(number),0)+1 FROM work_orders")
    order_id = await conn.fetchval(
        """INSERT INTO work_orders(number, kind, description, area_id, equipment_id,
               assignee_id, master_id, priority, due_at, status,
               work_done_text, fault_code_id, done_at)
           VALUES ($1,'planned',$2,$3,$4,$5,$6,'normal',now()+interval '4 hours',
               'ai_review',$7,$8,now()) RETURNING id""",
        number,
        "Течь масла в насосе",
        area_id,
        equip_id,
        worker_id,
        master_id,
        "Устранили течь масла в насосе",
        fault_id,
    )
    await conn.execute(
        """INSERT INTO photos(work_order_id, kind, object_key, content_type,
               size_bytes, sha256, author_id)
           VALUES ($1,'after',$2,'image/jpeg',100,$3,$4)""",
        order_id,
        f"chaos-{tag}-{uid}-after.jpg",
        f"chaossha-{tag}-{uid}",
        worker_id,
    )
    event_id = await conn.fetchval(
        "INSERT INTO outbox_events(event, channels, payload)"
        " VALUES ('ai.review_requested','{}',$1::jsonb) RETURNING id",
        json.dumps({"work_order_id": str(order_id)}),
    )
    return {
        "order_id": str(order_id),
        "event_id": str(event_id),
        "master_id": str(master_id),
    }


async def _claim_mine(conn: asyncpg.Connection, event_id: str, timeout: float = 20.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(f"own event {event_id} was never claimable")
        row = await background._claim_outbox_event(conn)
        if row is not None and str(row["id"]) == event_id:
            return row
        if row is not None:
            await conn.execute(
                "UPDATE outbox_events SET locked_until=NULL WHERE id=$1 AND published_at IS NULL",
                row["id"],
            )
        await asyncio.sleep(0.2)


async def _wait_until(predicate, timeout: float, step: float = 0.2) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if await predicate():
            return
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(f"condition not met within {timeout:.1f}s")
        await asyncio.sleep(step)


def test_chaos_dead_letter_override_and_loop_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Падение ревью -> dead-letter -> override-закрытие мастером -> цикл outbox жив."""
    url = _scratch_url()
    monkeypatch.setattr(background, "AI_REVIEW_MAX_ATTEMPTS", 2)

    async def always_down(_conn: object, _work_order_id: str) -> dict:
        raise RuntimeError("chaos: all AI providers down")

    monkeypatch.setattr(ai_review_module, "run_ai_review", always_down)

    async def main() -> dict:
        from app.db import close_pool

        conn = await asyncpg.connect(url)
        try:
            seed = await _seed_review_order(conn, "down")
            row = await _claim_mine(conn, seed["event_id"])
            await background._handle_ai_review_event(conn, row)
            row2 = await _claim_mine(conn, seed["event_id"])
            await background._handle_ai_review_event(conn, row2)
            final = await conn.fetchrow(
                "SELECT attempts, published_at, last_error FROM outbox_events WHERE id=$1::uuid",
                seed["event_id"],
            )
            assert final["published_at"] is not None, "dead-letter must mark event done"
            assert str(final["last_error"]).startswith("dead_letter_after_2_attempts")
            reviews = await conn.fetch(
                "SELECT id, verdict, score, created_at FROM ai_reviews"
                " WHERE work_order_id=$1::uuid ORDER BY attempt",
                seed["order_id"],
            )
            assert len(reviews) == 1
            assert reviews[0]["verdict"] == "needs_master_review"
            assert reviews[0]["score"] is None
            await conn.execute(
                "UPDATE ai_reviews SET master_decision='override', master_score=$2,"
                " master_comment=$3, decided_by=$4::uuid, decided_at=now() WHERE id=$1",
                reviews[0]["id"],
                85,
                "Принято мастером после недоступности ИИ",
                seed["master_id"],
            )
            await conn.execute(
                "UPDATE work_orders SET status='closed', closed_at=now() WHERE id=$1::uuid",
                seed["order_id"],
            )
            status = await conn.fetchval(
                "SELECT status FROM work_orders WHERE id=$1::uuid", seed["order_id"]
            )
        finally:
            await conn.close()

        monkeypatch.setattr(ai_review_module, "run_ai_review", ai_review_module.run_ai_review)

        async def no_text_provider(*_args: object) -> tuple:
            return None, "local"

        monkeypatch.setattr(ai_providers, "chat_text", no_text_provider)
        driver = await asyncpg.connect(url)
        try:
            healthy = await _seed_review_order(driver, "up")
        finally:
            await driver.close()
        task = asyncio.create_task(background.outbox_loop())
        try:

            async def healthy_published() -> bool:
                c = await asyncpg.connect(url)
                try:
                    state = await c.fetchrow(
                        "SELECT published_at, locked_until FROM outbox_events WHERE id=$1::uuid",
                        healthy["event_id"],
                    )
                    return state["published_at"] is not None
                finally:
                    await c.close()

            await _wait_until(healthy_published, timeout=25.0)
            check = await asyncpg.connect(url)
            try:
                rows = await check.fetch(
                    "SELECT verdict FROM ai_reviews WHERE work_order_id=$1::uuid",
                    healthy["order_id"],
                )
                assert rows, "loop must review events arriving after chaos failure"
                assert rows[0]["verdict"] in ALLOWED_VERDICTS
            finally:
                await check.close()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await close_pool()
        return {"status": status}

    result = asyncio.run(main())
    assert result["status"] == "closed"

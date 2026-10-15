"""P0: outbox_loop robustness — claim/processing split, timeout, dead-letter.

(а) провайдер падает посреди ревью → attempts растёт, цикл жив,
    повторные события обрабатываются;
(б) исчерпание попыток → fallback-строка needs_master_review и мастер
    может закрыть через override;
(в) висящий внешний вызов (долгая задержка + короткий timeout) → цикл не клинит.

Требуется scratch-БД naryadai_p0 (НЕ naryadai / naryadai_stand):
    DATABASE_URL=postgresql://naryadai:naryadai@localhost:15432/naryadai_p0
"""

import asyncio
import json
import os
import uuid

import asyncpg
import pytest

from app import ai_providers
from app.services import ai_review as ai_review_module
from app.services import background


def _scratch_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if "naryadai_p0" not in url:
        pytest.skip("requires scratch DB naryadai_p0 (refusing to touch other databases)")
    return url


async def _seed_review_order(conn: asyncpg.Connection, tag: str) -> dict:
    """Минимальный наряд в статусе ai_review + событие ai.review_requested."""
    uid = uuid.uuid4().hex[:8]
    area_id = await conn.fetchval(
        "INSERT INTO areas(name, code) VALUES ($1,$2) RETURNING id",
        f"P0 area {tag} {uid}",
        f"p0-{tag}-{uid}",
    )
    master_id = await conn.fetchval(
        "INSERT INTO employees(login, pin_hash, full_name, role) VALUES ($1,'x',$2,'master') RETURNING id",
        f"p0-master-{tag}-{uid}",
        f"P0 Master {tag}",
    )
    worker_id = await conn.fetchval(
        "INSERT INTO employees(login, pin_hash, full_name, role) VALUES ($1,'x',$2,'worker') RETURNING id",
        f"p0-worker-{tag}-{uid}",
        f"P0 Worker {tag}",
    )
    equip_id = await conn.fetchval(
        "INSERT INTO equipment(name, inventory_no, area_id, type, criticality)"
        " VALUES ($1,$2,$3,'pump','C') RETURNING id",
        f"P0 pump {tag} {uid}",
        f"p0-inv-{tag}-{uid}",
        area_id,
    )
    fault_id = await conn.fetchval(
        "INSERT INTO fault_codes(code, fault_group, name) VALUES ($1,'Э',$2) RETURNING id",
        f"P0-{tag}-{uid}",
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
        f"p0-{tag}-{uid}-after.jpg",
        f"p0sha-{tag}-{uid}",
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
        "worker_id": str(worker_id),
    }


async def _event_state(conn: asyncpg.Connection, event_id: str) -> dict:
    row = await conn.fetchrow(
        "SELECT attempts, published_at, locked_until, last_error"
        " FROM outbox_events WHERE id=$1::uuid",
        event_id,
    )
    return dict(row)


async def _reviews(conn: asyncpg.Connection, order_id: str) -> list:
    rows = await conn.fetch(
        "SELECT verdict, score, explanation, model, attempt, created_at"
        " FROM ai_reviews WHERE work_order_id=$1::uuid ORDER BY attempt",
        order_id,
    )
    return [dict(r) for r in rows]


async def _wait_until(predicate, timeout: float, step: float = 0.1) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if await predicate():
            return
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(f"condition not met within {timeout:.1f}s")
        await asyncio.sleep(step)


def _stop_local_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_text_provider(*_args: object) -> tuple:
        return None, "local"

    monkeypatch.setattr(ai_providers, "chat_text", no_text_provider)


async def _claim_mine(
    conn: asyncpg.Connection, event_id: str, timeout: float = 20.0
) -> asyncpg.Record:
    """Claim именно своего события по id; чужие строки вообще не трогаем.

    FIFO-claim здесь не годится: соседние тесты оставляют неопубликованные
    side-события старше нашего, и oldest-first вечно возвращает их.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(f"own event {event_id} was never claimable")
        row = await conn.fetchrow(
            """UPDATE outbox_events SET locked_until=now()+interval '30 seconds'
               WHERE id=$1::uuid AND published_at IS NULL
               AND (locked_until IS NULL OR locked_until < now())
               RETURNING *""",
            event_id,
        )
        if row is not None:
            return row
        await asyncio.sleep(0.2)


def test_provider_failure_keeps_loop_alive_and_attempts_grow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(а) Падение run_ai_review: attempts растёт, claim не откатывается,
    повторное событие обрабатывается тем же циклом.

    Отказ — ручным claim+handle (залоченную строку чужой воркер украсть
    не может), живость цикла — реальным outbox_loop с финальными проверками
    состояния, устойчивыми к чужому воркеру в shared-кластере.
    """
    url = _scratch_url()
    _stop_local_ai(monkeypatch)
    real_review = ai_review_module.run_ai_review

    async def flaky(_conn: object, _work_order_id: str) -> dict:
        raise RuntimeError("provider exploded mid-review")

    monkeypatch.setattr(ai_review_module, "run_ai_review", flaky)

    async def main() -> None:
        from app.db import close_pool

        conn = await asyncpg.connect(url)
        try:
            first = await _seed_review_order(conn, "a1")
            row = await _claim_mine(conn, first["event_id"])
            await background._handle_ai_review_event(conn, row)
            failed = await _event_state(conn, first["event_id"])
            assert int(failed["attempts"]) == 1, "attempts must grow on processing failure"
            assert failed["published_at"] is None
            assert "provider exploded" in str(failed["last_error"])
            assert failed["locked_until"] is None, "lock must not hang after failure"
            assert await _reviews(conn, first["order_id"]) == []
            # Лок снят — событие сразу доступно для повторного claim (не висит).
            row2 = await _claim_mine(conn, first["event_id"])
            assert str(row2["id"]) == first["event_id"]
            await conn.execute("UPDATE outbox_events SET locked_until=NULL WHERE id=$1", row2["id"])
        finally:
            await conn.close()

        monkeypatch.setattr(ai_review_module, "run_ai_review", real_review)
        driver = await asyncpg.connect(url)
        try:
            second = await _seed_review_order(driver, "a2")
        finally:
            await driver.close()
        task = asyncio.create_task(background.outbox_loop())
        try:

            async def both_published() -> bool:
                c = await asyncpg.connect(url)
                try:
                    s1 = await _event_state(c, first["event_id"])
                    s2 = await _event_state(c, second["event_id"])
                    return s1["published_at"] is not None and s2["published_at"] is not None
                finally:
                    await c.close()

            await _wait_until(both_published, timeout=20.0)
            check = await asyncpg.connect(url)
            try:
                s1 = await _event_state(check, first["event_id"])
                s2 = await _event_state(check, second["event_id"])
                assert int(s1["attempts"]) >= 1, "failure must stay recorded"
                assert s1["locked_until"] is None and s2["locked_until"] is None
                assert await _reviews(check, first["order_id"]), "retry must review first order"
                assert await _reviews(check, second["order_id"]), "loop must process next events"
            finally:
                await check.close()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await close_pool()

    asyncio.run(main())


def test_attempts_exhaustion_writes_fallback_and_master_can_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(б) После N попыток — fallback needs_master_review; мастер закрывает через override."""
    url = _scratch_url()
    monkeypatch.setattr(background, "AI_REVIEW_MAX_ATTEMPTS", 2)

    async def always_down(_conn: object, _work_order_id: str) -> dict:
        raise RuntimeError("AI provider down")

    monkeypatch.setattr(ai_review_module, "run_ai_review", always_down)

    async def main() -> dict:
        conn = await asyncpg.connect(url)
        try:
            seed = await _seed_review_order(conn, "b")
            row = await _claim_mine(conn, seed["event_id"])
            await background._handle_ai_review_event(conn, row)
            after_first = await _event_state(conn, seed["event_id"])
            assert int(after_first["attempts"]) == 1
            assert after_first["published_at"] is None
            assert await _reviews(conn, seed["order_id"]) == []
            # Лок снят — событие сразу доступно для повторного claim (не висит).
            row2 = await _claim_mine(conn, seed["event_id"])
            await background._handle_ai_review_event(conn, row2)
            final = await _event_state(conn, seed["event_id"])
            assert final["published_at"] is not None, "dead-letter must mark event processed"
            assert str(final["last_error"]).startswith("dead_letter_after_2_attempts")
            rows = await _reviews(conn, seed["order_id"])
            assert len(rows) == 1
            fallback = rows[0]
            assert fallback["verdict"] == "needs_master_review"
            assert fallback["score"] is None
            assert "проверка мастером" in fallback["explanation"]
            # Гейт закрытия из app/routers/work_orders.py: ревью свежее done_at.
            order = await conn.fetchrow(
                "SELECT done_at FROM work_orders WHERE id=$1::uuid", seed["order_id"]
            )
            assert fallback["created_at"] >= order["done_at"]
            # Закрытие через override тем же SQL, что выполняет роут close.
            latest = await conn.fetchrow(
                "SELECT id FROM ai_reviews WHERE work_order_id=$1::uuid"
                " ORDER BY attempt DESC LIMIT 1",
                seed["order_id"],
            )
            await conn.execute(
                "UPDATE ai_reviews SET master_decision='override', master_score=$2,"
                " master_comment=$3, decided_by=$4::uuid, decided_at=now() WHERE id=$1",
                latest["id"],
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
            # Повторный fallback идемпотентен — дубликата нет.
            again = await ai_review_module.write_dead_letter_review(
                conn, seed["order_id"], "repeat"
            )
            assert again["dead_letter"] is False
            assert len(await _reviews(conn, seed["order_id"])) == 1
            return {"status": status, "fallback": fallback}
        finally:
            await conn.close()

    result = asyncio.run(main())
    assert result["status"] == "closed"


def test_hanging_provider_does_not_stall_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """(в) Висящий внешний вызов + короткий timeout: цикл не клинит,
    соседние события обрабатываются, висящее уходит в dead-letter.

    Таймаут и dead-letter — ручным claim+handle (детерминированно, чужой
    воркер залоченную строку украсть не может); живость цикла под висящим
    соседом — реальным outbox_loop с ограничением общего времени.
    """
    url = _scratch_url()
    _stop_local_ai(monkeypatch)
    monkeypatch.setattr(background, "AI_REVIEW_TIMEOUT_SECONDS", 0.25)
    monkeypatch.setattr(background, "AI_REVIEW_MAX_ATTEMPTS", 2)
    real_review = ai_review_module.run_ai_review
    hanging_order: list = []

    async def selective(conn: object, work_order_id: str) -> dict:
        if work_order_id in hanging_order:
            await asyncio.sleep(30.0)
            return {"skipped": True}
        return await real_review(conn, work_order_id)

    monkeypatch.setattr(ai_review_module, "run_ai_review", selective)

    async def main() -> dict:
        from app.db import close_pool

        conn = await asyncpg.connect(url)
        try:
            seed = await _seed_review_order(conn, "c0")
            hanging_order.append(seed["order_id"])
            row = await _claim_mine(conn, seed["event_id"])
            await background._handle_ai_review_event(conn, row)
            timed_out = await _event_state(conn, seed["event_id"])
            assert int(timed_out["attempts"]) == 1
            assert timed_out["published_at"] is None
            assert "timed out" in str(timed_out["last_error"])
            row2 = await _claim_mine(conn, seed["event_id"])
            await background._handle_ai_review_event(conn, row2)
            dead = await _event_state(conn, seed["event_id"])
            assert dead["published_at"] is not None, "hanging event must dead-letter"
            assert str(dead["last_error"]).startswith("dead_letter_after_2_attempts")
            assert "timed out" in str(dead["last_error"])
            fallback = await _reviews(conn, seed["order_id"])
            assert len(fallback) == 1 and fallback[0]["verdict"] == "needs_master_review"
        finally:
            await conn.close()

        started = asyncio.get_running_loop().time()
        setup = await asyncpg.connect(url)
        try:
            hanging = await _seed_review_order(setup, "c1")
            healthy = await _seed_review_order(setup, "c2")
            hanging_order.append(hanging["order_id"])
        finally:
            await setup.close()
        monkeypatch.setattr(background, "AI_REVIEW_MAX_ATTEMPTS", 5)
        task = asyncio.create_task(background.outbox_loop())
        try:

            async def healthy_done() -> bool:
                c = await asyncpg.connect(url)
                try:
                    state = await _event_state(c, healthy["event_id"])
                    return state["published_at"] is not None
                finally:
                    await c.close()

            await _wait_until(healthy_done, timeout=20.0)
            elapsed = asyncio.get_running_loop().time() - started
            check = await asyncpg.connect(url)
            try:
                state = await _event_state(check, healthy["event_id"])
                assert elapsed < 15.0, f"loop stalled on hanging provider: {elapsed:.1f}s"
                assert int(state["attempts"]) == 0
                assert await _reviews(check, healthy["order_id"]), (
                    "healthy event must be processed despite hanging neighbour"
                )
            finally:
                await check.close()
            return {"elapsed": elapsed}
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await close_pool()

    asyncio.run(main())


def test_dead_letter_helper_is_idempotent_and_master_closable() -> None:
    """Прямая проверка write_dead_letter_review без цикла: контент и идемпотентность."""
    url = _scratch_url()

    async def main() -> None:
        conn = await asyncpg.connect(url)
        try:
            seed = await _seed_review_order(conn, "d")
            first = await ai_review_module.write_dead_letter_review(conn, seed["order_id"], "boom")
            assert first == {
                "verdict": "needs_master_review",
                "attempt": 1,
                "dead_letter": True,
            }
            rows = await _reviews(conn, seed["order_id"])
            assert len(rows) == 1
            assert rows[0]["score"] is None
            assert (
                "недоступна" in rows[0]["explanation"]
                or "недоступности" in rows[0]["explanation"]
                or "ИИ" in rows[0]["explanation"]
            )
            second = await ai_review_module.write_dead_letter_review(conn, seed["order_id"], "boom")
            assert second["dead_letter"] is False
            assert len(await _reviews(conn, seed["order_id"])) == 1
        finally:
            await conn.close()

    asyncio.run(main())

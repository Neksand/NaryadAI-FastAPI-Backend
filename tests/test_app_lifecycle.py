"""Application lifespan drains background tasks before closing the DB pool."""

import asyncio

from app import main
from app.services import background


def test_lifespan_cancels_and_awaits_each_worker_before_pool_close(monkeypatch):
    started = set()
    stopped = set()
    calls = []

    async def worker(name):
        started.add(name)
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            stopped.add(name)
            raise

    async def outbox():
        await worker("outbox")

    async def deadline():
        await worker("deadline")

    async def insights():
        await worker("insights")

    async def close_pool():
        assert stopped == {"outbox", "deadline", "insights"}
        calls.append("pool_closed")

    monkeypatch.setattr(background, "outbox_loop", outbox)
    monkeypatch.setattr(background, "deadline_loop", deadline)
    monkeypatch.setattr(background, "insights_loop", insights)
    monkeypatch.setattr(main, "ensure_upload_bucket", lambda: calls.append("bucket_checked"))
    monkeypatch.setattr(main, "close_pool", close_pool)

    async def exercise():
        async with main.lifespan(None):
            for _ in range(20):
                if started == {"outbox", "deadline", "insights"}:
                    break
                await asyncio.sleep(0)
            assert started == {"outbox", "deadline", "insights"}

    asyncio.run(exercise())
    assert calls == ["bucket_checked", "pool_closed"]

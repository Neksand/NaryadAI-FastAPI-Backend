from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from scripts.demo_dataset import history_specs


def test_seed_is_reproducible_and_spans_three_months():
    now = datetime(2026, 10, 14, 8, tzinfo=UTC)
    rows = history_specs(now)
    assert rows == history_specs(now)
    assert len(rows) == 600
    assert max(r["issued_at"] for r in rows) - min(r["issued_at"] for r in rows) > timedelta(
        days=90
    )
    assert all(r["issued_at"] < now for r in rows)
    assert all("СИНТЕТИЧЕСКИЙ" in r["description"] for r in rows)


def test_ground_truth_patterns_exist_in_records():
    now = datetime(2026, 10, 14, 8, tzinfo=UTC)
    rows = history_specs(now)
    conveyor = [r for r in rows if r["equipment_index"] == 0]
    assert len(conveyor) == 12
    assert all(r["kind"] == "unplanned" and r["fault_index"] == 0 for r in conveyor)
    crusher = [r for r in rows if r["equipment_index"] == 1]
    for failure in [r for r in crusher if r["kind"] == "unplanned"]:
        assert any(
            p["kind"] == "planned"
            and timedelta(0) < failure["issued_at"] - p["issued_at"] < timedelta(days=14)
            for p in crusher
        )
    pump = [r for r in rows if r["equipment_index"] == 2]
    assert Counter(r["quantity"] for r in pump) == {4: 6, 20: 6}
    mill = [r for r in rows if r["equipment_index"] == 3]
    assert sum(r["issued_at"] >= now - timedelta(days=30) for r in mill) == 16
    assert (
        sum(now - timedelta(days=60) <= r["issued_at"] < now - timedelta(days=30) for r in mill)
        == 4
    )


def test_seed_rejects_ambiguous_naive_reference_time():
    with pytest.raises(ValueError, match="timezone"):
        history_specs(datetime(2026, 10, 14))


def test_seed_refuses_existing_catalog_before_writes(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    from scripts import seed

    pool = AsyncMock()
    pool.fetchval.side_effect = [0, True]
    close = AsyncMock()
    monkeypatch.setattr(seed, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(seed, "close_pool", close)
    with pytest.raises(RuntimeError, match="Existing accounts or catalogs"):
        asyncio.run(seed.main())
    pool.execute.assert_not_awaited()
    pool.fetchrow.assert_not_awaited()
    close.assert_awaited_once()

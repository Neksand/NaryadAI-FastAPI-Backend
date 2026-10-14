"""Rule-boundary, deduplication, and localization tests for insight generation."""

import asyncio
import json

import pytest

from app.services.insights import generate_insights


class InsightsConn:
    def __init__(self, result_sets=None, *, existing=False):
        self.result_sets = list(result_sets or [[] for _ in range(7)])
        self.existing = existing
        self.fetch_calls = 0
        self.insertions = []

    async def fetch(self, *_):
        result = self.result_sets[self.fetch_calls]
        self.fetch_calls += 1
        return result

    async def fetchval(self, *_):
        return self.existing

    async def execute(self, query, *args):
        self.insertions.append((query, args))


def positive_rows():
    rows = [[] for _ in range(7)]
    rows[0] = [
        {
            "equipment_id": "eq-1",
            "eq_name": "Насос",
            "eq_name_kk": "Сорғы",
            "area_name": "Цех",
            "fault_code_id": "fault-1",
            "code": "F01",
            "n": 4,
            "total": 8,
        }
    ]
    rows[1] = [
        {"id": "wo-1", "equipment_id": "eq-2", "eq_name": "Компрессор", "eq_name_kk": None},
        {"id": "wo-2", "equipment_id": "eq-2", "eq_name": "Компрессор", "eq_name_kk": None},
    ]
    rows[2] = [
        {
            "name": "Фильтр",
            "mat_name_kk": None,
            "qty": 5.0,
            "typ": 2.0,
            "eq_name": "Насос",
            "eq_name_kk": "Сорғы",
            "equipment_id": "eq-1",
        }
    ]
    rows[3] = [{"shift": "night", "n": 6, "total": 10}]
    rows[4] = [
        {"equipment_id": "eq-3", "eq_name": "Линия", "eq_name_kk": "Желі", "cur": 8, "prev": 4}
    ]
    rows[5] = [{"equipment_id": "eq-4", "eq_name": "Турбина", "eq_name_kk": None, "hours": 4.0}]
    rows[6] = [
        {
            "assignee_id": "worker-1",
            "full_name": "Иван",
            "cur_min": 30,
            "base_min": 10,
            "sd": 5,
            "n": 6,
        }
    ]
    return rows


def test_all_seven_detectors_emit_actionable_findings_and_localized_evidence():
    conn = InsightsConn(positive_rows())

    created = asyncio.run(generate_insights(conn))

    expected_types = [
        "repeat_failure",
        "post_ppr_failure",
        "material_anomaly",
        "shift_correlation",
        "failure_forecast",
        "material_anomaly",
        "executor_pattern",
    ]
    assert [args[0] for _, args in conn.insertions] == expected_types
    assert len(created) == len(expected_types)
    assert len(created) == len(set(created))
    assert conn.fetch_calls == 7

    repeat = conn.insertions[0][1]
    assert repeat[4].startswith("Сорғы")
    assert json.loads(repeat[5])[0]["metric"] == "repeat_count"
    # Repeated post-PPR cases for the same asset collapse to one finding.
    assert sum("после ППР" in headline for headline in created) == 1
    growth = next(args for _, args in conn.insertions if args[0] == "failure_forecast")
    assert json.loads(growth[5]) == [{"metric": "growth", "value": 8, "baseline": 4}]


@pytest.mark.parametrize(
    ("index", "row"),
    [
        (
            0,
            {
                "equipment_id": "eq",
                "eq_name": "E",
                "eq_name_kk": None,
                "fault_code_id": "f",
                "code": "F",
                "n": 4,
                "total": 9,
            },
        ),
        (3, {"shift": "day", "n": 6, "total": 11}),
        (4, {"equipment_id": "eq", "eq_name": "E", "eq_name_kk": None, "cur": 8, "prev": 5}),
        (5, {"equipment_id": "eq", "eq_name": "E", "eq_name_kk": None, "hours": 3.99}),
        (
            6,
            {
                "assignee_id": "worker",
                "full_name": "A",
                "cur_min": 11,
                "base_min": 10,
                "sd": 5,
                "n": 5,
            },
        ),
    ],
)
def test_detector_thresholds_do_not_emit_false_positive_insights(index, row):
    result_sets = [[] for _ in range(7)]
    result_sets[index] = [row]
    conn = InsightsConn(result_sets)

    created = asyncio.run(generate_insights(conn))

    assert created == []
    assert conn.insertions == []


def test_existing_headline_is_not_inserted_twice():
    conn = InsightsConn(positive_rows(), existing=True)

    created = asyncio.run(generate_insights(conn))

    assert created == []
    assert conn.insertions == []
    assert conn.fetch_calls == 7


def test_executor_z_score_uses_a_floor_when_baseline_deviation_is_zero():
    rows = [[] for _ in range(7)]
    rows[6] = [
        {
            "assignee_id": "worker-2",
            "full_name": "Алия",
            "cur_min": 40,
            "base_min": 10,
            "sd": 0,
            "n": 4,
        }
    ]
    conn = InsightsConn(rows)

    created = asyncio.run(generate_insights(conn))

    assert len(created) == 1
    assert conn.insertions[0][1][0] == "executor_pattern"
    evidence = json.loads(conn.insertions[0][1][5])
    assert evidence[0]["value"] == 15.0

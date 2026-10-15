"""Rule-based anomaly detection writing to `insights` (no external calls).

Detectors (30-day window, Russian headlines + recommendations):
  repeat_failure   — one fault code dominates one equipment (>=4, >=50% share)
  post_ppr_failure — unplanned job within 14 days after completed planned maintenance, same equipment
  material_anomaly — writeoff quantity > 2x typical_usage_per_order
  shift_correlation — breakdowns skew to one shift (chi-ish: share >=60%, n>=6)
  unplanned_growth  — unplanned count last 30d >= 2x previous 30d per equipment
  downtime_top      — equipment with largest total stoppage
Run on demand (POST /analytics/insights/generate) and every 6h in background.

Every finding also stores evidence links: `order_ids` (uuids of the work orders
that entered the sample), `denominator` (sample size) and `formula` (an honest
description of the rule that fired — never a calibrated failure probability).
"""

import json
from typing import Any


def _sample_ids(row: Any, key: str = "ids") -> list[str]:
    """Collectors tolerate rows without the key (unit-test doubles)."""
    try:
        vals = row[key]
    except (KeyError, IndexError, TypeError):
        return []
    return [str(v) for v in (vals or [])]


def _single_id(row: Any, key: str = "order_id") -> str | None:
    try:
        val = row[key]
    except (KeyError, IndexError, TypeError):
        return None
    return str(val) if val is not None else None


async def generate_insights(conn: Any) -> list[str]:
    created: list[str] = []

    async def add(
        typ: str,
        sev: str,
        scope: dict[str, Any],
        headline: str,
        evidence: Any,
        rec: str,
        conf: float,
        headline_kk: str | None = None,
        recommendation_kk: str | None = None,
        order_ids: list[str] | None = None,
        denominator: int | None = None,
        formula: str | None = None,
    ) -> None:
        exists = await conn.fetchval("SELECT 1 FROM insights WHERE headline=$1", headline)
        if exists:
            return
        await conn.execute(
            """INSERT INTO insights(type,severity,scope,period_from,period_to,headline,headline_kk,evidence,recommendation,recommendation_kk,confidence,order_ids,denominator,formula)
               VALUES ($1,$2,$3::jsonb,current_date-30,current_date,$4,$5,$6::jsonb,$7,$8,$9,$10::uuid[],$11,$12)""",
            typ,
            sev,
            json.dumps(scope),
            headline,
            headline_kk,
            json.dumps(evidence),
            rec,
            recommendation_kk,
            conf,
            order_ids or [],
            denominator,
            formula,
        )
        created.append(headline)

    # 1. repeat_failure
    rows = await conn.fetch(
        """SELECT w.equipment_id, e.name AS eq_name, e.name_kk AS eq_name_kk, a.name AS area_name, w.fault_code_id, f.code,
                  count(*)::int AS n,
                  (SELECT count(*) FROM work_orders x WHERE x.equipment_id=w.equipment_id
                    AND x.issued_at >= now()-interval '30 days')::int AS total,
                  array_agg(w.id) AS ids
           FROM work_orders w JOIN equipment e ON e.id=w.equipment_id
           JOIN areas a ON a.id=w.area_id JOIN fault_codes f ON f.id=w.fault_code_id
           WHERE w.issued_at >= now()-interval '30 days' AND w.kind='unplanned'
           GROUP BY w.equipment_id, e.name, e.name_kk, a.name, w.fault_code_id, f.code
           HAVING count(*) >= 4"""
    )
    for r in rows:
        if r["total"] and r["n"] / r["total"] >= 0.5:
            eq, eq_kk = r["eq_name"], r["eq_name_kk"] or r["eq_name"]
            ids = _sample_ids(r)
            await add(
                "repeat_failure",
                "critical",
                {"equipment_id": str(r["equipment_id"])},
                f"{eq}: повторные отказы по шифру {r['code']} ({r['n']} за 30 дней)",
                [{"metric": "repeat_count", "value": r["n"], "baseline": 2}],
                f"Ремонт не устраняет причину. Рекомендуем внеплановую диагностику {eq} и включение в план ППР.",
                0.85,
                f"{eq_kk}: {r['code']} шифры бойынша қайта ақаулар ({r['n']}, 30 күнде)",
                f"Жөндеу себебін жоймайды. {eq_kk} кезектен тыс диагностикасын және ЖЖЖ жоспарына енгізуді ұсынамыз.",
                order_ids=ids,
                denominator=r["total"],
                formula="share = repeat_count / equipment_orders_30d; rule: repeat_count >= 4 AND share >= 0.50",
            )

    # 2. post_ppr_failure
    rows = await conn.fetch(
        """SELECT u.id, u.equipment_id, e.name AS eq_name, e.name_kk AS eq_name_kk FROM work_orders u
           JOIN equipment e ON e.id=u.equipment_id
           WHERE u.kind='unplanned' AND u.issued_at >= now()-interval '30 days'
             AND EXISTS (SELECT 1 FROM work_orders p WHERE p.equipment_id=u.equipment_id
                         AND p.kind='planned' AND p.status='closed' AND p.done_at < u.issued_at
                         AND p.done_at >= u.issued_at - interval '14 days')
           LIMIT 20"""
    )
    by_equipment: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = str(r["equipment_id"])
        slot = by_equipment.setdefault(key, {"row": r, "ids": []})
        slot["ids"].append(str(r["id"]))
    for key, slot in by_equipment.items():
        r = slot["row"]
        ids = slot["ids"]
        eq, eq_kk = r["eq_name"], r["eq_name_kk"] or r["eq_name"]
        await add(
            "post_ppr_failure",
            "warning",
            {"equipment_id": key},
            f"{eq}: внеплановые работы вскоре после ППР",
            [{"metric": "post_ppr_case", "order_id": ids[0]}],
            "Проверьте качество планового ремонта и назначьте контрольный осмотр через 10 дней.",
            0.74,
            f"{eq_kk}: ЖЖЖ-дан кейін көп ұзамай жоспардан тыс жұмыстар",
            "Жоспарлы жөндеу сапасын тексеріп, 10 күннен кейін бақылау тексеруін тағайындаңыз.",
            order_ids=ids,
            denominator=len(ids),
            formula="unplanned.issued_at - planned.done_at <= 14 days AND planned.status = 'closed' (same equipment, 30d window)",
        )

    # 3. material_anomaly
    rows = await conn.fetch(
        """SELECT m.name, m.name_kk AS mat_name_kk, mw.quantity::float AS qty, m.typical_usage_per_order::float AS typ,
                  e.name AS eq_name, e.name_kk AS eq_name_kk, w.equipment_id, w.id AS order_id
           FROM material_writeoffs mw JOIN materials m ON m.id=mw.material_id
           JOIN work_orders w ON w.id=mw.work_order_id JOIN equipment e ON e.id=w.equipment_id
           WHERE m.typical_usage_per_order IS NOT NULL AND mw.quantity > 2*m.typical_usage_per_order
             AND mw.created_at >= now()-interval '30 days' LIMIT 20"""
    )
    for r in rows:
        eq, eq_kk = r["eq_name"], r["eq_name_kk"] or r["eq_name"]
        mat, mat_kk = r["name"], r["mat_name_kk"] or r["name"]
        oid = _single_id(r)
        await add(
            "material_anomaly",
            "warning",
            {"equipment_id": str(r["equipment_id"])},
            f"{eq}: перерасход «{mat}» ({r['qty']} против нормы {r['typ']})",
            [{"metric": "overuse_ratio", "value": round(r["qty"] / r["typ"], 2)}],
            "Проверьте утечки/нормы списания и обоснуйте расход в комментарии наряда.",
            0.78,
            f"{eq_kk}: «{mat_kk}» артық шығыны ({r['qty']}, норма {r['typ']})",
            "Ағуларды/есептен шығару нормаларын тексеріп, шығынды наряд пікірінде негіздеңіз.",
            order_ids=[oid] if oid else [],
            denominator=1,
            formula="ratio = writeoff_qty / typical_usage_per_order; rule: ratio > 2.0 (single write-off)",
        )

    # 4. shift_correlation
    rows = await conn.fetch(
        """SELECT COALESCE(assignee.shift,'?') AS shift, count(*)::int AS n,
                  (SELECT count(*)::int FROM work_orders WHERE kind='unplanned' AND issued_at >= now()-interval '30 days') AS total,
                  array_agg(w.id) AS ids
           FROM work_orders w LEFT JOIN employees assignee ON assignee.id=w.assignee_id
           WHERE w.kind='unplanned' AND w.issued_at >= now()-interval '30 days'
           GROUP BY assignee.shift HAVING count(*) >= 6"""
    )
    for r in rows:
        if r["total"] and r["n"] / r["total"] >= 0.6:
            ids = _sample_ids(r)
            await add(
                "shift_correlation",
                "info",
                {"shift": r["shift"]},
                f"Поломки тяготеют к смене «{r['shift']}» ({r['n']} из {r['total']} за 30 дней)",
                [{"metric": "shift_share", "value": round(r["n"] / r["total"], 2)}],
                "Проверьте загрузку и состав этой смены, сравните с регламентом.",
                0.58,
                f"Ақаулар «{r['shift']}» ауысымына шоғырланған ({r['total']}-дан {r['n']}, 30 күнде)",
                "Бұл ауысымның жүктемесін мен құрамын тексеріп, регламентпен салыстырыңыз.",
                order_ids=ids,
                denominator=r["total"],
                formula="share = shift_unplanned / unplanned_30d; rule: shift_unplanned >= 6 AND share >= 0.60",
            )

    # 5. unplanned_growth
    rows = await conn.fetch(
        """SELECT w.equipment_id, e.name AS eq_name, e.name_kk AS eq_name_kk,
                  count(*) FILTER (WHERE w.issued_at >= now()-interval '30 days')::int AS cur,
                  count(*) FILTER (WHERE w.issued_at >= now()-interval '60 days' AND w.issued_at < now()-interval '30 days')::int AS prev,
                  array_agg(w.id) FILTER (WHERE w.issued_at >= now()-interval '30 days') AS ids
           FROM work_orders w JOIN equipment e ON e.id=w.equipment_id
           WHERE w.kind='unplanned' AND w.issued_at >= now()-interval '60 days'
           GROUP BY w.equipment_id, e.name, e.name_kk HAVING count(*) FILTER (WHERE w.issued_at >= now()-interval '30 days') >= 4"""
    )
    for r in rows:
        if r["prev"] and r["cur"] >= 2 * r["prev"]:
            eq, eq_kk = r["eq_name"], r["eq_name_kk"] or r["eq_name"]
            ids = _sample_ids(r)
            await add(
                "failure_forecast",
                "warning",
                {"equipment_id": str(r["equipment_id"])},
                f"{eq}: рост внеплановых {r['prev']} → {r['cur']} за месяц",
                [{"metric": "growth", "value": r["cur"], "baseline": r["prev"]}],
                "Возможен отказ в ближайшие 2 недели — запланируйте диагностику.",
                0.66,
                f"{eq_kk}: жоспардан тыс жұмыстардың өсуі {r['prev']} → {r['cur']} (айда)",
                "Алдағы 2 аптада ақау болуы мүмкін — диагностика жоспарлаңыз.",
                order_ids=ids,
                denominator=r["cur"] + r["prev"],
                formula="growth = cur_30d / prev_30d; rule: cur_30d >= 4 AND cur_30d >= 2 * prev_30d",
            )

    # 6. downtime_top
    rows = await conn.fetch(
        """SELECT w.equipment_id, e.name AS eq_name, e.name_kk AS eq_name_kk,
                  sum(EXTRACT(EPOCH FROM (COALESCE(w.equipment_restored_at, now()) - w.equipment_stopped_at))/3600)::float AS hours,
                  count(*)::int AS n, array_agg(w.id) AS ids
           FROM work_orders w JOIN equipment e ON e.id=w.equipment_id
           WHERE w.equipment_stopped_at IS NOT NULL AND w.issued_at >= now()-interval '30 days'
           GROUP BY w.equipment_id, e.name, e.name_kk ORDER BY hours DESC LIMIT 3"""
    )
    for r in rows:
        if (r["hours"] or 0) >= 4:
            eq, eq_kk = r["eq_name"], r["eq_name_kk"] or r["eq_name"]
            ids = _sample_ids(r)
            await add(
                "material_anomaly",
                "info",
                {"equipment_id": str(r["equipment_id"])},
                f"{eq}: простой {round(r['hours'], 1)} ч за 30 дней — входит в тройку по длительности",
                [{"metric": "downtime_hours", "value": round(r["hours"], 1)}],
                "Разберите причины простоев по шифрам и включите узел в план ППР.",
                0.6,
                f"{eq_kk}: 30 күнде тоқтау {round(r['hours'], 1)} сағ — ұзақтығы бойынша алғашқы үштікте",
                "Шифрлар бойынша тоқтау себептерін талдап, торапты ЖЖЖ жоспарына енгізіңіз.",
                order_ids=ids,
                denominator=(dict(r).get("n") or len(ids) or 1),
                formula="downtime_hours = sum(restored_at - stopped_at) per equipment over 30d; rule: top-3 AND hours >= 4",
            )
    # 7. executor_pattern via z-score on durations (baseline = previous 60-30d)
    rows = await conn.fetch(
        """WITH cur AS (
             SELECT assignee_id, AVG(EXTRACT(EPOCH FROM (done_at-started_at))/60)::float AS avg_min, count(*)::int AS n,
                    array_agg(id) AS ids
             FROM work_orders WHERE assignee_id IS NOT NULL AND done_at IS NOT NULL AND started_at IS NOT NULL
               AND issued_at >= now()-interval '30 days' GROUP BY assignee_id HAVING count(*) >= 4),
           base AS (
             SELECT assignee_id, AVG(EXTRACT(EPOCH FROM (done_at-started_at))/60)::float AS avg_min,
                    COALESCE(stddev_samp(EXTRACT(EPOCH FROM (done_at-started_at))/60), 0)::float AS sd
             FROM work_orders WHERE assignee_id IS NOT NULL AND done_at IS NOT NULL AND started_at IS NOT NULL
               AND issued_at >= now()-interval '90 days' AND issued_at < now()-interval '30 days'
             GROUP BY assignee_id)
           SELECT c.assignee_id, e.full_name, c.avg_min AS cur_min, b.avg_min AS base_min, b.sd, c.n, c.ids
           FROM cur c JOIN base b ON b.assignee_id=c.assignee_id JOIN employees e ON e.id=c.assignee_id"""
    )
    for r in rows:
        denom = r["sd"] if (r["sd"] or 0) > 1e-6 else max((r["base_min"] or 1) * 0.2, 1.0)
        z = ((r["cur_min"] or 0) - (r["base_min"] or 0)) / denom
        if z >= 2.0:
            pct = round(((r["cur_min"] or 0) / (r["base_min"] or 1) - 1) * 100)
            ids = _sample_ids(r)
            await add(
                "executor_pattern",
                "warning",
                {"employee_id": str(r["assignee_id"])},
                f"{r['full_name']}: среднее время {round(r['cur_min'])} мин против базовых {round(r['base_min'] or 0)} (+{pct}%, z={round(z, 1)})",
                [
                    {
                        "metric": "duration_z",
                        "value": round(z, 2),
                        "baseline": round(r["base_min"] or 0, 1),
                    }
                ],
                "Разберите причины slowdown: очередь, оборудование или пропуск шагов.",
                0.62,
                f"{r['full_name']}: орташа уақыт {round(r['cur_min'])} мин, базалық {round(r['base_min'] or 0)} (+{pct}%, z={round(z, 1)})",
                "Баяулау себептерін талдаңыз: кезек, жабдық немесе өткізілген қадамдар.",
                order_ids=ids,
                denominator=r["n"],
                formula="z = (cur_avg_min - base_avg_min) / sd_floor; rule: z >= 2.0 AND n >= 4; sd_floor = sd if sd > 0 else max(base*0.2, 1)",
            )
    return created

"""Rule-based anomaly detection writing to `insights` (no external calls).

Detectors (30-day window, Russian headlines + recommendations):
  repeat_failure   — one fault code dominates one equipment (>=4, >=50% share)
  post_ppr_failure — unplanned job within 14 days after a planned one, same equipment
  material_anomaly — writeoff quantity > 2x typical_usage_per_order
  shift_correlation — breakdowns skew to one shift (chi-ish: share >=60%, n>=6)
  unplanned_growth  — unplanned count last 30d >= 2x previous 30d per equipment
  downtime_top      — equipment with largest total stoppage
Run on demand (POST /analytics/insights/generate) and every 6h in background.
"""
import json
from datetime import date, timedelta


async def generate_insights(conn) -> list[str]:
    created: list[str] = []

    async def add(typ, sev, scope, headline, evidence, rec, conf):
        exists = await conn.fetchval("SELECT 1 FROM insights WHERE headline=$1", headline)
        if exists:
            return
        await conn.execute(
            """INSERT INTO insights(type,severity,scope,period_from,period_to,headline,evidence,recommendation,confidence)
               VALUES ($1,$2,$3::jsonb,current_date-30,current_date,$4,$5::jsonb,$6,$7)""",
            typ, sev, json.dumps(scope), headline, json.dumps(evidence), rec, conf)
        created.append(headline)

    # 1. repeat_failure
    rows = await conn.fetch(
        """SELECT w.equipment_id, e.name AS eq_name, a.name AS area_name, w.fault_code_id, f.code,
                  count(*)::int AS n,
                  (SELECT count(*) FROM work_orders x WHERE x.equipment_id=w.equipment_id
                    AND x.issued_at >= now()-interval '30 days')::int AS total
           FROM work_orders w JOIN equipment e ON e.id=w.equipment_id
           JOIN areas a ON a.id=w.area_id JOIN fault_codes f ON f.id=w.fault_code_id
           WHERE w.issued_at >= now()-interval '30 days' AND w.kind='unplanned'
           GROUP BY w.equipment_id, e.name, a.name, w.fault_code_id, f.code
           HAVING count(*) >= 4""")
    for r in rows:
        if r["total"] and r["n"] / r["total"] >= 0.5:
            await add("repeat_failure", "critical",
                      {"equipment_id": str(r["equipment_id"])},
                      f"{r['eq_name']}: повторные отказы по шифру {r['code']} ({r['n']} за 30 дней)",
                      [{"metric": "repeat_count", "value": r["n"], "baseline": 2}],
                      f"Ремонт не устраняет причину. Рекомендуем внеплановую диагностику {r['eq_name']} и включение в план ППР.", 0.85)

    # 2. post_ppr_failure
    rows = await conn.fetch(
        """SELECT u.id, u.equipment_id, e.name AS eq_name FROM work_orders u
           JOIN equipment e ON e.id=u.equipment_id
           WHERE u.kind='unplanned' AND u.issued_at >= now()-interval '30 days'
             AND EXISTS (SELECT 1 FROM work_orders p WHERE p.equipment_id=u.equipment_id
                         AND p.kind='planned' AND p.issued_at < u.issued_at
                         AND p.issued_at >= u.issued_at - interval '14 days')
           LIMIT 20""")
    seen = set()
    for r in rows:
        key = str(r["equipment_id"])
        if key in seen:
            continue
        seen.add(key)
        await add("post_ppr_failure", "warning", {"equipment_id": key},
                  f"{r['eq_name']}: внеплановые работы вскоре после ППР",
                  [{"metric": "post_ppr_case", "order_id": str(r["id"])}],
                  "Проверьте качество планового ремонта и назначьте контрольный осмотр через 10 дней.", 0.74)

    # 3. material_anomaly
    rows = await conn.fetch(
        """SELECT m.name, mw.quantity::float AS qty, m.typical_usage_per_order::float AS typ,
                  e.name AS eq_name, w.equipment_id
           FROM material_writeoffs mw JOIN materials m ON m.id=mw.material_id
           JOIN work_orders w ON w.id=mw.work_order_id JOIN equipment e ON e.id=w.equipment_id
           WHERE m.typical_usage_per_order IS NOT NULL AND mw.quantity > 2*m.typical_usage_per_order
             AND mw.created_at >= now()-interval '30 days' LIMIT 20""")
    for r in rows:
        await add("material_anomaly", "warning", {"equipment_id": str(r["equipment_id"])},
                  f"{r['eq_name']}: перерасход «{r['name']}» ({r['qty']} против нормы {r['typ']})",
                  [{"metric": "overuse_ratio", "value": round(r['qty'] / r['typ'], 2)}],
                  "Проверьте утечки/нормы списания и обоснуйте расход в комментарии наряда.", 0.78)

    # 4. shift_correlation
    rows = await conn.fetch(
        """SELECT COALESCE(assignee.shift,'?') AS shift, count(*)::int AS n,
                  (SELECT count(*)::int FROM work_orders WHERE kind='unplanned' AND issued_at >= now()-interval '30 days') AS total
           FROM work_orders w LEFT JOIN employees assignee ON assignee.id=w.assignee_id
           WHERE w.kind='unplanned' AND w.issued_at >= now()-interval '30 days'
           GROUP BY assignee.shift HAVING count(*) >= 6""")
    for r in rows:
        if r["total"] and r["n"] / r["total"] >= 0.6:
            await add("shift_correlation", "info", {"shift": r["shift"]},
                      f"Поломки тяготеют к смене «{r['shift']}» ({r['n']} из {r['total']} за 30 дней)",
                      [{"metric": "shift_share", "value": round(r['n'] / r['total'], 2)}],
                      "Проверьте загрузку и состав этой смены, сравните с регламентом.", 0.58)

    # 5. unplanned_growth
    rows = await conn.fetch(
        """SELECT w.equipment_id, e.name AS eq_name,
                  count(*) FILTER (WHERE w.issued_at >= now()-interval '30 days')::int AS cur,
                  count(*) FILTER (WHERE w.issued_at >= now()-interval '60 days' AND w.issued_at < now()-interval '30 days')::int AS prev
           FROM work_orders w JOIN equipment e ON e.id=w.equipment_id
           WHERE w.kind='unplanned' AND w.issued_at >= now()-interval '60 days'
           GROUP BY w.equipment_id, e.name HAVING count(*) FILTER (WHERE w.issued_at >= now()-interval '30 days') >= 4""")
    for r in rows:
        if r["prev"] and r["cur"] >= 2 * r["prev"]:
            await add("failure_forecast", "warning", {"equipment_id": str(r["equipment_id"])},
                      f"{r['eq_name']}: рост внеплановых {r['prev']} → {r['cur']} за месяц",
                      [{"metric": "growth", "value": r["cur"], "baseline": r["prev"]}],
                      "Возможен отказ в ближайшие 2 недели — запланируйте диагностику.", 0.66)

    # 6. downtime_top
    rows = await conn.fetch(
        """SELECT w.equipment_id, e.name AS eq_name,
                  sum(EXTRACT(EPOCH FROM (COALESCE(w.equipment_restored_at, now()) - w.equipment_stopped_at))/3600)::float AS hours
           FROM work_orders w JOIN equipment e ON e.id=w.equipment_id
           WHERE w.equipment_stopped_at IS NOT NULL AND w.issued_at >= now()-interval '30 days'
           GROUP BY w.equipment_id, e.name ORDER BY hours DESC LIMIT 3""")
    for r in rows:
        if (r["hours"] or 0) >= 4:
            await add("material_anomaly", "info", {"equipment_id": str(r["equipment_id"])},
                      f"{r['eq_name']}: простой {round(r['hours'], 1)} ч за 30 дней — максимум по парку",
                      [{"metric": "downtime_hours", "value": round(r['hours'], 1)}],
                      "Разберите причины простоев по шифрам и включите узел в план ППР.", 0.6)
    # 7. executor_pattern via z-score on durations (baseline = previous 60-30d)
    rows = await conn.fetch(
        """WITH cur AS (
             SELECT assignee_id, AVG(EXTRACT(EPOCH FROM (done_at-started_at))/60)::float AS avg_min, count(*)::int AS n
             FROM work_orders WHERE assignee_id IS NOT NULL AND done_at IS NOT NULL AND started_at IS NOT NULL
               AND issued_at >= now()-interval '30 days' GROUP BY assignee_id HAVING count(*) >= 4),
           base AS (
             SELECT assignee_id, AVG(EXTRACT(EPOCH FROM (done_at-started_at))/60)::float AS avg_min,
                    COALESCE(stddev_samp(EXTRACT(EPOCH FROM (done_at-started_at))/60), 0)::float AS sd
             FROM work_orders WHERE assignee_id IS NOT NULL AND done_at IS NOT NULL AND started_at IS NOT NULL
               AND issued_at >= now()-interval '90 days' AND issued_at < now()-interval '30 days'
             GROUP BY assignee_id)
           SELECT c.assignee_id, e.full_name, c.avg_min AS cur_min, b.avg_min AS base_min, b.sd, c.n
           FROM cur c JOIN base b ON b.assignee_id=c.assignee_id JOIN employees e ON e.id=c.assignee_id""")
    for r in rows:
        denom = r["sd"] if (r["sd"] or 0) > 1e-6 else max((r["base_min"] or 1) * 0.2, 1.0)
        z = ((r["cur_min"] or 0) - (r["base_min"] or 0)) / denom
        if z >= 2.0:
            pct = round(((r["cur_min"] or 0) / (r["base_min"] or 1) - 1) * 100)
            await add("executor_pattern", "warning", {"employee_id": str(r["assignee_id"])},
                      f"{r['full_name']}: среднее время {round(r['cur_min'])} мин против базовых {round(r['base_min'] or 0)} (+{pct}%, z={round(z, 1)})",
                      [{"metric": "duration_z", "value": round(z, 2), "baseline": round(r["base_min"] or 0, 1)}],
                      "Разберите причины slowdown: очередь, оборудование или пропуск шагов.", 0.62)
    return created

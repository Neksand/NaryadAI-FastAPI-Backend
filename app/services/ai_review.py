"""rules-v1: локальная проверка закрытого наряда, порт src/jobs/ai-review.ts."""
import hashlib
import json


async def run_ai_review(conn, work_order_id: str) -> dict:
    order = await conn.fetchrow("SELECT * FROM work_orders WHERE id=$1::uuid FOR UPDATE", work_order_id)
    if not order or order["status"] != "ai_review":
        return {"skipped": True}
    photos = await conn.fetch("SELECT sha256, kind FROM photos WHERE work_order_id=$1::uuid", work_order_id)
    after = [p for p in photos if p["kind"] == "after"]
    dup = None
    if after:
        for p in after:
            other = await conn.fetchval(
                "SELECT work_order_id FROM photos WHERE sha256=$1 AND work_order_id IS NOT NULL AND work_order_id <> $2::uuid LIMIT 1",
                p["sha256"], work_order_id)
            if other:
                dup = str(other)
                break
    mats = await conn.fetch(
        """SELECT mw.quantity, m.typical_usage_per_order FROM material_writeoffs mw
           JOIN materials m ON m.id=mw.material_id WHERE mw.work_order_id=$1::uuid""", work_order_id)
    missing = []
    if not (order["work_done_text"] or ""):
        missing.append("work_done_text")
    if not order["fault_code_id"]:
        missing.append("fault_code_id")
    if order["kind"] == "unplanned" and not after:
        missing.append("after_photo")
    mat_flag = any(float(m["quantity"]) > 2 * float(m["typical_usage_per_order"] or 0) for m in mats if m["typical_usage_per_order"])
    checks = {
        "completeness": {"pass": not missing, "confidence": 0.7},
        "materials": {"pass": not mat_flag, "confidence": 0.65},
        "photos": {"pass": bool(after) and not dup, "score": 0 if not after else (1 if dup else 4), "confidence": 0.72},
    }
    confidence = 0.68 if after else 0.52
    if missing or dup:
        verdict, score = "needs_rework", max(0, 100 - 30 * len(missing) - (40 if dup else 0))
    elif confidence < 0.6:
        verdict, score = "needs_master_review", None
    elif mat_flag:
        verdict, score = "accepted_with_remarks", 82
    else:
        verdict, score = "accepted", 90
    attempt = await conn.fetchval("SELECT COALESCE(max(attempt),0)+1 FROM ai_reviews WHERE work_order_id=$1::uuid", work_order_id)
    input_hash = hashlib.sha256(json.dumps(
        {"id": work_order_id, "work": order["work_done_text"], "fault": str(order["fault_code_id"]),
         "photos": sorted(p["sha256"] for p in after)}, sort_keys=True).encode()).hexdigest()
    await conn.execute(
        """INSERT INTO ai_reviews(work_order_id, attempt, verdict, score, confidence, checks, explanation, model, input_hash)
           VALUES ($1::uuid,$2,$3,$4,$5,$6::jsonb,'Проверка rules-v1: локальные эвристики, без внешней модели.','rules-v1',$7)""",
        work_order_id, int(attempt), verdict, score, confidence, json.dumps(checks), input_hash)
    if verdict == "needs_rework":
        await conn.execute("UPDATE work_orders SET status='rework', return_count=return_count+1, updated_at=now() WHERE id=$1::uuid", work_order_id)
        await conn.execute("INSERT INTO work_order_events(work_order_id, actor_kind, action, payload) VALUES ($1::uuid,'ai','returned',$2::jsonb)",
                           work_order_id, json.dumps({"verdict": verdict}))
    await conn.execute("INSERT INTO work_order_events(work_order_id, actor_kind, action, payload) VALUES ($1::uuid,'ai','ai_review_done',$2::jsonb)",
                       work_order_id, json.dumps({"verdict": verdict, "score": score}))
    await conn.execute("INSERT INTO outbox_events(event, channels, payload) VALUES ('order.ai_review_ready',$1,$2::jsonb)",
                       [f"order:{work_order_id}"], json.dumps({"id": work_order_id, "verdict": verdict}))
    return {"verdict": verdict, "score": score, "attempt": int(attempt)}

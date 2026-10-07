"""rules-v1: local checks + optional external AI (vision/text) with fallback.

Without API keys everything below runs locally:
  sha256 exact duplicates, phash near-duplicates (hamming <= 6),
  EXIF freshness, keyword text relevance, material/timing rules.
With GEMINI_API_KEY: before/after vision comparison (score 1-5).
With any text key: LLM relevance problem-vs-work (else keyword heuristic).
"""
import hashlib
import json
from datetime import datetime, timezone


def _hamming(a: str, b: str) -> int:
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except (ValueError, TypeError):
        return 64


def _keyword_relevance(problem: str, work: str, fault_name: str) -> tuple[bool, float]:
    import re
    pw = set(re.findall(r"[\w\-]{3,}", (problem or "").lower()))
    ww = set(re.findall(r"[\w\-]{3,}", (work or "").lower()))
    if not pw or not ww:
        return False, 0.4
    overlap = len(pw & ww) + sum(1 for w in ww if fault_name and w in fault_name.lower())
    return overlap > 0, min(0.9, 0.45 + 0.12 * overlap)


async def _llm_relevance(problem: str, work: str, fault_code: str) -> tuple[bool | None, float | None, str]:
    from app.ai_providers import chat_text

    out, provider = await chat_text(
        "Ты контролёр ремонтов. Ответь строго JSON без персональных данных: "
        '{"match": true|false, "confidence": 0.0-1.0}.',
        f"Проблема: {problem}\nВыполненные работы: {work}\nШифр: {fault_code}\n"
        "Соответствуют ли работы проблеме и шифру?",
    )
    if not out:
        return None, None, provider
    try:
        start, end = out.index("{"), out.rindex("}") + 1
        data = json.loads(out[start:end])
        return bool(data["match"]), float(data["confidence"]), provider
    except (ValueError, KeyError, TypeError):
        return None, None, provider


async def run_ai_review(conn, work_order_id: str) -> dict:
    from app.ai_providers import compare_photos

    order = await conn.fetchrow("SELECT * FROM work_orders WHERE id=$1::uuid FOR UPDATE", work_order_id)
    if not order or order["status"] != "ai_review":
        return {"skipped": True}
    photos = await conn.fetch("SELECT sha256, phash, kind, exif_taken_at, object_key FROM photos WHERE work_order_id=$1::uuid", work_order_id)
    before = [p for p in photos if p["kind"] == "before"]
    after = [p for p in photos if p["kind"] == "after"]

    # exact + near duplicates against other orders
    dup = None
    if after:
        for p in after:
            other = await conn.fetchval(
                "SELECT work_order_id FROM photos WHERE sha256=$1 AND work_order_id IS NOT NULL AND work_order_id <> $2::uuid LIMIT 1",
                p["sha256"], work_order_id)
            if other:
                dup = str(other)
                break
            if p["phash"]:
                others = await conn.fetch(
                    "SELECT work_order_id, phash FROM photos WHERE work_order_id IS NOT NULL AND work_order_id <> $1::uuid AND phash IS NOT NULL LIMIT 200",
                    work_order_id)
                for o in others:
                    if _hamming(p["phash"], o["phash"]) <= 6:
                        dup = str(o["work_order_id"])
                        break
            if dup:
                break

    # EXIF freshness: photo taken within 12h before done_at
    stale_exif = False
    if after and order["done_at"]:
        for p in after:
            if p["exif_taken_at"] and (order["done_at"] - p["exif_taken_at"]).total_seconds() > 12 * 3600:
                stale_exif = True

    # vision hook (before+after + key)
    vision_note, vision_score = None, None
    if before and after:
        from app.storage import get_s3
        from app.config import settings

        try:
            s3 = get_s3()
            b = s3.get_object(Bucket=settings.S3_BUCKET, Key=before[0]["object_key"])["Body"].read()
            a = s3.get_object(Bucket=settings.S3_BUCKET, Key=after[0]["object_key"])["Body"].read()
            text, provider = await compare_photos(b, a, f"шифр={order['fault_code_id']}")
            if text:
                try:
                    s_idx, e_idx = text.index("{"), text.rindex("}") + 1
                    v = json.loads(text[s_idx:e_idx])
                    vision_note, vision_score = str(v.get("note", ""))[:300], int(v.get("score", 3))
                except (ValueError, KeyError, TypeError):
                    vision_note = text[:300]
        except Exception:
            pass

    if vision_score is not None:
        photo_score = max(1, min(5, vision_score))
    elif not after:
        photo_score = 1
    elif dup:
        photo_score = 1
    elif stale_exif:
        photo_score = 2
    else:
        photo_score = 4

    fault = await conn.fetchrow("SELECT code, name FROM fault_codes WHERE id=$1::uuid", order["fault_code_id"]) if order["fault_code_id"] else None
    match, conf, provider = await _llm_relevance(order["description"] or "", order["work_done_text"] or "",
                                                 fault["code"] if fault else "")
    if match is None:
        match, conf = _keyword_relevance(order["description"] or "", order["work_done_text"] or "",
                                        fault["name"] if fault else "")
        provider = "local"

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
        "relevance": {"pass": bool(match), "confidence": round(float(conf or 0), 2), "provider": provider},
        "materials": {"pass": not mat_flag, "confidence": 0.65},
        "photos": {"pass": bool(after) and not dup, "score": photo_score, "confidence": 0.72,
                   "vision": vision_note, "stale_exif": stale_exif},
    }
    confidence = 0.68 if after else 0.52
    if missing or dup:
        verdict, score = "needs_rework", max(0, 100 - 30 * len(missing) - (40 if dup else 0))
    elif confidence < 0.6 or (match is False and (conf or 0) >= 0.7):
        verdict, score = "needs_master_review", None
    elif mat_flag or stale_exif:
        verdict, score = "accepted_with_remarks", 82
    else:
        verdict, score = "accepted", 90

    attempt = await conn.fetchval("SELECT COALESCE(max(attempt),0)+1 FROM ai_reviews WHERE work_order_id=$1::uuid", work_order_id)
    input_hash = hashlib.sha256(json.dumps(
        {"id": work_order_id, "work": order["work_done_text"], "fault": str(order["fault_code_id"]),
         "photos": sorted(p["sha256"] for p in after)}, sort_keys=True).encode()).hexdigest()
    explanation = ("Проверка rules-v1: локальные эвристики" + (", зрение Gemini" if vision_note else "") +
                   (", релевантность LLM" if provider != "local" else "") + ". Без внешней модели персональные данные не покидали контур.")
    if vision_note:
        explanation += f" Фото: {vision_note}"
    await conn.execute(
        """INSERT INTO ai_reviews(work_order_id, attempt, verdict, score, photo_score, confidence, checks, explanation, model, input_hash)
           VALUES ($1::uuid,$2,$3,$4,$5,$6,$7::jsonb,$8,'rules-v1',$9)""",
        work_order_id, int(attempt), verdict, score, photo_score, confidence, json.dumps(checks), explanation, input_hash)
    if verdict == "needs_rework":
        await conn.execute("UPDATE work_orders SET status='rework', return_count=return_count+1, updated_at=now() WHERE id=$1::uuid", work_order_id)
        await conn.execute("INSERT INTO work_order_events(work_order_id, actor_kind, action, payload) VALUES ($1::uuid,'ai','returned',$2::jsonb)",
                           work_order_id, json.dumps({"verdict": verdict}))
    await conn.execute("INSERT INTO work_order_events(work_order_id, actor_kind, action, payload) VALUES ($1::uuid,'ai','ai_review_done',$2::jsonb)",
                       work_order_id, json.dumps({"verdict": verdict, "score": score}))
    await conn.execute("INSERT INTO outbox_events(event, channels, payload) VALUES ('order.ai_review_ready',$1,$2::jsonb)",
                       [f"order:{work_order_id}"], json.dumps({"id": work_order_id, "verdict": verdict}))
    return {"verdict": verdict, "score": score, "attempt": int(attempt)}

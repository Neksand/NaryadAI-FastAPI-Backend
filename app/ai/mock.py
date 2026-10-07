"""Deterministic MockAIProvider (spec §27). Works offline, no keys.

Never presented as a real vendor: results carry provider='mock'.
Supports ru + kk explanations.
"""
import hashlib


def _pick(key: str, options: list) -> int:
    return int(hashlib.sha256(key.encode()).hexdigest(), 16) % len(options)


RU = {
    "pass": "Работы соответствуют наряду: описание, шифр и материалы на месте.",
    "review": "Есть замечания: проверьте материалы и сроки, мастер решит.",
    "fail": "Требуется доработка: неполный объём, мало доказательств или чужое фото.",
}
KK = {
    "pass": "Жұмыстар нарядқа сәйкес: сипаттама, шифр және материалдар орнында.",
    "review": "Ескертулер бар: материалдар мен мерзімдерді тексеріңіз, шешімді шебер қабылдайды.",
    "fail": "Пысықтау қажет: көлем толық емес, дәлел аз немесе бөтен фото.",
}


class MockAIProvider:
    name = "mock"

    async def recommend_worker(self, ctx: dict, language: str = "ru") -> dict:
        cands = ctx.get("candidates", [])
        if not cands:
            expl = {"ru": "Нет свободных исполнителей на участке.", "kk": "Учаскеде бос орындаушы жоқ."}
            return {"recommended_worker_id": None, "confidence": 0.2, "reason": expl.get(language, expl["ru"]), "alternatives": [], "provider": "mock"}
        best = cands[0]
        expl = {"ru": f"Свободен, подходящая загрузка, лучший рейтинг среди {len(cands)} кандидатов.",
                "kk": f"Бос, жүктемесі сай, {len(cands)} үміткер арасында рейтингі жоғары."}
        return {"recommended_worker_id": best.get("employee_id"), "confidence": 0.82,
                "reason": expl.get(language, expl["ru"]),
                "alternatives": [c.get("employee_id") for c in cands[1:3]], "provider": "mock"}

    async def inspect_completion(self, ctx: dict, language: str = "ru") -> dict:
        table = RU if language != "kk" else KK
        if ctx.get("duplicate") or ctx.get("missing"):
            return {"verdict": "FAIL", "score": 42, "confidence": 0.81, "issues": ctx.get("missing", []) + (["duplicate_photo"] if ctx.get("duplicate") else []),
                    "explanation": table["fail"], "recommendation": "REWORK", "provider": "mock"}
        if ctx.get("material_flag") or ctx.get("stale_exif"):
            return {"verdict": "REVIEW", "score": 74, "confidence": 0.66, "issues": ["remarks"],
                    "explanation": table["review"], "recommendation": "MASTER_REVIEW", "provider": "mock"}
        return {"verdict": "PASS", "score": 91, "confidence": 0.88, "issues": [],
                "explanation": table["pass"], "recommendation": "CLOSE", "provider": "mock"}

    async def explain_anomaly(self, ctx: dict, language: str = "ru") -> str:
        if language == "kk":
            return f"{ctx.get('headline', '')}. Ұсыныс: {ctx.get('recommendation', '')}"
        return f"{ctx.get('headline', '')}. Рекомендация: {ctx.get('recommendation', '')}"

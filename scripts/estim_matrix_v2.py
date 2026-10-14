"""Estimin3n research matrix: real local-LLM verdicts on evidence cases.

Loads model ONCE, runs GOOD+BAD (ru) + HOLDOUT (ru+kk), writes /tmp/estim_matrix_v2.json.
Honest protocol: same cases as mock evidence (tests/test_ai_evidence.py),
parsed verdict vs expected_of(). Slow on MPS (~1 min/gen) — run in background.
"""

import json
import sys
import time

sys.path.insert(0, "/Users/neksand/Desktop/NaryadAI-FastAPI-Backend")

from app import ai_providers as P  # noqa: E402
from tests.test_ai_evidence import (  # noqa: E402
    BAD_REPORTS,
    GOOD_REPORTS,
    HOLDOUT_REPORTS,
    expected_of,
)

PROMPT = (
    "Ты строгий контролёр качества ремонта на заводе. Отчёт исполнителя: «{text}» "
    "Шифр неисправности: {code}. Материалы: {mats}. "
    "Критерии FAIL (доработка): отписка из 1-3 слов ('сделано', 'всё ок', 'готово'), "
    "пустое описание работ, работа явно не выполнена, проблема осталась. "
    "Критерии REVIEW (замечания): неполный объём, сомнительные материалы, старое фото. "
    "Иначе PASS. Ответь строго двумя строками. Первая строка: ВЕРДИКТ: PASS или REVIEW или FAIL. "
    "Вторая строка: ПОЯСНЕНИЕ: коротко."
)


def ask(text, code, mats):
    msgs = [{"role": "user", "content": [{"type": "text", "text": PROMPT.format(text=text, code=code, mats=mats)}]}]
    t0 = time.perf_counter()
    out = P._estimin3n_generate(msgs, 128)
    dt = round(time.perf_counter() - t0, 1)
    up = str(out).upper()
    verdict = "PASS" if "ВЕРДИКТ: PASS" in up or "\nPASS" in up or up.startswith("PASS") else (
        "FAIL" if "ВЕРДИКТ: FAIL" in up or "\nFAIL" in up else (
            "REVIEW" if "ВЕРДИКТ: REVIEW" in up or "\nREVIEW" in up else "UNPARSED"))
    return verdict, str(out)[:400], dt


def main():
    rows = []
    plan = [(r, "ru") for r in GOOD_REPORTS + BAD_REPORTS]
    plan += [(r, lang) for r in HOLDOUT_REPORTS for lang in ("ru", "kk")]
    print(f"cases: {len(plan)}", flush=True)
    for i, (rep, lang) in enumerate(plan):
        text = rep["work_done_text"]
        mats = ", ".join(f"{m['name']} x{m['qty']}" for m in rep.get("materials", [])) or "—"
        exp = expected_of(rep)
        try:
            got, raw, dt = ask(text, rep.get("fault_code_id", "?"), mats)
        except Exception as e:  # noqa: BLE001
            got, raw, dt = "ERROR", str(e)[:200], -1
        ok = got == exp
        rows.append({"id": rep["id"], "lang": lang, "expected": exp, "got": got,
                     "match": ok, "seconds": dt, "raw": raw})
        print(f"[{i + 1}/{len(plan)}] {rep['id']}/{lang} exp={exp} got={got} {'OK' if ok else 'MISS'} {dt}s", flush=True)
    n = len(rows)
    m = sum(1 for r in rows if r["match"])
    summary = {"n": n, "match": m, "accuracy": round(m / n, 4) if n else 0,
               "by_expected": {}, "rows": rows}
    for r in rows:
        b = summary["by_expected"].setdefault(r["expected"], {"n": 0, "match": 0})
        b["n"] += 1
        b["match"] += int(r["match"])
    json.dump(summary, open("/tmp/estim_matrix_v2.json", "w"), ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, ensure_ascii=False))


if __name__ == "__main__":
    main()

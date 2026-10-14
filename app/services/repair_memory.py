"""Similar-repairs memory (rule-based, no external calls).

When a master issues a work order, showing the most relevant *previously closed*
repairs for the same equipment lets them reuse accepted work and materials
instead of inventing them. The router fetches candidates from `work_orders`;
this module ranks them and explains each match so the suggestion is auditable.

Deterministic on purpose: identical inputs always yield the same ranking.
"""

from __future__ import annotations

import json
from typing import Any

# A repair is "finished" for memory purposes once it left the worker's hands.
CLOSED_REPAIR_STATUSES = ("done", "ai_review", "closed")

REASON_TEXT: dict[str, dict[str, str]] = {
    "base": {"ru": "ремонт этого оборудования", "kk": "осы жабдықтың жөндеуі"},
    "same_fault": {"ru": "тот же шифр неисправности", "kk": "бірдей ақау шифры"},
    "work_done": {"ru": "описаны выполненные работы", "kk": "орындалған жұмыстар сипатталған"},
    "materials": {
        "ru": "указаны израсходованные материалы",
        "kk": "жұмсалған материалдар көрсетілген",
    },
    "no_rework": {"ru": "закрыт без доработок", "kk": "қайта өңдеусіз жабылды"},
}


def reason_text(key: str, lang: str = "ru") -> str:
    entry = REASON_TEXT[key]
    return entry.get(lang, entry["ru"])


def _as_materials(value: Any) -> list[dict[str, Any]]:
    """Accept a list (already parsed) or a JSON string from a json/jsonb column."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if isinstance(value, list):
        return [dict(m) for m in value if isinstance(m, dict)]
    return []


def rank_similar_repairs(
    candidates: list[dict[str, Any]],
    *,
    fault_code_id: Any | None = None,
    limit: int = 3,
    lang: str = "ru",
) -> list[dict[str, Any]]:
    """Score and explain past repairs. Never invents data: missing fields simply
    contribute no reason and no weight."""
    fault = str(fault_code_id) if fault_code_id else None
    ranked: list[dict[str, Any]] = []
    for c in candidates:
        score = 0.3
        reasons = [reason_text("base", lang)]
        if fault and str(c.get("fault_code_id") or "") == fault:
            score += 0.5
            reasons.insert(0, reason_text("same_fault", lang))
        if str(c.get("work_done_text") or "").strip():
            score += 0.1
            reasons.append(reason_text("work_done", lang))
        materials = _as_materials(c.get("materials"))
        if materials:
            score += 0.05
            reasons.append(reason_text("materials", lang))
        if int(c.get("return_count") or 0) == 0:
            score += 0.05
            reasons.append(reason_text("no_rework", lang))
        item = dict(c)
        item["materials"] = materials
        item["match_score"] = round(min(score, 0.99), 3)
        item["match_reasons"] = reasons
        ranked.append(item)

    ranked.sort(
        key=lambda item: (item["match_score"], str(item.get("closed_at") or "")), reverse=True
    )
    return ranked[: max(0, int(limit))]

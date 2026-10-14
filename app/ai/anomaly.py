"""Anomaly orchestration: deterministic detectors first (services/insights),
LLM only explains. See app/services/insights.py for detectors."""

from typing import Any

from app.ai import gateway as _gw


async def explain(headline: str, evidence: Any, recommendation: str, language: str = "ru") -> str:
    return await _gw.explain_anomaly(
        {"headline": headline, "evidence": evidence, "recommendation": recommendation}, language
    )

"""Anomaly orchestration: deterministic detectors first (services/insights),
LLM only explains. See app/services/insights.py for detectors."""
from app.ai import gateway as _gw


async def explain(headline: str, evidence, recommendation: str, language: str = "ru") -> str:
    return await _gw.explain_anomaly({"headline": headline, "evidence": evidence, "recommendation": recommendation}, language)

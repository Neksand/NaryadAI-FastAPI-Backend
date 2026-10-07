"""AI provider abstraction (spec §25). Business code talks to the gateway,
never to a vendor. Structured values stay language-independent; only
`explanation` is localized."""
from typing import Protocol


class AIProvider(Protocol):
    name: str

    async def recommend_worker(self, ctx: dict, language: str) -> dict: ...
    async def inspect_completion(self, ctx: dict, language: str) -> dict: ...
    async def explain_anomaly(self, ctx: dict, language: str) -> str: ...

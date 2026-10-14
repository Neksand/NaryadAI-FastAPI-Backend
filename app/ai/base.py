"""AI provider abstraction (spec §25). Business code talks to the gateway,
never to a vendor. Structured values stay language-independent; only
`explanation` is localized."""

from typing import Any, Protocol


class AIProvider(Protocol):
    name: str

    async def recommend_worker(self, ctx: dict[str, Any], language: str) -> dict[str, Any]: ...
    async def inspect_completion(self, ctx: dict[str, Any], language: str) -> dict[str, Any]: ...
    async def explain_anomaly(self, ctx: dict[str, Any], language: str) -> str: ...

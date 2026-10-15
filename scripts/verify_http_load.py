"""Bounded loopback-only HTTP sample; never targets production. No tokens logged."""

import asyncio
import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8080"
ENDPOINTS = ["/api/v1/work-orders?limit=20", "/api/v1/analytics/dashboard"]


async def main():
    started = time.monotonic()
    rows = []
    sem = asyncio.Semaphore(5)
    async with httpx.AsyncClient(
        base_url=BASE, timeout=15, trust_env=False, follow_redirects=False
    ) as client:
        auth = await client.post("/api/v1/auth/login", json={"login": "master1", "pin": "3333"})
        if auth.status_code != 200:
            raise RuntimeError(f"Local synthetic login failed: HTTP {auth.status_code}")
        token = auth.json()["access_token"]

        async def request(path):
            async with sem:
                before = time.monotonic()
                try:
                    response = await client.get(path, headers={"Authorization": f"Bearer {token}"})
                    status = response.status_code
                except httpx.HTTPError:
                    status = 0
                rows.append(
                    {"path": path, "status": status, "ms": (time.monotonic() - before) * 1000}
                )

        await asyncio.gather(*(request(ENDPOINTS[i % 2]) for i in range(100)))

    def summarize(selected):
        values = sorted(row["ms"] for row in selected)
        codes = {
            str(code): sum(row["status"] == code for row in selected)
            for code in sorted({row["status"] for row in selected})
        }
        return {
            "requests": len(selected),
            "errors": sum(row["status"] != 200 for row in selected),
            "p50_ms": round(statistics.median(values), 3),
            "p95_ms": round(values[int((len(values) - 1) * 0.95)], 3),
            "max_ms": round(max(values), 3),
            "status_counts": codes,
        }

    result = {
        "at": datetime.now(UTC).isoformat(),
        "base_url": BASE,
        "concurrency": 5,
        "wall_seconds": round(time.monotonic() - started, 3),
        "overall": summarize(rows),
        "endpoints": {
            path: summarize([row for row in rows if row["path"] == path]) for path in ENDPOINTS
        },
        "limitations": [
            "Local read-only sample of 100 HTTP GET requests; one synthetic login.",
            "No production throughput/SLA conclusion; concurrent development services may affect timing.",
            "No Android, external push, AI or write load measured.",
        ],
    }
    result["status"] = "PASS" if result["overall"]["errors"] == 0 else "FAIL"
    path = Path(__file__).resolve().parent.parent / "docs/evidence/load.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())

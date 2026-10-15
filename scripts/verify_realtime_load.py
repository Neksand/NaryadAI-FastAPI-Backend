"""Bounded loopback-only WebSocket fan-out verification, not a throughput benchmark.

Create one synthetic order and accept it once. Five authenticated subscribers
must receive that transition within five seconds. A sixth, unauthorized worker
checks subscription rejection and absence of the event. Retain all test records.
Never print token-bearing URLs, credentials, response bodies or exception text.

    python scripts/verify_realtime_load.py --output docs/evidence/realtime-load.json

Synthetic credentials can be overridden using VERIFY_{ACTOR}_{LOGIN,PIN}, as in
verify_live.py. Remote targets are prohibited; there is no remote override flag.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

try:
    from scripts.verify_live import LiveVerification, validate_target
except ModuleNotFoundError:
    from verify_live import LiveVerification, validate_target


def is_accept_event(message: dict[str, Any], order_id: str) -> bool:
    payload = message.get("payload")
    return (
        isinstance(payload, dict)
        and payload.get("id") == order_id
        and payload.get("action") == "accept"
    )


class RealtimeVerification(LiveVerification):
    def __init__(self, base: str) -> None:
        # Validation here also protects callers importing this class directly.
        super().__init__(validate_target(base))
        self.results["scenario"] = "five-subscriber local event delivery"
        self.results["subscriber_count"] = 5
        self.results["negative_observer_count"] = 1
        self.results["criterion_seconds"] = 5
        self.results["deliveries"] = []
        self.results["limitations"] = [
            "One synthetic order transition, five positive subscribers and one negative observer.",
            "Latency starts before the HTTP transition request; includes API and outbox delivery.",
            "One local API process; this is not throughput, saturation or production SLA evidence.",
            "No production network, mobile background delivery, TLS or multi-process fan-out test.",
            "Authenticated session and order/channel scope are exercised, not all role combinations.",
            "One synthetic order and login sessions are retained; no reset or deletion is performed.",
        ]

    def socket_url(self, token: str) -> str:
        parsed = urlsplit(self.base)
        return urlunsplit(
            (
                "wss" if parsed.scheme == "https" else "ws",
                parsed.netloc,
                "/api/ws",
                urlencode({"token": token}),
                "",
            )
        )

    async def subscribe(
        self, stack: AsyncExitStack, actor: str, channels: list[str]
    ) -> tuple[Any, dict[str, Any]]:
        import websockets

        socket = await stack.enter_async_context(
            websockets.connect(
                self.socket_url(self.tokens[actor]),
                open_timeout=5,
                close_timeout=1,
                proxy=None,
            )
        )
        await socket.send(json.dumps({"op": "subscribe", "channels": channels}))
        async with asyncio.timeout(5):
            while True:
                message = json.loads(await socket.recv())
                if message.get("type") == "SUBSCRIBED":
                    return socket, message

    async def observe(
        self, socket: Any, name: str, order_id: str, started: float, *, forbidden: bool = False
    ) -> dict[str, Any]:
        try:
            async with asyncio.timeout_at(started + 5):
                while True:
                    message = json.loads(await socket.recv())
                    if is_accept_event(message, order_id):
                        return {
                            "subscriber": name,
                            "status": "FAIL" if forbidden else "PASS",
                            "latency_seconds": round(time.monotonic() - started, 4),
                            "event_received": True,
                        }
        except TimeoutError:
            return {
                "subscriber": name,
                "status": "PASS" if forbidden else "FAIL",
                "event_received": False,
                "observation_seconds": 5,
            }

    async def scenario(self) -> None:
        import websockets
        from websockets.exceptions import ConnectionClosed

        await self.request("GET", "/health/ready", name="readiness")
        # Invalid-token rejection happens before opening the measured sockets.
        async with websockets.connect(
            self.socket_url("invalid-synthetic-token"),
            open_timeout=5,
            close_timeout=1,
            proxy=None,
        ) as invalid:
            try:
                await asyncio.wait_for(invalid.recv(), timeout=5)
            except ConnectionClosed as exc:
                self.check("invalid_token_rejected", bool(exc.rcvd and exc.rcvd.code == 4401))
            else:
                self.check("invalid_token_rejected", False)

        for actor, login, pin in [
            ("master", "master1", "3333"),
            ("worker", "worker01", "1001"),
            ("other", "worker06", "1006"),
            ("manager", "manager1", "2222"),
        ]:
            await self.login(actor, login, pin)
        equipment = (
            await self.request("GET", "/api/v1/dict/equipment", actor="master")
        ).json()["items"]
        common = set(self.users["master"]["area_ids"]) & set(self.users["worker"]["area_ids"])
        self.check("master_worker_common_area", bool(common))
        selected = next(item for item in equipment if item["area_id"] in common)
        order = (
            await self.request(
                "POST",
                "/api/v1/work-orders",
                actor="master",
                expected=(201,),
                key=str(uuid.uuid4()),
                name="create_synthetic_order",
                json={
                    "kind": "unplanned",
                    "description": f"WS-FANOUT-SYNTHETIC-{uuid.uuid4().hex[:10]}: тест доставки",
                    "equipment_id": selected["id"],
                    "assignee_id": self.users["worker"]["id"],
                    "priority": "normal",
                    "due_at": (datetime.now(UTC) + timedelta(hours=4)).isoformat(),
                },
            )
        ).json()
        order_id = order["id"]
        self.results["created_order_ids"].append(order_id)
        await self.request(
            "GET",
            f"/api/v1/work-orders/{order_id}",
            actor="other",
            expected=(403, 404),
            name="unassigned_worker_http_scope_denied",
        )

        specifications = [
            ("master_order", "master", [f"order:{order_id}"]),
            ("master_shift", "master", ["shift:current"]),
            ("worker_order", "worker", [f"order:{order_id}"]),
            ("worker_personal", "worker", ["user:me"]),
            ("manager_shift", "manager", ["shift"]),
        ]
        async with AsyncExitStack() as stack:
            subscribers = []
            for name, actor, channels in specifications:
                socket, ack = await self.subscribe(stack, actor, channels)
                self.check(
                    f"{name}_subscription",
                    ack.get("channels") == channels and not ack.get("rejected"),
                )
                subscribers.append((name, socket))
            denied_channels = [f"order:{order_id}", "shift:current"]
            forbidden_socket, ack = await self.subscribe(
                stack, "other", [*denied_channels, "user:me"]
            )
            self.check(
                "unassigned_worker_ws_scope_denied",
                ack.get("channels") == ["user:me"]
                and set(ack.get("rejected", [])) == set(denied_channels),
            )
            started = time.monotonic()
            async with asyncio.TaskGroup() as group:
                watchers = [
                    group.create_task(self.observe(socket, name, order_id, started))
                    for name, socket in subscribers
                ]
                negative = group.create_task(
                    self.observe(
                        forbidden_socket, "unassigned_worker", order_id, started, forbidden=True
                    )
                )
                transition = await self.transition(order_id, "accept")
                self.check("single_real_accept_transition", transition.get("to") == "accepted")
            deliveries = [watcher.result() for watcher in watchers]
            self.results["deliveries"] = deliveries
            self.results["negative_observer"] = negative.result()
            self.check("unauthorized_observer_received_no_transition", negative.result()["status"] == "PASS")
            self.check(
                "all_five_subscribers_received_within_5_seconds",
                len(deliveries) == 5
                and all(
                    item["status"] == "PASS" and item["latency_seconds"] < 5
                    for item in deliveries
                ),
            )
            self.results["metrics"]["maximum_delivery_seconds"] = max(
                item["latency_seconds"] for item in deliveries
            )
        response = await self.request(
            "GET", f"/api/v1/work-orders/{order_id}", actor="master", name="refetch_authoritative_state"
        )
        self.check("authoritative_status_accepted", response.json().get("status") == "accepted")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--output", type=Path, default=Path("docs/evidence/realtime-load.json"))
    args = parser.parse_args()
    try:
        verification = RealtimeVerification(args.base_url)
    except ValueError as exc:
        parser.error(str(exc))
    result = asyncio.run(verification.run())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "metrics": result["metrics"], "output": str(args.output)}))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

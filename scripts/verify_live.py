"""Non-destructive live API verification against a seeded, isolated demo server.

Creates three explicitly synthetic orders; does not reset or delete any records.
Default target is loopback. Remote execution requires --allow-remote and should
only target a test environment. Tokens, PINs and response bodies are not logged.

    python scripts/verify_live.py --output live-verification.json

Override synthetic credentials with VERIFY_{MASTER,WORKER,OTHER,MANAGER}_{LOGIN,PIN}.
Run with the repository's development dependencies installed (httpx/websockets/Pillow).
"""

from __future__ import annotations

import argparse
import asyncio
import io
import ipaddress
import json
import os
import platform
import random
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit


def validate_target(value: str, allow_remote: bool = False) -> str:
    """Reject accidental production targets and URL credentials before any I/O."""
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("Use an http(s) origin without credentials, path, query or fragment")
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname.lower() == "localhost"
    if not loopback and not allow_remote:
        raise ValueError(
            "Refusing non-loopback target; --allow-remote is required for a test server"
        )
    return value.rstrip("/")


def sample_photo(seed: str) -> bytes:
    """Synthetic pixels, intentionally distinct between orders and repeated runs."""
    from PIL import Image

    rng = random.Random(seed)
    data = bytes(rng.randrange(256) for _ in range(80 * 80 * 3))
    image = Image.frombytes("RGB", (80, 80), data)
    stream = io.BytesIO()
    image.save(stream, format="JPEG", quality=90)
    return stream.getvalue()


class LiveVerification:
    def __init__(self, base: str, review_timeout: float = 40) -> None:
        self.base = base
        self.review_timeout = review_timeout
        self.started = time.monotonic()
        self.results: dict[str, Any] = {
            "started_at": datetime.now(UTC).isoformat(),
            "base_url": base,
            "python": platform.python_version(),
            "status": "RUNNING",
            "checks": [],
            "metrics": {},
            "created_order_ids": [],
            "limitations": [
                "Synthetic local API test; no Android or browser interaction.",
                "WebSocket latency measures one local event, not a production SLA.",
                "External AI, FCM delivery, TLS and production deployment are not verified.",
                "Acceptance escalation (3/10 min) and manager escalation (120 min) are not time-accelerated.",
                "Test records are retained; no reset or deletion is performed.",
            ],
        }
        self.tokens: dict[str, str] = {}
        self.users: dict[str, dict[str, Any]] = {}
        self.client: Any = None

    def check(self, name: str, passed: bool, **detail: Any) -> None:
        self.results["checks"].append(
            {"name": name, "status": "PASS" if passed else "FAIL", **detail}
        )
        if not passed:
            raise AssertionError(name)

    async def request(
        self,
        method: str,
        path: str,
        *,
        actor: str | None = None,
        expected: tuple[int, ...] = (200,),
        name: str | None = None,
        key: str | None = None,
        **kwargs: Any,
    ) -> Any:
        headers = {}
        if actor:
            headers["Authorization"] = f"Bearer {self.tokens[actor]}"
        if key:
            headers["Idempotency-Key"] = key
        started = time.monotonic()
        response = await self.client.request(method, path, headers=headers, **kwargs)
        self.check(
            name or f"{method} {path}",
            response.status_code in expected,
            http_status=response.status_code,
            elapsed_ms=round((time.monotonic() - started) * 1000, 2),
        )
        return response

    async def login(self, actor: str, login: str, pin: str) -> None:
        login = os.environ.get(f"VERIFY_{actor.upper()}_LOGIN", login)
        pin = os.environ.get(f"VERIFY_{actor.upper()}_PIN", pin)
        response = await self.request(
            "POST",
            "/api/v1/auth/login",
            name=f"login_{actor}",
            json={"login": login, "pin": pin},
        )
        data = response.json()
        self.tokens[actor], self.users[actor] = data["access_token"], data["user"]

    async def transition(
        self, oid: str, action: str, actor: str = "worker", **data: Any
    ) -> dict[str, Any]:
        response = await self.request(
            "POST",
            f"/api/v1/work-orders/{oid}/transitions",
            actor=actor,
            key=str(uuid.uuid4()),
            name=f"transition_{action}",
            json={"action": action, **data},
        )
        return response.json()

    async def wait_review(self, oid: str, previous_attempt: int = 0) -> dict[str, Any]:
        started = time.monotonic()
        while time.monotonic() - started < self.review_timeout:
            response = await self.client.get(
                f"/api/v1/work-orders/{oid}/ai-review",
                headers={"Authorization": f"Bearer {self.tokens['master']}"},
            )
            if response.status_code == 200 and response.json().get("attempt", 0) > previous_attempt:
                data = response.json()
                self.check(
                    "background_ai_review_available",
                    True,
                    attempt=data["attempt"],
                    verdict=data["verdict"],
                )
                self.results["metrics"].setdefault("ai_review_seconds", []).append(
                    round(time.monotonic() - started, 3)
                )
                return data
            if response.status_code not in {200, 404}:
                self.check("background_ai_review_poll", False, http_status=response.status_code)
            await asyncio.sleep(0.25)
        self.check("background_ai_review_available", False, timeout_seconds=self.review_timeout)
        raise AssertionError("unreachable")

    async def measure_socket(self, oid: str) -> None:
        import websockets

        target = urlsplit(self.base)
        ws_url = urlunsplit(
            (
                "wss" if target.scheme == "https" else "ws",
                target.netloc,
                "/api/ws",
                urlencode({"token": self.tokens["master"]}),
                "",
            )
        )
        # The token URL is never included in diagnostics or result files.
        async with websockets.connect(
            ws_url, open_timeout=5, close_timeout=2, proxy=None
        ) as socket:
            await socket.send(json.dumps({"op": "subscribe", "channels": [f"order:{oid}"]}))
            ack = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
            self.check(
                "websocket_subscription",
                ack.get("type") == "SUBSCRIBED" and not ack.get("rejected"),
            )
            started = time.monotonic()
            key = str(uuid.uuid4())
            first = await self.request(
                "POST",
                f"/api/v1/work-orders/{oid}/transitions",
                actor="worker",
                key=key,
                name="accept_once",
                json={"action": "accept"},
            )
            replay = await self.request(
                "POST",
                f"/api/v1/work-orders/{oid}/transitions",
                actor="worker",
                key=key,
                name="accept_idempotent_replay",
                json={"action": "accept"},
            )
            self.check(
                "same_action_replayed",
                first.json().get("to") == "accepted" and replay.json().get("replayed") is True,
            )
            while time.monotonic() - started < 5:
                message = json.loads(
                    await asyncio.wait_for(
                        socket.recv(), timeout=max(0.01, 5 - (time.monotonic() - started))
                    )
                )
                payload = message.get("payload", {})
                if payload.get("id") == oid and payload.get("action") == "accept":
                    latency = round(time.monotonic() - started, 3)
                    self.results["metrics"]["websocket_accept_seconds"] = latency
                    self.check("websocket_update_under_5_seconds", latency < 5)
                    break
            else:
                self.check("websocket_update_under_5_seconds", False)
        # Reconnect must permit a fresh subscription; authoritative state is refetched.
        async with websockets.connect(
            ws_url, open_timeout=5, close_timeout=2, proxy=None
        ) as socket:
            await socket.send(json.dumps({"op": "subscribe", "channels": [f"order:{oid}"]}))
            ack = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
            self.check("websocket_reconnect", ack.get("type") == "SUBSCRIBED")
        response = await self.request(
            "GET", f"/api/v1/work-orders/{oid}", actor="master", name="refetch_after_reconnect"
        )
        self.check(
            "authoritative_state_after_reconnect", response.json().get("status") == "accepted"
        )

    async def run(self) -> dict[str, Any]:
        import httpx

        try:
            async with httpx.AsyncClient(
                base_url=self.base, timeout=15, follow_redirects=False, trust_env=False
            ) as self.client:
                await self.scenario()
            self.results["status"] = "PASS"
        except Exception as exc:
            self.results["status"] = "FAIL"
            # Avoid exception strings: transport exceptions can include token-bearing URLs.
            self.results["error_type"] = type(exc).__name__
            if isinstance(exc, AssertionError):
                self.results["failed_check"] = str(exc)
        self.results["metrics"]["total_seconds"] = round(time.monotonic() - self.started, 3)
        self.results["finished_at"] = datetime.now(UTC).isoformat()
        return self.results

    async def scenario(self) -> None:
        await self.request("GET", "/health/ready", name="readiness")
        await self.request("GET", "/api/v1/work-orders", expected=(401,), name="anonymous_denied")
        for actor, login, pin in [
            ("master", "master1", "3333"),
            ("worker", "worker01", "1001"),
            ("other", "worker06", "1006"),
            ("manager", "manager1", "2222"),
        ]:
            await self.login(actor, login, pin)
        equipment = (
            await self.request(
                "GET", "/api/v1/dict/equipment", actor="master", name="equipment_reference"
            )
        ).json()["items"]
        faults = (
            await self.request(
                "GET", "/api/v1/dict/fault-codes", actor="master", name="fault_reference"
            )
        ).json()["items"]
        reasons = (
            await self.request(
                "GET", "/api/v1/dict/reasons", actor="master", name="reason_reference"
            )
        ).json()["items"]
        common = set(self.users["worker"]["area_ids"]) & set(self.users["master"]["area_ids"])
        eq = next(item for item in equipment if item["area_id"] in common)
        reason = {
            kind: next(item["code"] for item in reasons if item["kind"] == kind)
            for kind in ("pause", "reject")
        }
        tag = f"LIVE-SYNTHETIC-{uuid.uuid4().hex[:10]}"
        payload = {
            "kind": "unplanned",
            "description": f"{tag}: течь масла подшипника, требуется замена",
            "equipment_id": eq["id"],
            "assignee_id": self.users["worker"]["id"],
            "priority": "high",
            "due_at": (datetime.now(UTC) + timedelta(hours=4)).isoformat(),
        }
        await self.request(
            "POST",
            "/api/v1/work-orders",
            actor="worker",
            expected=(403,),
            name="worker_create_denied",
            json=payload,
        )
        key = str(uuid.uuid4())
        created = (
            await self.request(
                "POST",
                "/api/v1/work-orders",
                actor="master",
                expected=(201,),
                key=key,
                name="master_create",
                json=payload,
            )
        ).json()
        oid = created["id"]
        self.results["created_order_ids"].append(oid)
        replay = (
            await self.request(
                "POST",
                "/api/v1/work-orders",
                actor="master",
                expected=(201,),
                key=key,
                name="create_replay",
                json=payload,
            )
        ).json()
        self.check("create_exactly_once", replay["id"] == oid and replay.get("replayed") is True)
        await self.request(
            "POST",
            "/api/v1/work-orders",
            actor="master",
            expected=(409,),
            key=key,
            name="same_key_different_payload_denied",
            json={**payload, "description": tag + " changed"},
        )
        await self.request(
            "GET",
            f"/api/v1/work-orders/{oid}",
            actor="other",
            expected=(403, 404),
            name="other_worker_read_denied",
        )
        endpoint = f"/api/v1/work-orders/{oid}/transitions"
        await self.request(
            "POST",
            endpoint,
            actor="other",
            expected=(403, 404),
            name="other_worker_mutation_denied",
            json={"action": "accept"},
        )
        await self.request(
            "POST",
            endpoint,
            actor="manager",
            expected=(403,),
            name="manager_mutation_denied",
            json={"action": "accept"},
        )
        await self.request(
            "POST",
            endpoint,
            actor="worker",
            expected=(409,),
            name="start_before_accept_denied",
            json={"action": "start"},
        )
        await self.measure_socket(oid)
        await self.transition(oid, "start")
        await self.request(
            "POST",
            endpoint,
            actor="worker",
            expected=(422,),
            name="pause_requires_reason",
            json={"action": "pause"},
        )
        await self.transition(
            oid,
            "pause",
            reason_code=reason["pause"],
            comment="Синтетическая проверка ожидания материала",
        )
        await self.transition(oid, "resume")
        form = {
            "work_done_text": "Заменён подшипник, течь масла устранена, выполнена проверка запуска",
            "fault_code_id": faults[0]["id"],
            "materials": [],
        }
        await self.request(
            "POST",
            endpoint,
            actor="worker",
            expected=(422,),
            name="unplanned_completion_requires_after_photo",
            json={"action": "complete", "form": form},
        )
        await self.request(
            "POST",
            f"/api/v1/work-orders/{oid}/photos",
            actor="worker",
            expected=(415, 422),
            name="invalid_image_bytes_denied",
            files={"file": ("invalid.jpg", b"not an image", "image/jpeg")},
            data={"kind": "after"},
        )
        await self.request(
            "POST",
            f"/api/v1/work-orders/{oid}/photos",
            actor="worker",
            expected=(415, 422),
            name="forged_jpeg_header_denied",
            files={"file": ("forged.jpg", b"\xff\xd8\xffnot-a-real-image", "image/jpeg")},
            data={"kind": "after"},
        )
        await self.request(
            "POST",
            f"/api/v1/work-orders/{oid}/photos",
            actor="worker",
            name="synthetic_after_photo_uploaded",
            files={"file": ("synthetic-after.jpg", sample_photo(tag), "image/jpeg")},
            data={"kind": "after"},
        )
        # Deliberately bad synthetic report must trigger a rules verdict while
        # leaving the actual workflow decision to the master.
        await self.transition(
            oid,
            "complete",
            form={**form, "work_done_text": form["work_done_text"] + "; отключил защиту"},
        )
        review = await self.wait_review(oid)
        self.check("safety_rule_requires_rework", review["verdict"] == "needs_rework")
        state = (
            await self.request(
                "GET", f"/api/v1/work-orders/{oid}", actor="master", name="state_after_ai"
            )
        ).json()
        self.check("ai_keeps_human_decision_pending", state["status"] == "ai_review")
        await self.request(
            "POST",
            endpoint,
            actor="worker",
            expected=(403, 409),
            name="worker_cannot_close",
            json={
                "action": "close",
                "decision": "override",
                "score": 75,
                "comment": "Синтетическая проверка прав",
            },
        )
        await self.transition(
            oid, "return_to_rework", actor="master", comment="Уточните результаты проверки запуска"
        )
        await self.transition(oid, "start")
        await self.transition(
            oid,
            "complete",
            form={
                **form,
                "work_done_text": form["work_done_text"] + "; повторно проверен запуск без течи",
            },
        )
        await self.wait_review(oid, previous_attempt=int(review["attempt"]))
        closed = await self.transition(
            oid,
            "close",
            actor="master",
            decision="override",
            score=85,
            comment="Синтетическая приёмка мастером: фото и испытание проверены",
        )
        self.check("human_final_close", closed["to"] == "closed")
        await self.request(
            "POST",
            endpoint,
            actor="worker",
            expected=(409,),
            name="terminal_state_immutable",
            json={"action": "start"},
        )
        events = (
            await self.request(
                "GET", f"/api/v1/work-orders/{oid}/events", actor="master", name="audit_history"
            )
        ).json()["items"]
        actions = [event["action"] for event in events]
        required = {"accept", "start", "pause", "resume", "complete", "return_to_rework", "close"}
        human = [event for event in events if event["action"] in required]
        self.check(
            "audit_authors_times_and_lifecycle",
            required.issubset(actions)
            and all(event.get("actor_id") and event.get("at") for event in human),
        )
        self.check("idempotent_accept_single_event", actions.count("accept") == 1)
        report = (
            await self.request(
                "GET",
                f"/api/v1/work-orders/{oid}/report?audience=master",
                actor="master",
                name="order_report",
            )
        ).json()
        self.check(
            "report_contains_human_decision",
            report["order"]["status"] == "closed" and report["ai_review"].get("master_score") == 85,
        )
        for path in [
            "/api/v1/shift/board",
            "/api/v1/shift/summary",
            "/api/v1/analytics/dashboard",
            "/api/v1/analytics/ratings",
        ]:
            await self.request("GET", path, actor="master", name=path.rsplit("/", 1)[-1] + "_read")
        notifications = (
            await self.request(
                "GET",
                "/api/v1/notifications?limit=200",
                actor="worker",
                name="worker_notifications",
            )
        ).json()["data"]
        self.check(
            "assignment_notification_persisted",
            any(
                item.get("related_entity_id") == oid and item.get("type") == "WORK_ORDER_ASSIGNED"
                for item in notifications
            ),
        )
        second = (
            await self.request(
                "POST",
                "/api/v1/work-orders",
                actor="master",
                expected=(201,),
                key=str(uuid.uuid4()),
                name="create_rejection_scenario",
                json={**payload, "description": tag + ": тест отказа и очереди"},
            )
        ).json()["id"]
        self.results["created_order_ids"].append(second)
        await self.transition(second, "queue")
        await self.request(
            "POST",
            f"/api/v1/work-orders/{second}/transitions",
            actor="worker",
            expected=(422,),
            name="rejection_requires_reason",
            json={"action": "reject"},
        )
        rejected = await self.transition(
            second,
            "reject",
            reason_code=reason["reject"],
            comment="Синтетическая проверка мотивированного отказа",
        )
        self.check("rejection_recorded", rejected["to"] == "rejected")
        await self.verify_overdue(payload, tag)

    async def verify_overdue(self, payload: dict[str, Any], tag: str) -> None:
        due = datetime.now(UTC) + timedelta(seconds=3)
        overdue_id = (
            await self.request(
                "POST",
                "/api/v1/work-orders",
                actor="master",
                expected=(201,),
                key=str(uuid.uuid4()),
                name="create_short_deadline_scenario",
                json={
                    **payload,
                    "description": tag + ": проверка просрочки",
                    "due_at": due.isoformat(),
                },
            )
        ).json()["id"]
        self.results["created_order_ids"].append(overdue_id)
        started = time.monotonic()
        received: set[str] = set()
        while time.monotonic() - started < 40:
            for actor in {"master", "worker"} - received:
                response = await self.client.get(
                    "/api/v1/notifications?limit=200",
                    headers={"Authorization": f"Bearer {self.tokens[actor]}"},
                )
                if response.status_code != 200:
                    self.check("overdue_notification_poll", False, http_status=response.status_code)
                if any(
                    item.get("related_entity_id") == overdue_id
                    and item.get("type") == "DEADLINE_EXCEEDED"
                    for item in response.json()["data"]
                ):
                    received.add(actor)
            if received == {"master", "worker"}:
                self.results["metrics"]["overdue_notification_after_deadline_seconds"] = round(
                    (datetime.now(UTC) - due).total_seconds(), 3
                )
                self.check("real_deadline_worker_and_master_notification", True)
                return
            await asyncio.sleep(1)
        self.check(
            "real_deadline_worker_and_master_notification",
            False,
            recipients=sorted(received),
            timeout_seconds=40,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("live-verification.json"))
    parser.add_argument("--review-timeout", type=float, default=40)
    args = parser.parse_args()
    try:
        base = validate_target(args.base_url, args.allow_remote)
    except ValueError as exc:
        parser.error(str(exc))
    if not 1 <= args.review_timeout <= 120:
        parser.error("--review-timeout must be between 1 and 120 seconds")
    result = asyncio.run(LiveVerification(base, args.review_timeout).run())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "checks": len(result["checks"]),
                "metrics": result["metrics"],
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from . import e4_sub10ms_repairs_v12 as repairs
from . import e4_v12_authority as authority

core = repairs.core
LOGGER = logging.getLogger("gambit.e4.sub10ms.transport.final.v12")


@dataclass(slots=True)
class DispatchTelemetry:
    request_id: str
    route: str
    started_perf_ns: int
    request_sent_perf_ns: int
    response_perf_ns: int
    started_wall_ns: int
    response_wall_ns: int
    accepted: bool
    http_status: int
    error: str = ""

    @property
    def acknowledgement_ms(self) -> float:
        return max(0.0, (self.response_perf_ns - self.started_perf_ns) / 1e6)


TELEMETRY: deque[DispatchTelemetry] = deque(maxlen=50_000)


def _csv(name: str) -> list[str]:
    return [value.strip() for value in os.getenv(name, "").split(",") if value.strip()]


def _origin(url: str) -> str:
    parsed = urlsplit(str(url))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))


_BaseSender = repairs.FastPersistentRouteSender


class FinalPersistentRouteSender(_BaseSender):
    """Drop-in sender preserving the legacy `_session()` API.

    Every route receives the exact same signed transaction immediately. The
    class deliberately keeps `_session()` callable because existing benchmark,
    engine and cleanup code use that interface.
    """

    def __init__(self, settings: Any, rpc: Any) -> None:
        super().__init__(settings, rpc)
        # An older RouteSender ancestor stored a session/None directly in the
        # instance attribute `_session`, shadowing this class's method. Remove
        # only that instance-level value so normal method resolution is
        # restored. Retain an actual inherited ClientSession for safe cleanup.
        shadowed_session = self.__dict__.pop("_session", None)
        self._shadowed_parent_session: aiohttp.ClientSession | None = (
            shadowed_session
            if isinstance(shadowed_session, aiohttp.ClientSession)
            else None
        )
        # The parent may maintain its own `_http_session`; final V12 uses a
        # separate explicit field so it never shadows the `_session()` method.
        self._final_http_session: aiohttp.ClientSession | None = None
        self._final_keepalive_task: asyncio.Task[Any] | None = None
        self._request_counter = 0
        self._payload_cache: dict[tuple[str, str], bytes] = {}
        self._payload_cache_order: deque[tuple[str, str]] = deque(maxlen=16)
        self._headers_cache: dict[str, dict[str, str]] = {}
        additions: list[tuple[str, str]] = []
        relay = os.getenv("E4_ALLENHARK_RELAY_URL", "").strip()
        if relay:
            additions.append(("allenhark_relay", relay))
        additions.extend(
            (f"jito_{index}", url)
            for index, url in enumerate(_csv("E4_JITO_SEND_TRANSACTION_URLS"), 1)
        )
        additions.extend(
            (f"rpc_fanout_{index}", url)
            for index, url in enumerate(_csv("E4_RPC_FANOUT_URLS"), 1)
        )
        existing = {(str(name), str(url)) for name, url in getattr(self, "routes", [])}
        for route in reversed(additions):
            if route not in existing:
                self.routes.insert(0, route)
                existing.add(route)

    def _session(self) -> aiohttp.ClientSession:
        if self._final_http_session is None or self._final_http_session.closed:
            self._final_http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=1.5, sock_connect=0.4, sock_read=1.0),
                connector=aiohttp.TCPConnector(
                    limit=max(64, len(getattr(self, "routes", [])) * 16),
                    limit_per_host=32,
                    ttl_dns_cache=3_600,
                    keepalive_timeout=90,
                    enable_cleanup_closed=True,
                    happy_eyeballs_delay=0.0,
                ),
                json_serialize=lambda value: json.dumps(value, separators=(",", ":")),
            )
        return self._final_http_session

    @staticmethod
    def _kind(name: str) -> str:
        value = name.split("#", 1)[0].lower()
        if value.startswith("allenhark"):
            return "allenhark"
        if value.startswith("jito") or "block_engine" in value:
            return "jito"
        return "rpc"

    def _headers_for(self, name: str) -> dict[str, str]:
        headers = {"content-type": "application/json", "accept": "application/json"}
        kind = self._kind(name)
        if kind == "allenhark":
            key = os.getenv("E4_ALLENHARK_API_KEY", "").strip()
            if key:
                headers["x-api-key"] = key
        elif kind == "jito":
            key = os.getenv("E4_JITO_AUTH_UUID", "").strip()
            if key:
                headers["x-jito-auth"] = key
        try:
            configured = super()._headers(name.split("#", 1)[0])
            if configured:
                headers.update(configured)
        except (AttributeError, TypeError):
            LOGGER.debug("route-specific headers unavailable route=%s", name)
        return headers

    def _payload(self, name: str, tx: str) -> dict[str, Any]:
        if self._kind(name) == "allenhark":
            return {"tx": tx, "simulate": False}
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                tx,
                {
                    "encoding": "base64",
                    "skipPreflight": True,
                    "maxRetries": 0,
                },
            ],
        }

    def _cached_headers_for(self, name: str) -> dict[str, str]:
        key = str(name)
        cached = self._headers_cache.get(key)
        if cached is None:
            cached = self._headers_for(key)
            self._headers_cache[key] = cached
        return cached

    def _payload_bytes(
        self,
        name: str,
        tx: str,
        expected_signature: str,
    ) -> bytes:
        kind = self._kind(name)
        key = (expected_signature, kind)
        cached = self._payload_cache.get(key)
        if cached is not None:
            return cached
        payload = json.dumps(
            self._payload(name, tx),
            separators=(",", ":"),
        ).encode()
        if len(self._payload_cache_order) == self._payload_cache_order.maxlen:
            stale = self._payload_cache_order.popleft()
            self._payload_cache.pop(stale, None)
        self._payload_cache_order.append(key)
        self._payload_cache[key] = payload
        return payload

    async def _warm_one(self, name: str, url: str) -> None:
        origin = _origin(url)
        if not origin:
            return
        try:
            async with self._session().get(
                origin,
                headers={"accept": "application/json"},
            ) as response:
                await response.read()
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            LOGGER.debug("route warmup failed route=%s error=%s", name, exc)

    async def warm(self) -> None:
        tasks = [
            asyncio.create_task(self._warm_one(str(name), str(url)))
            for name, url in getattr(self, "routes", [])
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._final_keepalive_task is None:
            self._final_keepalive_task = asyncio.create_task(
                self._keepalive_loop(),
                name="e4-v12-final-route-keepalive",
            )

    async def _keepalive_loop(self) -> None:
        while self._final_http_session is not None and not self._final_http_session.closed:
            tasks = [
                asyncio.create_task(self._warm_one(str(name), str(url)))
                for name, url in getattr(self, "routes", [])
            ]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(7.5)

    async def _send(
        self,
        index: int,
        name: str,
        url: str,
        tx: str,
        expected_signature: str,
    ):
        del index  # Explicitly remove all inherited route staggering.
        started_perf = time.perf_counter_ns()
        started_wall = time.time_ns()
        request_sent = started_perf
        accepted = False
        status = 0
        error = ""
        returned = expected_signature
        self._request_counter += 1
        request_id = f"{self._request_counter}:{expected_signature[:16]}"
        try:
            request_sent = time.perf_counter_ns()
            async with self._session().post(
                url,
                data=self._payload_bytes(name, tx, expected_signature),
                headers=self._cached_headers_for(name),
            ) as response:
                status = response.status
                text = await response.text()
                if response.status >= 400:
                    raise RuntimeError(f"HTTP {response.status}: {text[:300]}")
                body = json.loads(text) if text else {}
                if isinstance(body, dict) and body.get("error"):
                    raise RuntimeError(str(body["error"]))
                if self._kind(name) == "allenhark":
                    state = str((body or {}).get("status") or "accepted").lower()
                    if state in {"rejected", "failed", "error"}:
                        raise RuntimeError(str((body or {}).get("error") or state))
                    returned = str(
                        (body or {}).get("signature")
                        or (body or {}).get("result")
                        or expected_signature
                    )
                else:
                    returned = str((body or {}).get("result") or expected_signature)
                accepted = True
                response_wall = time.time_ns()
                return core.RouteResult(
                    name,
                    started_wall,
                    response_wall,
                    True,
                    returned or expected_signature,
                )
        except (
            aiohttp.ClientError,
            TimeoutError,
            json.JSONDecodeError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            error = str(exc)
            response_wall = time.time_ns()
            return core.RouteResult(
                name,
                started_wall,
                response_wall,
                False,
                "rejected",
                error,
            )
        finally:
            TELEMETRY.append(DispatchTelemetry(
                request_id=request_id,
                route=name,
                started_perf_ns=started_perf,
                request_sent_perf_ns=request_sent,
                response_perf_ns=time.perf_counter_ns(),
                started_wall_ns=started_wall,
                response_wall_ns=time.time_ns(),
                accepted=accepted,
                http_status=status,
                error=error,
            ))

    async def submit(
        self,
        tx: str,
        signature: str,
    ):
        """Race all routes immediately and begin confirmation on first acceptance.

        Remaining route responses are still collected for telemetry, but a slow
        duplicate route can no longer postpone confirmation polling.
        """
        tasks = [
            asyncio.create_task(
                self._send(index, str(name), str(url), tx, signature),
                name=f"v12-route-{name}",
            )
            for index, (name, url) in enumerate(getattr(self, "routes", []))
        ]
        if not tasks:
            return "NONE", False, None, "no execution routes", []

        first_accepted = None
        confirmation_task: asyncio.Task[Any] | None = None
        try:
            for completed in asyncio.as_completed(tasks):
                result = await completed
                if result.accepted and first_accepted is None:
                    first_accepted = result
                    confirmation_task = asyncio.create_task(
                        self.rpc.confirm(
                            signature,
                            self.settings.confirmation_timeout_seconds,
                        ),
                        name="v12-first-ack-confirmation",
                    )
                    break

            results = await asyncio.gather(*tasks)
            accepted = [item for item in results if item.accepted]
            if not accepted:
                return (
                    "NONE",
                    False,
                    None,
                    "; ".join(f"{item.name}:{item.error}" for item in results),
                    results,
                )
            winner = min(accepted, key=lambda item: item.completed_ns)
            if confirmation_task is None:
                confirmation_task = asyncio.create_task(
                    self.rpc.confirm(
                        signature,
                        self.settings.confirmation_timeout_seconds,
                    )
                )
            confirmed, slot, error = await confirmation_task
            return winner.name, confirmed, slot, error, results
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

    async def close(self) -> None:
        if self._final_keepalive_task is not None:
            self._final_keepalive_task.cancel()
            await asyncio.gather(self._final_keepalive_task, return_exceptions=True)
            self._final_keepalive_task = None
        if self._final_http_session is not None:
            await self._final_http_session.close()
            self._final_http_session = None
        if self._shadowed_parent_session is not None and not self._shadowed_parent_session.closed:
            await self._shadowed_parent_session.close()
            self._shadowed_parent_session = None
        parent_close = getattr(super(), "close", None)
        if callable(parent_close):
            value = parent_close()
            if asyncio.iscoroutine(value):
                await value


_ACTIVE_SENDER = authority.install_route_sender(
    core,
    FinalPersistentRouteSender,
    priority=authority.ROUTE_PRIORITY_FINAL,
    authority="v12_final_transport",
)
if _ACTIVE_SENDER is FinalPersistentRouteSender:
    repairs.FastPersistentRouteSender = FinalPersistentRouteSender


def telemetry_snapshot() -> list[dict[str, Any]]:
    return [
        {**asdict(row), "acknowledgement_ms": row.acknowledgement_ms}
        for row in TELEMETRY
    ]

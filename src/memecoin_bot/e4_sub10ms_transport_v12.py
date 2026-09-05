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

core = repairs.core
LOGGER = logging.getLogger("gambit.e4.sub10ms.transport.v12")


@dataclass(slots=True)
class DispatchTelemetry:
    request_id: str
    route: str
    started_ns: int
    request_sent_ns: int
    response_ns: int
    accepted: bool
    status: int
    error: str = ""

    @property
    def acknowledgement_ms(self) -> float:
        return max(0.0, (self.response_ns - self.started_ns) / 1e6)


TELEMETRY: deque[DispatchTelemetry] = deque(maxlen=20_000)


def _csv(name: str) -> list[str]:
    return [value.strip() for value in os.getenv(name, "").split(",") if value.strip()]


def _origin_keepalive(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))


_PreviousSender = core.RouteSender


class Sub10msPersistentSender(_PreviousSender):
    """Persistent same-signature fan-out with route-specific wire formats.

    The first successful acknowledgement is observable independently from the
    later confirmation path.  All routes receive the exact same signed wire
    bytes at once; no per-route sleep or re-signing is allowed.
    """

    def __init__(self, settings: Any, rpc: Any) -> None:
        super().__init__(settings, rpc)
        self._session: aiohttp.ClientSession | None = None
        self._keepalive_task: asyncio.Task[Any] | None = None
        self._request_counter = 0
        additions: list[tuple[str, str]] = []
        relay = os.getenv("E4_ALLENHARK_RELAY_URL", "").strip()
        if relay:
            additions.append(("allenhark_relay", relay))
        additions.extend((f"jito_{index}", url) for index, url in enumerate(_csv("E4_JITO_SEND_TRANSACTION_URLS"), 1))
        additions.extend((f"rpc_fanout_{index}", url) for index, url in enumerate(_csv("E4_RPC_FANOUT_URLS"), 1))
        existing = {(str(name), str(url)) for name, url in getattr(self, "routes", [])}
        for item in reversed(additions):
            if item not in existing:
                self.routes.insert(0, item)
                existing.add(item)

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=1.5, sock_connect=0.4, sock_read=1.0)
            connector = aiohttp.TCPConnector(
                limit=max(64, len(getattr(self, "routes", [])) * 16),
                limit_per_host=32,
                ttl_dns_cache=3_600,
                keepalive_timeout=90,
                enable_cleanup_closed=True,
                happy_eyeballs_delay=0.0,
            )
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                json_serialize=lambda value: json.dumps(value, separators=(",", ":")),
            )
        return self._session

    def _route_kind(self, name: str) -> str:
        value = name.split("#", 1)[0].lower()
        if value.startswith("allenhark"):
            return "allenhark"
        if value.startswith("jito") or "block_engine" in value:
            return "jito"
        return "rpc"

    def _headers_for_route(self, name: str) -> dict[str, str]:
        headers = {"content-type": "application/json", "accept": "application/json"}
        kind = self._route_kind(name)
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
        except Exception:
            pass
        return headers

    def _payload(self, name: str, tx: str) -> dict[str, Any]:
        kind = self._route_kind(name)
        if kind == "allenhark":
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

    async def warm(self) -> None:
        session = self._http()
        tasks = []
        for name, url in getattr(self, "routes", []):
            keepalive = _origin_keepalive(str(url))
            if keepalive:
                tasks.append(asyncio.create_task(self._warm_one(session, name, keepalive)))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._keepalive_task is None:
            self._keepalive_task = asyncio.create_task(
                self._keepalive_loop(),
                name="e4-v12-sub10ms-route-keepalive",
            )

    async def _warm_one(self, session: aiohttp.ClientSession, name: str, url: str) -> None:
        try:
            async with session.get(url, headers={"accept": "application/json"}) as response:
                await response.read()
        except Exception as exc:
            LOGGER.debug("route warmup failed route=%s error=%s", name, exc)

    async def _keepalive_loop(self) -> None:
        while self._session is not None and not self._session.closed:
            session = self._session
            tasks = [
                asyncio.create_task(self._warm_one(session, str(name), _origin_keepalive(str(url))))
                for name, url in getattr(self, "routes", [])
                if _origin_keepalive(str(url))
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
        del index  # No route staggering in V12's low-latency transport.
        started_ns = time.perf_counter_ns()
        self._request_counter += 1
        request_id = f"{self._request_counter}:{expected_signature[:12]}"
        request_sent_ns = started_ns
        status = 0
        accepted = False
        error = ""
        returned = expected_signature
        try:
            session = self._http()
            request_sent_ns = time.perf_counter_ns()
            async with session.post(
                url,
                json=self._payload(name, tx),
                headers=self._headers_for_route(name),
            ) as response:
                status = response.status
                text = await response.text()
                if response.status >= 400:
                    raise RuntimeError(f"HTTP {response.status}: {text[:300]}")
                body = json.loads(text) if text else {}
                if isinstance(body, dict) and body.get("error"):
                    raise RuntimeError(str(body["error"]))
                kind = self._route_kind(name)
                if kind == "allenhark":
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
                return core.RouteResult(
                    name,
                    time.time_ns(),
                    time.time_ns(),
                    True,
                    returned or expected_signature,
                )
        except Exception as exc:
            error = str(exc)
            return core.RouteResult(
                name,
                time.time_ns(),
                time.time_ns(),
                False,
                "rejected",
                error,
            )
        finally:
            TELEMETRY.append(DispatchTelemetry(
                request_id=request_id,
                route=name,
                started_ns=started_ns,
                request_sent_ns=request_sent_ns,
                response_ns=time.perf_counter_ns(),
                accepted=accepted,
                status=status,
                error=error,
            ))

    async def close(self) -> None:
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            await asyncio.gather(self._keepalive_task, return_exceptions=True)
            self._keepalive_task = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        close = getattr(super(), "close", None)
        if close is not None:
            value = close()
            if asyncio.iscoroutine(value):
                await value


core.RouteSender = Sub10msPersistentSender
repairs.FastPersistentRouteSender = Sub10msPersistentSender


def telemetry_snapshot() -> list[dict[str, Any]]:
    return [
        {**asdict(item), "acknowledgement_ms": item.acknowledgement_ms}
        for item in TELEMETRY
    ]

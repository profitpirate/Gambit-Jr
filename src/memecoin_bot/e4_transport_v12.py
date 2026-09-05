from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from . import e4_role_model_v12 as role_model

core = role_model.core
LOGGER = logging.getLogger("gambit.e4.transport.v12")
_PREVIOUS_ROUTE_SENDER = core.RouteSender


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number and abs(number) != float("inf") else default


@dataclass(frozen=True, slots=True)
class DispatchTelemetry:
    route: str
    started_ns: int
    response_ns: int
    success: bool
    status: str

    @property
    def elapsed_ms(self) -> float:
        return max(0.0, (self.response_ns - self.started_ns) / 1_000_000.0)


class WarmFanoutRouteSender(_PREVIOUS_ROUTE_SENDER):
    """Keep transport sockets hot and dispatch one signed transaction at once.

    This changes only transport scheduling. All routes receive the same signed
    transaction/signature, preserving idempotency. There is no stagger between
    route copies. The first successful response wins; later route responses are
    diagnostic duplicates of the same signature, not separate orders.
    """

    def __init__(self, settings: Any, rpc: Any) -> None:
        super().__init__(settings, rpc)
        self._http_session: aiohttp.ClientSession | None = None
        self._warm_task: asyncio.Task[Any] | None = None
        self._closed = False
        self._telemetry: list[DispatchTelemetry] = []
        self._telemetry_limit = max(128, int(_finite(os.getenv("E4_V12_TRANSPORT_TELEMETRY_LIMIT"), 2048)))
        self._relay_url = os.getenv("E4_ALLENHARK_RELAY_URL", "").strip()
        self._relay_api_key = os.getenv("E4_ALLENHARK_API_KEY", "").strip()
        self._relay_keepalive_url = os.getenv(
            "E4_ALLENHARK_KEEPALIVE_URL",
            self._default_keepalive_url(self._relay_url),
        ).strip()
        if self._relay_url and all(str(name).split("#", 1)[0] != "allenhark_relay" for name, _ in self.routes):
            self.routes.insert(0, ("allenhark_relay", self._relay_url))

    @staticmethod
    def _default_keepalive_url(relay_url: str) -> str:
        if not relay_url:
            return ""
        parsed = urlsplit(relay_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        return urlunsplit((parsed.scheme, parsed.netloc, "/keepalive", "", ""))

    @property
    def telemetry(self) -> tuple[DispatchTelemetry, ...]:
        return tuple(self._telemetry)

    def _record(self, item: DispatchTelemetry) -> None:
        self._telemetry.append(item)
        if len(self._telemetry) > self._telemetry_limit:
            del self._telemetry[: len(self._telemetry) - self._telemetry_limit]

    def _session(self) -> aiohttp.ClientSession:
        if self._closed:
            raise RuntimeError("E4 V12 transport is closed")
        if self._http_session is None or self._http_session.closed:
            timeout = aiohttp.ClientTimeout(
                total=max(0.25, _finite(os.getenv("E4_ROUTE_TIMEOUT_SECONDS"), 1.5)),
                connect=max(0.10, _finite(os.getenv("E4_ROUTE_CONNECT_TIMEOUT_SECONDS"), 0.55)),
                sock_connect=max(0.10, _finite(os.getenv("E4_ROUTE_CONNECT_TIMEOUT_SECONDS"), 0.55)),
                sock_read=max(0.20, _finite(os.getenv("E4_ROUTE_READ_TIMEOUT_SECONDS"), 1.25)),
            )
            connector = aiohttp.TCPConnector(
                limit=max(16, int(_finite(os.getenv("E4_ROUTE_CONNECTION_LIMIT"), 128))),
                limit_per_host=max(4, int(_finite(os.getenv("E4_ROUTE_CONNECTIONS_PER_HOST"), 32))),
                ttl_dns_cache=max(60, int(_finite(os.getenv("E4_ROUTE_DNS_TTL_SECONDS"), 600))),
                keepalive_timeout=max(15.0, _finite(os.getenv("E4_ROUTE_KEEPALIVE_SECONDS"), 75.0)),
                enable_cleanup_closed=True,
                family=socket.AF_UNSPEC,
            )
            self._http_session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                raise_for_status=False,
                read_bufsize=64 * 1024,
                auto_decompress=False,
                headers={"accept": "application/json"},
            )
            self._warm_task = asyncio.create_task(
                self._keep_connections_warm(),
                name="e4-v12-transport-keepalive",
            )
        return self._http_session

    def _headers_for(self, name: str) -> dict[str, str]:
        base_name = str(name).split("#", 1)[0]
        headers = {"content-type": "application/json", "accept": "application/json", "connection": "keep-alive"}
        if base_name == "allenhark_relay":
            if self._relay_api_key:
                headers["x-api-key"] = self._relay_api_key
            return headers
        try:
            inherited = super()._headers(base_name)
            if isinstance(inherited, Mapping):
                headers.update({str(key): str(value) for key, value in inherited.items()})
        except Exception:
            pass
        return headers

    @staticmethod
    def _origin_keepalive(url: str) -> str:
        parsed = urlsplit(str(url))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        return urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))

    async def warm(self) -> None:
        session = self._session()
        targets = []
        for name, url in self.routes:
            base_name = str(name).split("#", 1)[0]
            target = self._relay_keepalive_url if base_name == "allenhark_relay" and self._relay_keepalive_url else self._origin_keepalive(url)
            if target:
                targets.append((name, target))
        async def one(name: str, target: str) -> None:
            try:
                async with session.get(target, headers=self._headers_for(name)) as response:
                    await response.read()
            except Exception as exc:
                LOGGER.debug("E4 V12 transport warm-up failed route=%s error=%s", name, exc)
        await asyncio.gather(*(one(name, target) for name, target in targets), return_exceptions=True)

    async def _keep_connections_warm(self) -> None:
        interval = max(3.0, _finite(os.getenv("E4_ROUTE_KEEPALIVE_INTERVAL_SECONDS"), 7.5))
        try:
            while not self._closed:
                await self.warm()
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("E4 V12 keepalive loop failed")

    async def _send(
        self,
        index: int,
        name: str,
        url: str,
        tx: str,
        expected_signature: str,
    ):
        # Parent implementations historically staggered by route index. V12
        # intentionally ignores it so every warmed route is written at once.
        del index
        started_ns = time.perf_counter_ns()
        base_name = str(name).split("#", 1)[0]
        try:
            session = self._session()
            if base_name == "allenhark_relay":
                payload: Mapping[str, Any] = {"tx": tx, "simulate": False}
            else:
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "sendTransaction",
                    "params": [
                        tx,
                        {
                            "encoding": "base64",
                            "skipPreflight": True,
                            "maxRetries": 0,
                            "preflightCommitment": "processed",
                        },
                    ],
                }
            async with session.post(url, json=payload, headers=self._headers_for(name)) as response:
                text = await response.text()
                response_ns = time.perf_counter_ns()
                if response.status >= 400:
                    raise RuntimeError(f"HTTP {response.status}: {text[:300]}")
                body = json.loads(text) if text else {}
                if isinstance(body, Mapping) and body.get("error"):
                    raise RuntimeError(str(body["error"]))
                if base_name == "allenhark_relay":
                    state = str((body or {}).get("status") or "accepted").lower() if isinstance(body, Mapping) else "accepted"
                    if state in {"rejected", "error", "failed"}:
                        raise RuntimeError(str((body or {}).get("error") or state))
                    returned = str((body or {}).get("signature") or (body or {}).get("result") or expected_signature) if isinstance(body, Mapping) else expected_signature
                else:
                    returned = str((body or {}).get("result") or expected_signature) if isinstance(body, Mapping) else expected_signature
                if returned and expected_signature and returned != expected_signature and len(returned) >= 64:
                    raise RuntimeError(f"route signature mismatch expected={expected_signature} got={returned}")
                self._record(DispatchTelemetry(str(name), started_ns, response_ns, True, "accepted"))
                return core.RouteResult(name, started_ns, response_ns, True, returned or expected_signature)
        except Exception as exc:
            response_ns = time.perf_counter_ns()
            self._record(DispatchTelemetry(str(name), started_ns, response_ns, False, str(exc)))
            return core.RouteResult(name, started_ns, response_ns, False, "rejected", str(exc))

    async def close(self) -> None:
        self._closed = True
        if self._warm_task is not None:
            self._warm_task.cancel()
            await asyncio.gather(self._warm_task, return_exceptions=True)
            self._warm_task = None
        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None


core.RouteSender = WarmFanoutRouteSender

_PREVIOUS_ENGINE_RUN = core.Engine.run


async def _run_with_transport_cleanup(self: Any) -> None:
    try:
        await _PREVIOUS_ENGINE_RUN(self)
    finally:
        close = getattr(getattr(self, "sender", None), "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result


core.Engine.run = _run_with_transport_cleanup

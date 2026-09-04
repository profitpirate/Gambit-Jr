from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import time
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from . import e4_direct_copy_v12 as direct
from . import e4_role_model_v12 as role_model

core = role_model.core
v6 = role_model.v6
PIPELINES = role_model.PIPELINES
LOGGER = logging.getLogger("gambit.e4.copy-fidelity.v12")

DIRECT_COPY_FAMILIES = {
    role_model.ROLE_MODEL_FAMILY,
    role_model.LEGACY_ROLE_MODEL_FAMILY,
}
_DEFAULT_SOURCE_STALE_MS = 65_000.0


def policy_fingerprint() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def assert_policy_fingerprint(expected: str) -> None:
    expected = str(expected or "").strip().lower()
    actual = policy_fingerprint()
    if not expected or expected != actual:
        raise RuntimeError(
            "E4 V12 copy-fidelity policy fingerprint mismatch "
            f"expected={expected or '<missing>'} actual={actual}"
        )


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_PREVIOUS_OBSERVE_E4_EXIT = type(PIPELINES).observe_e4_exit


def _observe_e4_exit_copy_fidelity_v12(
    self: Any,
    mint: str,
    *,
    token_amount: float = 0.0,
    sell_fraction: float = 0.0,
    fully_exited: bool = False,
    observed_ns: int | None = None,
    signature: str = "",
):
    key = str(mint)
    existing = self._e4_entries.get(key)
    if existing is None:
        return None
    if signature and existing.last_sell_signature == signature:
        return existing

    before = max(0.0, _finite(existing.remaining_tokens or existing.entry_tokens))
    sold_tokens = max(0.0, _finite(token_amount))
    fraction = min(1.0, max(0.0, _finite(sell_fraction)))
    token_accounted = sold_tokens > 0 and before > 0

    if fraction <= 0 and token_accounted:
        fraction = min(1.0, sold_tokens / before)
    if fraction <= 0 and not fully_exited:
        fraction = 0.30 if existing.sell_count == 0 else 1.0

    remaining = (
        max(0.0, before - sold_tokens)
        if sold_tokens > 0
        else before * (1.0 - fraction)
    )
    if token_accounted:
        complete = bool(fully_exited or sold_tokens >= before * 0.995)
    else:
        complete = bool(fully_exited or fraction >= 0.985)
    if existing.entry_tokens > 0 and remaining <= existing.entry_tokens * 1e-6:
        complete = True

    updated = replace(
        existing,
        remaining_tokens=0.0 if complete else remaining,
        last_sell_fraction=fraction,
        last_sell_ns=int(observed_ns or time.time_ns()),
        last_sell_signature=signature,
        sell_count=existing.sell_count + 1,
        fully_exited=complete,
        sold=True,
    )
    lock = getattr(self, "_lock", None)
    if lock is None:
        current = dict(self._e4_entries)
        current[key] = updated
        self._e4_entries = MappingProxyType(current)
    else:
        with lock:
            current = dict(self._e4_entries)
            current[key] = updated
            self._e4_entries = MappingProxyType(current)
    return updated


type(PIPELINES).observe_e4_exit = _observe_e4_exit_copy_fidelity_v12


_PREVIOUS_EXIT = core.E4Policy.exit


def _exit_copy_fidelity_v12(self: Any, position: Any, state: Any):
    profile = v6._PROFILE_BY_MINT.get(str(position.mint))
    family = str(getattr(profile, "family", "") or "")
    if family not in DIRECT_COPY_FAMILIES:
        return _PREVIOUS_EXIT(self, position, state)

    source = PIPELINES.e4_signal(position.mint)
    if source is None:
        return "SELL_ALL", 1.0, "E4 V12 direct-copy source missing fail-safe"

    if bool(getattr(source, "fully_exited", False)):
        return "SELL_ALL", 1.0, "E4 V12 direct-copy source fully exited"

    entry_tokens = max(0.0, _finite(getattr(source, "entry_tokens", 0.0)))
    remaining_source = max(0.0, _finite(getattr(source, "remaining_tokens", 0.0)))
    if entry_tokens > 0:
        target_sold = min(1.0, max(0.0, 1.0 - remaining_source / entry_tokens))
        original_tokens = max(0.0, _finite(getattr(position, "tokens", 0.0)))
        remaining_tokens = max(0.0, _finite(getattr(position, "remaining", 0.0)))
        gambit_sold = (
            min(1.0, max(0.0, 1.0 - remaining_tokens / original_tokens))
            if original_tokens > 0
            else 0.0
        )
        if target_sold >= 0.985:
            return "SELL_ALL", 1.0, "E4 V12 direct-copy cumulative full exit"
        additional_original = max(0.0, target_sold - gambit_sold)
        remaining_original = max(1e-12, 1.0 - gambit_sold)
        fraction_of_remaining = min(1.0, additional_original / remaining_original)
        if fraction_of_remaining >= 0.01:
            return (
                "SELL_PARTIAL",
                fraction_of_remaining,
                "E4 V12 direct-copy cumulative sell mirror "
                f"target={target_sold:.2%} gambit={gambit_sold:.2%}",
            )

    latest_source_ns = max(
        _integer(getattr(source, "observed_ns", 0)),
        _integer(getattr(source, "last_sell_ns", 0)),
    )
    now_ns = _integer(getattr(state, "latest_ns", 0)) or time.time_ns()
    stale_ms = max(
        _DEFAULT_SOURCE_STALE_MS,
        _finite(os.getenv("E4_DIRECT_COPY_SOURCE_STALE_MS"), _DEFAULT_SOURCE_STALE_MS),
    )
    if latest_source_ns <= 0 or max(0.0, (now_ns - latest_source_ns) / 1e6) >= stale_ms:
        return "SELL_ALL", 1.0, "E4 V12 direct-copy source stale fail-safe"

    return "HOLD", 0.0, "E4 V12 direct-copy awaiting source exit"


core.E4Policy.exit = _exit_copy_fidelity_v12


_PREVIOUS_ROUTE_SENDER = core.RouteSender


class FastPersistentRouteSender(_PREVIOUS_ROUTE_SENDER):
    def __init__(self, settings: Any, rpc: Any) -> None:
        super().__init__(settings, rpc)
        self._http_session: aiohttp.ClientSession | None = None
        self._keepalive_task: asyncio.Task[Any] | None = None
        self._relay_url = os.getenv("E4_ALLENHARK_RELAY_URL", "").strip()
        self._relay_api_key = os.getenv("E4_ALLENHARK_API_KEY", "").strip()
        self._relay_keepalive_url = os.getenv(
            "E4_ALLENHARK_KEEPALIVE_URL",
            self._default_keepalive_url(self._relay_url),
        ).strip()
        if self._relay_url and all(name != "allenhark_relay" for name, _ in self.routes):
            self.routes.insert(0, ("allenhark_relay", self._relay_url))

    @staticmethod
    def _default_keepalive_url(relay_url: str) -> str:
        if not relay_url:
            return ""
        parsed = urlsplit(relay_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        return urlunsplit((parsed.scheme, parsed.netloc, "/keepalive", "", ""))

    def _session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            timeout = aiohttp.ClientTimeout(total=1.5, sock_connect=0.75, sock_read=1.25)
            connector = aiohttp.TCPConnector(
                limit=64,
                ttl_dns_cache=600,
                keepalive_timeout=60,
                enable_cleanup_closed=True,
            )
            self._http_session = aiohttp.ClientSession(timeout=timeout, connector=connector)
            if self._relay_keepalive_url and self._keepalive_task is None:
                self._keepalive_task = asyncio.create_task(
                    self._keepalive_loop(),
                    name="e4-v12-relay-keepalive",
                )
        return self._http_session

    def _headers_for(self, name: str) -> dict[str, str]:
        base_name = name.split("#", 1)[0]
        headers = {"content-type": "application/json", "accept": "application/json"}
        if base_name == "allenhark_relay":
            if self._relay_api_key:
                headers["x-api-key"] = self._relay_api_key
            return headers
        try:
            headers.update(super()._headers(base_name))
        except Exception:
            pass
        return headers

    async def _keepalive_loop(self) -> None:
        while self._http_session is not None and not self._http_session.closed:
            try:
                async with self._http_session.get(self._relay_keepalive_url) as response:
                    await response.read()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.debug("E4 V12 relay keepalive failed: %s", exc)
            await asyncio.sleep(7.5)

    async def _send(
        self,
        index: int,
        name: str,
        url: str,
        tx: str,
        expected_signature: str,
    ):
        del index
        started = time.time_ns()
        base_name = name.split("#", 1)[0]
        try:
            session = self._session()
            if base_name == "allenhark_relay":
                payload = {"tx": tx, "simulate": False}
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
                        },
                    ],
                }
            async with session.post(
                url,
                json=payload,
                headers=self._headers_for(name),
            ) as response:
                text = await response.text()
                if response.status >= 400:
                    raise RuntimeError(f"HTTP {response.status}: {text[:300]}")
                body = json.loads(text) if text else {}
                if isinstance(body, dict) and body.get("error"):
                    raise RuntimeError(str(body["error"]))
                if base_name == "allenhark_relay":
                    status = str((body or {}).get("status") or "accepted").lower()
                    if status in {"rejected", "error", "failed"}:
                        raise RuntimeError(str((body or {}).get("error") or status))
                    returned = str(
                        (body or {}).get("signature")
                        or (body or {}).get("result")
                        or expected_signature
                    )
                else:
                    returned = str((body or {}).get("result") or expected_signature)
                if returned and expected_signature and returned != expected_signature:
                    if len(returned) >= 64:
                        raise RuntimeError(
                            f"route signature mismatch expected={expected_signature} got={returned}"
                        )
                return core.RouteResult(
                    name,
                    started,
                    time.time_ns(),
                    True,
                    returned or expected_signature,
                )
        except Exception as exc:
            return core.RouteResult(
                name,
                started,
                time.time_ns(),
                False,
                "rejected",
                str(exc),
            )

    async def close(self) -> None:
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            await asyncio.gather(self._keepalive_task, return_exceptions=True)
            self._keepalive_task = None
        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None


core.RouteSender = FastPersistentRouteSender

_PREVIOUS_ENGINE_RUN = core.Engine.run


async def _run_with_fast_sender_cleanup(self: Any) -> None:
    try:
        await _PREVIOUS_ENGINE_RUN(self)
    finally:
        close = getattr(getattr(self, "sender", None), "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result


core.Engine.run = _run_with_fast_sender_cleanup

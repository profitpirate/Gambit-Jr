from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from dataclasses import replace
from types import MappingProxyType
from typing import Any

import aiohttp

from . import e4_direct_copy_v12 as direct
from . import e4_role_model_v12 as role_model

core = role_model.core
v6 = role_model.v6
PIPELINES = role_model.PIPELINES
LOGGER = logging.getLogger("gambit.e4.sub10ms-repairs.v12")

_DEFAULT_MAX_OUTPUT_SHORTFALL_BPS = 600
_DEFAULT_SOURCE_STALE_MS = 65_000.0
DIRECT_COPY_FAMILIES = {
    role_model.ROLE_MODEL_FAMILY,
    role_model.LEGACY_ROLE_MODEL_FAMILY,
}


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


def max_output_shortfall_bps() -> int:
    """Maximum deterioration accepted by the V12 buy instruction.

    Pump's BuyExactSolIn instruction protects a minimum token quantity.  The
    old direct-copy path used 9,000 bps, which deliberately shrank that minimum
    and forced fills after E4 had moved the curve.  V12 now defaults to a 6%
    output floor and permits research overrides only inside a bounded range.
    """
    raw = os.getenv(
        "E4_DIRECT_COPY_MAX_OUTPUT_SHORTFALL_BPS",
        str(_DEFAULT_MAX_OUTPUT_SHORTFALL_BPS),
    )
    try:
        requested = int(raw)
    except (TypeError, ValueError):
        requested = _DEFAULT_MAX_OUTPUT_SHORTFALL_BPS
    return min(1_200, max(50, requested))


def _tight_direct_copy_slippage_bps(_settings: Any) -> int:
    return max_output_shortfall_bps()


# The direct-copy executor resolves this function at call time, so replacing it
# changes both the local Pump quote and the remote fallback without duplicating
# the order lifecycle.
direct.direct_copy_slippage_bps = _tight_direct_copy_slippage_bps


# Preserve real multi-leg E4 exits.  The inherited manager could mark a second
# token-accounted sell as complete merely because it was >=50% of the remaining
# balance, collapsing legitimate third/fourth legs.  Exact token deltas are now
# authoritative; heuristics remain only for token-less notifications.
def _observe_e4_exit_exact(
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


type(PIPELINES).observe_e4_exit = _observe_e4_exit_exact


_PREVIOUS_EXIT = core.E4Policy.exit


def _exit_exact_source_mirror(self: Any, position: Any, state: Any):
    profile = v6._PROFILE_BY_MINT.get(str(position.mint))
    family = str(getattr(profile, "family", "") or "")
    if family not in DIRECT_COPY_FAMILIES:
        return _PREVIOUS_EXIT(self, position, state)

    source = PIPELINES.e4_signal(position.mint)
    if source is None:
        return "SELL_ALL", 1.0, "E4 V12 source missing fail-safe"
    if bool(getattr(source, "fully_exited", False)):
        return "SELL_ALL", 1.0, "E4 V12 source fully exited"

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
            return "SELL_ALL", 1.0, "E4 V12 cumulative full exit"
        additional_original = max(0.0, target_sold - gambit_sold)
        remaining_original = max(1e-12, 1.0 - gambit_sold)
        fraction_of_remaining = min(1.0, additional_original / remaining_original)
        if fraction_of_remaining >= 0.01:
            return (
                "SELL_PARTIAL",
                fraction_of_remaining,
                "E4 V12 cumulative sell mirror "
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
        return "SELL_ALL", 1.0, "E4 V12 source stale fail-safe"
    return "HOLD", 0.0, "E4 V12 awaiting source exit"


core.E4Policy.exit = _exit_exact_source_mirror


# Remove route staggering and keep HTTP/TLS connections warm.  This changes the
# measurable internal target to signal-received -> all route requests dispatched;
# Solana leader/network landing time is recorded separately and is never claimed
# to be controllable below 10 ms.
_PREVIOUS_ROUTE_SENDER = core.RouteSender


class FastPersistentRouteSender(_PREVIOUS_ROUTE_SENDER):
    def __init__(self, settings: Any, rpc: Any) -> None:
        super().__init__(settings, rpc)
        self._http_session: aiohttp.ClientSession | None = None

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
        return self._http_session

    async def warm(self) -> None:
        session = self._session()
        async def one(name: str, url: str) -> None:
            try:
                base_name = name.split("#", 1)[0]
                headers = {"content-type": "application/json", "accept": "application/json"}
                try:
                    headers.update(super(FastPersistentRouteSender, self)._headers(base_name))
                except Exception:
                    pass
                payload = {"jsonrpc": "2.0", "id": 0, "method": "getHealth", "params": []}
                async with session.post(url, json=payload, headers=headers) as response:
                    await response.read()
            except Exception:
                return
        await asyncio.gather(*(one(name, url) for name, url in self.routes), return_exceptions=True)

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
            headers = {"content-type": "application/json", "accept": "application/json"}
            try:
                headers.update(super()._headers(base_name))
            except Exception:
                pass
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
            async with self._session().post(url, json=payload, headers=headers) as response:
                text = await response.text()
                if response.status >= 400:
                    raise RuntimeError(f"HTTP {response.status}: {text[:300]}")
                body = json.loads(text) if text else {}
                if isinstance(body, dict) and body.get("error"):
                    raise RuntimeError(str(body["error"]))
                returned = str((body or {}).get("result") or expected_signature)
                return core.RouteResult(
                    name,
                    started,
                    time.time_ns(),
                    True,
                    returned,
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
        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None


core.RouteSender = FastPersistentRouteSender


# Start both persistent builders and warm every route before consuming live
# events.  The first real trade must not pay Node startup, DNS or TLS setup.
_PREVIOUS_ENGINE_RUN = core.Engine.run


async def _run_prewarmed(self: Any) -> None:
    workers = list(getattr(getattr(self, "builder", None), "workers", ()) or ())
    sender = getattr(self, "sender", None)
    try:
        await asyncio.gather(
            *(worker.start() for worker in workers if hasattr(worker, "start")),
            sender.warm() if sender is not None and hasattr(sender, "warm") else asyncio.sleep(0),
            return_exceptions=True,
        )
        await _PREVIOUS_ENGINE_RUN(self)
    finally:
        close = getattr(sender, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result


core.Engine.run = _run_prewarmed

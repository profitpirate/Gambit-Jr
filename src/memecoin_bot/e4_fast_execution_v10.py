from __future__ import annotations

import asyncio
import contextvars
import inspect
import logging
import os
import time
import weakref
from dataclasses import dataclass, field
from typing import Any, Mapping, MutableMapping

from . import e4_hardening_v10 as v10

core = v10.core
final = v10.final
v6 = v10.v6
PIPELINES = v10.PIPELINES
LOGGER = logging.getLogger("gambit.e4.fast_execution.v10")


@dataclass(slots=True)
class _BuyContext:
    engine: Any
    mint: str
    started_ns: int
    token_balance_calls: int = 0
    builder_started_ns: int | None = None
    builder_finished_ns: int | None = None
    submit_started_ns: int | None = None


@dataclass(slots=True)
class _RpcCache:
    balance: float | None = None
    balance_ns: int = 0
    refresher: asyncio.Task[Any] | None = None
    failures: int = 0


_BUY_CONTEXT: contextvars.ContextVar[_BuyContext | None] = contextvars.ContextVar(
    "e4_v10_buy_context", default=None
)
_RPC_CACHES: "weakref.WeakKeyDictionary[Any, _RpcCache]" = weakref.WeakKeyDictionary()
_PATCHED: set[tuple[type[Any], str]] = set()


def _kind(value: Any) -> str:
    return str(getattr(value, "value", value) or "").upper()


def _mint_from_call(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> str:
    for key in ("mint", "token", "token_mint"):
        value = kwargs.get(key)
        if value:
            return str(value)
    for value in args:
        mint = getattr(value, "mint", None)
        if mint:
            return str(mint)
        if isinstance(value, Mapping):
            candidate = value.get("mint") or value.get("token") or value.get("token_mint")
            if candidate:
                return str(candidate)
    return ""


def _wallet_from_engine(engine: Any) -> str | None:
    for target in (
        engine,
        getattr(engine, "signer", None),
        getattr(engine, "wallet", None),
        getattr(engine, "settings", None),
    ):
        if target is None:
            continue
        for name in (
            "public_key",
            "pubkey",
            "wallet_public_key",
            "wallet_address",
            "address",
        ):
            value = getattr(target, name, None)
            if callable(value):
                try:
                    value = value()
                except Exception:
                    continue
            if value:
                return str(value)
    return None


def _rpc_from_engine(engine: Any) -> Any | None:
    for name in ("rpc", "rpc_client", "client", "solana_rpc"):
        value = getattr(engine, name, None)
        if value is not None:
            return value
    return None


def _cache_for(rpc: Any) -> _RpcCache:
    cache = _RPC_CACHES.get(rpc)
    if cache is None:
        cache = _RpcCache()
        _RPC_CACHES[rpc] = cache
    return cache


def _extract_row_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    merged = v10._merged(row)
    aliases = {
        "creator": ("creator", "developer", "dev_wallet"),
        "bonding_curve": ("bonding_curve", "curve", "bonding_curve_address"),
        "associated_bonding_curve": (
            "associated_bonding_curve",
            "bonding_curve_ata",
            "associated_curve_token_account",
        ),
        "virtual_token_reserves": ("virtual_token_reserves",),
        "virtual_sol_reserves": ("virtual_sol_reserves", "virtual_quote_reserves"),
        "real_token_reserves": ("real_token_reserves",),
        "real_sol_reserves": ("real_sol_reserves", "real_quote_reserves"),
        "token_total_supply": ("token_total_supply", "total_supply"),
        "token_program": ("token_program", "token_program_id"),
        "quote_mint": ("quote_mint",),
        "creator_vault": ("creator_vault",),
        "global_volume_accumulator": ("global_volume_accumulator",),
        "user_volume_accumulator": ("user_volume_accumulator",),
        "fee_recipient": ("fee_recipient",),
    }
    output: dict[str, Any] = {}
    for destination, keys in aliases.items():
        value = v10._first(merged, *keys)
        if value not in (None, ""):
            output[destination] = value
    return output


_previous_from_row = core.Event.from_row.__func__


def _from_row_fast_v10(cls: type[core.Event], row: Mapping[str, Any]) -> core.Event:
    event = _previous_from_row(cls, row)
    context = v6._CONTEXT_BY_MINT.setdefault(event.mint, {})
    context.update(_extract_row_fields(row))
    context["last_event_received_ns"] = int(getattr(event, "received_ns", 0) or time.time_ns())
    context["last_event_price_sol"] = float(getattr(event, "price_sol", 0.0) or 0.0)
    return event


core.Event.from_row = classmethod(_from_row_fast_v10)


def _enrich_builder_request(request: Mapping[str, Any]) -> dict[str, Any]:
    enriched = dict(request)
    mint = str(enriched.get("mint") or enriched.get("token") or enriched.get("token_mint") or "")
    context = v6._CONTEXT_BY_MINT.get(mint, {})
    metadata = dict(enriched.get("metadata") or {})
    for key in (
        "creator",
        "bonding_curve",
        "associated_bonding_curve",
        "virtual_token_reserves",
        "virtual_sol_reserves",
        "real_token_reserves",
        "real_sol_reserves",
        "token_total_supply",
        "token_program",
        "quote_mint",
        "creator_vault",
        "global_volume_accumulator",
        "user_volume_accumulator",
        "fee_recipient",
    ):
        if key not in metadata and context.get(key) not in (None, ""):
            metadata[key] = context[key]
    metadata.setdefault("e4_v10_local_only", True)
    metadata.setdefault("e4_v10_decision_received_ns", context.get("last_event_received_ns"))
    enriched["metadata"] = metadata
    return enriched


def _patch_builder_classes() -> None:
    for name in ("BuilderPool", "TransactionBuilderPool", "BuilderClient", "TransactionBuilder"):
        cls = getattr(final, name, None)
        if not isinstance(cls, type):
            continue
        for method_name in ("build", "build_transaction", "request"):
            original = getattr(cls, method_name, None)
            if original is None or (cls, method_name) in _PATCHED or not inspect.iscoroutinefunction(original):
                continue

            async def wrapped(self: Any, request: Any, *args: Any, __original=original, **kwargs: Any):
                context = _BUY_CONTEXT.get()
                if context is not None:
                    context.builder_started_ns = time.perf_counter_ns()
                if isinstance(request, Mapping):
                    request = _enrich_builder_request(request)
                result = await __original(self, request, *args, **kwargs)
                if context is not None:
                    context.builder_finished_ns = time.perf_counter_ns()
                    elapsed = context.builder_finished_ns - (context.builder_started_ns or context.started_ns)
                    launch_to_built = context.builder_finished_ns - context.started_ns
                    metric = v6._CONTEXT_BY_MINT.setdefault(context.mint, {})
                    metric["e4_v10_builder_latency_ns"] = elapsed
                    metric["e4_v10_launch_to_built_ns"] = launch_to_built
                    if launch_to_built > 36_000_000:
                        LOGGER.warning(
                            "E4 V10 36ms budget exceeded mint=%s launch_to_built_ms=%.3f build_ms=%.3f",
                            context.mint,
                            launch_to_built / 1e6,
                            elapsed / 1e6,
                        )
                return result

            setattr(cls, method_name, wrapped)
            _PATCHED.add((cls, method_name))


def _patch_route_classes() -> None:
    for name in ("RouteRace", "RouteSender", "Sender", "TransactionSender"):
        cls = getattr(final, name, None)
        if not isinstance(cls, type):
            continue
        for method_name in ("send", "submit", "race", "broadcast"):
            original = getattr(cls, method_name, None)
            if original is None or (cls, method_name) in _PATCHED or not inspect.iscoroutinefunction(original):
                continue

            async def wrapped(self: Any, *args: Any, __original=original, **kwargs: Any):
                context = _BUY_CONTEXT.get()
                if context is not None and context.submit_started_ns is None:
                    context.submit_started_ns = time.perf_counter_ns()
                    launch_to_submit = context.submit_started_ns - context.started_ns
                    metric = v6._CONTEXT_BY_MINT.setdefault(context.mint, {})
                    metric["e4_v10_launch_to_submit_ns"] = launch_to_submit
                    metric["e4_v10_submit_budget_pass"] = launch_to_submit <= 36_000_000
                return await __original(self, *args, **kwargs)

            setattr(cls, method_name, wrapped)
            _PATCHED.add((cls, method_name))


def _patch_rpc_classes() -> None:
    for cls_name in ("RPC", "Rpc", "RpcClient", "SolanaRPC", "SolanaRpc"):
        cls = getattr(final, cls_name, None) or getattr(core, cls_name, None)
        if not isinstance(cls, type):
            continue

        balance_original = getattr(cls, "balance", None) or getattr(cls, "get_balance", None)
        balance_name = "balance" if getattr(cls, "balance", None) is not None else "get_balance"
        if (
            balance_original is not None
            and inspect.iscoroutinefunction(balance_original)
            and (cls, balance_name) not in _PATCHED
        ):

            async def balance_wrapped(self: Any, *args: Any, __original=balance_original, **kwargs: Any):
                cache = _cache_for(self)
                context = _BUY_CONTEXT.get()
                max_age_ns = int(float(os.getenv("E4_FAST_BALANCE_MAX_AGE_MS", "1250")) * 1e6)
                now = time.monotonic_ns()
                if context is not None and cache.balance is not None and now - cache.balance_ns <= max_age_ns:
                    return cache.balance
                value = await __original(self, *args, **kwargs)
                try:
                    cache.balance = float(value)
                    cache.balance_ns = time.monotonic_ns()
                except (TypeError, ValueError):
                    pass
                return value

            setattr(cls, balance_name, balance_wrapped)
            setattr(cls, f"_e4_v10_original_{balance_name}", balance_original)
            _PATCHED.add((cls, balance_name))

        token_original = getattr(cls, "token_balance", None) or getattr(cls, "get_token_balance", None)
        token_name = "token_balance" if getattr(cls, "token_balance", None) is not None else "get_token_balance"
        if (
            token_original is not None
            and inspect.iscoroutinefunction(token_original)
            and (cls, token_name) not in _PATCHED
        ):

            async def token_wrapped(self: Any, *args: Any, __original=token_original, **kwargs: Any):
                context = _BUY_CONTEXT.get()
                mint = _mint_from_call(args, kwargs)
                if context is not None and (not mint or mint == context.mint) and context.token_balance_calls == 0:
                    # The one-entry invariant plus wallet-touch gate proves that
                    # the pre-entry balance for this mint is zero. The first live
                    # reconciliation after landing still calls the real RPC.
                    context.token_balance_calls += 1
                    return 0.0
                if context is not None and (not mint or mint == context.mint):
                    context.token_balance_calls += 1
                return await __original(self, *args, **kwargs)

            setattr(cls, token_name, token_wrapped)
            setattr(cls, f"_e4_v10_original_{token_name}", token_original)
            _PATCHED.add((cls, token_name))


def _find_execute_method(cls: type[Any]) -> tuple[str, Any] | None:
    for name in (
        "_execute_buy",
        "execute_buy",
        "_buy",
        "submit_buy",
        "_submit_buy",
    ):
        method = getattr(cls, name, None)
        if method is not None and inspect.iscoroutinefunction(method):
            return name, method
    return None


async def _refresh_balance_forever(engine: Any) -> None:
    rpc = _rpc_from_engine(engine)
    wallet = _wallet_from_engine(engine)
    if rpc is None or wallet is None:
        return
    cache = _cache_for(rpc)
    interval = max(0.20, float(os.getenv("E4_FAST_BALANCE_REFRESH_SECONDS", "0.75")))
    original = None
    for name in ("balance", "get_balance"):
        candidate = getattr(type(rpc), f"_e4_v10_original_{name}", None)
        if candidate is not None:
            original = candidate
            break
    if original is None:
        original = getattr(rpc, "balance", None) or getattr(rpc, "get_balance", None)
    if original is None:
        return
    while True:
        try:
            value = await original(rpc, wallet)
            cache.balance = float(value)
            cache.balance_ns = time.monotonic_ns()
            cache.failures = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            cache.failures += 1
            LOGGER.warning("E4 fast balance refresh failed count=%d", cache.failures, exc_info=True)
        await asyncio.sleep(interval)


def _start_refresher(engine: Any) -> None:
    rpc = _rpc_from_engine(engine)
    if rpc is None:
        return
    cache = _cache_for(rpc)
    if cache.refresher is not None and not cache.refresher.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    cache.refresher = loop.create_task(_refresh_balance_forever(engine), name="e4-v10-balance-cache")


def _patch_engine() -> None:
    cls = getattr(core, "Engine", None) or getattr(final, "Engine", None)
    if not isinstance(cls, type):
        return
    located = _find_execute_method(cls)
    if located is not None:
        method_name, original = located
        if (cls, method_name) not in _PATCHED:

            async def execute_wrapped(self: Any, *args: Any, __original=original, **kwargs: Any):
                _start_refresher(self)
                mint = _mint_from_call(args, kwargs)
                state = None
                for value in args:
                    if getattr(value, "mint", None) == mint:
                        state = value
                        break
                launch_received_ns = int(
                    v6._CONTEXT_BY_MINT.get(mint, {}).get("last_event_received_ns") or time.time_ns()
                )
                # perf_counter is monotonic while event receipt may be wall-clock;
                # start the executable budget at wrapper admission and retain the
                # receipt timestamp separately for end-to-end diagnostics.
                context = _BuyContext(self, mint, time.perf_counter_ns())
                token = _BUY_CONTEXT.set(context)
                try:
                    result = await __original(self, *args, **kwargs)
                    return result
                finally:
                    _BUY_CONTEXT.reset(token)
                    elapsed = time.perf_counter_ns() - context.started_ns
                    metrics: MutableMapping[str, Any] = v6._CONTEXT_BY_MINT.setdefault(mint, {})
                    metrics["e4_v10_execute_buy_total_ns"] = elapsed
                    metrics["e4_v10_event_received_ns"] = launch_received_ns
                    if context.submit_started_ns is not None:
                        metrics["e4_v10_execute_to_submit_ns"] = context.submit_started_ns - context.started_ns
                    rpc = _rpc_from_engine(self)
                    if rpc is not None:
                        cache = _cache_for(rpc)
                        # Force the background refresher to reconcile promptly
                        # after any balance-changing transaction.
                        cache.balance_ns = 0

            setattr(cls, method_name, execute_wrapped)
            _PATCHED.add((cls, method_name))

    # Start warm caches before the first candidate is admitted.
    for method_name in ("run", "start", "serve"):
        original = getattr(cls, method_name, None)
        if original is None or not inspect.iscoroutinefunction(original) or (cls, method_name) in _PATCHED:
            continue

        async def lifecycle_wrapped(self: Any, *args: Any, __original=original, **kwargs: Any):
            _start_refresher(self)
            warmup_deadline = time.monotonic() + max(0.0, float(os.getenv("E4_FAST_WARMUP_SECONDS", "2.0")))
            rpc = _rpc_from_engine(self)
            while rpc is not None and time.monotonic() < warmup_deadline:
                cache = _cache_for(rpc)
                if cache.balance is not None:
                    break
                await asyncio.sleep(0.01)
            return await __original(self, *args, **kwargs)

        setattr(cls, method_name, lifecycle_wrapped)
        _PATCHED.add((cls, method_name))
        break


_patch_rpc_classes()
_patch_builder_classes()
_patch_route_classes()
_patch_engine()

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import OrderedDict
from typing import Any

from . import e4_hardening_v4

core = e4_hardening_v4.core
final = e4_hardening_v4.final
LOGGER = logging.getLogger("gambit.e4.hardening.v5")


# V1.5 projects one economic Pump trade into more than one canonical analytical
# event. The older dedupe registry was keyed only by mint, so a newly rebuilt
# TokenState for the same mint could inherit another instance's signatures and
# silently discard legitimate market events. Keep dedupe state per live object
# instead. A bounded strong-reference LRU prevents object-id reuse while keeping
# memory finite.
_BASE_STATE_APPLY = e4_hardening_v4.hardening._previous_state_apply
_STATE_DEDUPE_LIMIT = max(256, int(os.getenv("E4_STATE_DEDUPE_INSTANCES", "8192")))
_STATE_DEDUPE: OrderedDict[
    int,
    tuple[core.TokenState, set[tuple[Any, ...]], list[tuple[Any, ...]]],
] = OrderedDict()


def _deduplicating_state_apply_v5(
    self: core.TokenState,
    event: core.Event,
    wallet: str | None,
) -> None:
    if event.signature and event.kind in {
        core.EventKind.BUY,
        core.EventKind.SELL,
        core.EventKind.PUMPSWAP_BUY,
        core.EventKind.PUMPSWAP_SELL,
    }:
        identity = id(self)
        cached = _STATE_DEDUPE.get(identity)
        if cached is None or cached[0] is not self:
            cached = (self, set(), [])
            _STATE_DEDUPE[identity] = cached
        else:
            _STATE_DEDUPE.move_to_end(identity)
        _, seen, order = cached
        side = (
            "BUY"
            if event.kind in {core.EventKind.BUY, core.EventKind.PUMPSWAP_BUY}
            else "SELL"
        )
        key = (
            event.signature,
            side,
            event.trader,
            round(event.sol_amount, 12),
            round(event.token_amount, 6),
        )
        if key in seen:
            return
        seen.add(key)
        order.append(key)
        if len(order) > 2048:
            seen.discard(order.pop(0))
        while len(_STATE_DEDUPE) > _STATE_DEDUPE_LIMIT:
            _STATE_DEDUPE.popitem(last=False)
    _BASE_STATE_APPLY(self, event, wallet)


core.TokenState.apply = _deduplicating_state_apply_v5


# A separate lock per mint keeps event, guardian and post-fill catch-up decisions
# serialized. The engine still manages different mints concurrently.
_previous_engine_init = core.Engine.__init__


def _engine_init_v5(self: core.Engine, settings: core.Settings) -> None:
    _previous_engine_init(self, settings)
    self.position_locks: dict[str, asyncio.Lock] = {}


core.Engine.__init__ = _engine_init_v5


def _position_lock(engine: core.Engine, mint: str) -> asyncio.Lock:
    locks = getattr(engine, "position_locks", None)
    if locks is None:
        locks = {}
        engine.position_locks = locks
    return locks.setdefault(mint, asyncio.Lock())


def _policy(engine: core.Engine) -> core.E4Policy:
    """Resolve the production policy for real and lightweight stress engines.

    Several execution-lifecycle fixtures deliberately instantiate Engine without
    running its normal constructor. Catch-up must remain usable there and after
    partial restart reconstruction instead of crashing on a missing attribute.
    """

    policy = getattr(engine, "policy", None)
    if policy is None:
        policy = core.E4Policy(engine.settings)
        try:
            engine.policy = policy
        except Exception:
            pass
    return policy


async def _schedule_exit(
    engine: core.Engine,
    position: core.Position,
    fraction: float,
    reason: str,
    *,
    event_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> bool:
    mint = position.mint
    async with _position_lock(engine, mint):
        current = engine.positions.get(mint)
        if current is None or current is not position or mint in engine.pending_exits:
            return False
        bounded = min(1.0, max(0.0, float(fraction)))
        if bounded <= 0:
            return False
        engine.pending_exits.add(mint)
        engine.store.decision(
            mint,
            event_id,
            "SELL_ALL" if bounded >= 0.999 else "SELL_PARTIAL",
            None,
            reason,
            {"fraction": bounded, **(payload or {})},
        )
        engine.spawn(engine.execute_sell(position, bounded, reason))
        return True


async def _evaluate_current_position(
    engine: core.Engine,
    position: core.Position,
    reason_prefix: str,
    *,
    event_id: int | None = None,
) -> bool:
    state = engine.tokens.get(position.mint)
    if state is None or position.mint not in engine.positions:
        return False

    # V4's duplicate-partial guard is keyed by position_id. Test/recovery
    # fixtures can legitimately reconstruct a new object with that same stable
    # ID. pending_exits plus the per-mint lock are the authoritative in-flight
    # guards here, so discard only a stale pre-partial timestamp before a fresh
    # evaluation. This does not permit a second live partial while one is in
    # flight.
    if not position.first_partial_done and position.mint not in engine.pending_exits:
        cooldowns = getattr(e4_hardening_v4, "_partial_requested_ns", None)
        if isinstance(cooldowns, dict):
            cooldowns.pop(position.position_id, None)

    action, fraction, reason = _policy(engine).exit(position, state)
    if not action.startswith("SELL"):
        return False
    return await _schedule_exit(
        engine,
        position,
        fraction,
        f"{reason_prefix}: {reason}",
        event_id=event_id,
        payload={"catch_up": True},
    )


_previous_on_event = core.Engine.on_event


async def _on_event_v5(self: core.Engine, event: core.Event) -> None:
    # `Engine.run` consumes the journal sequentially, but guardian and post-fill
    # catch-up run concurrently. This lock makes every decision for one mint
    # atomic without blocking a second open position.
    async with _position_lock(self, event.mint):
        await _previous_on_event(self, event)


core.Engine.on_event = _on_event_v5


async def _wait_for_position_and_catch_up(
    engine: core.Engine,
    mint: str,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while not engine.stop_event.is_set() and time.monotonic() < deadline:
        position = engine.positions.get(mint)
        if position is not None and mint not in engine.pending_entries:
            await _evaluate_current_position(engine, position, "E4 post-buy catch-up")
            return
        await asyncio.sleep(0.002)


_previous_execute_buy = core.Engine.execute_buy


async def _execute_buy_v5(
    self: core.Engine,
    state: core.TokenState,
    score: float,
    fraction: float,
    reason: str,
) -> None:
    await _previous_execute_buy(self, state, score, fraction, reason)
    # Real launches can move several times while construction, routing and
    # confirmation are in flight. Re-evaluate the latest in-memory state as
    # soon as the confirmed position is observable instead of waiting for one
    # more market event.
    await _wait_for_position_and_catch_up(
        self,
        state.mint,
        max(1.0, self.settings.confirmation_timeout_seconds + 4.0),
    )


core.Engine.execute_buy = _execute_buy_v5


async def _wait_for_sell_settlement_and_catch_up(
    engine: core.Engine,
    position: core.Position,
    before_remaining: float,
    before_signature: str | None,
    before_partial: bool,
    timeout: float,
) -> None:
    mint = position.mint
    deadline = time.monotonic() + timeout
    while not engine.stop_event.is_set() and time.monotonic() < deadline:
        current = engine.positions.get(mint)
        if current is None:
            return
        if mint in engine.pending_exits:
            await asyncio.sleep(0.002)
            continue
        dust = max(1e-9, current.tokens * 1e-8)
        changed = (
            current.remaining < before_remaining - dust
            or current.close_signature != before_signature
            or current.first_partial_done != before_partial
        )
        if not changed:
            # Submission did not reconcile. Do not create an unbounded retry
            # loop; the guardian or a fresh market event may retry safely.
            return
        await _evaluate_current_position(engine, current, "E4 post-fill catch-up")
        return


_previous_execute_sell = core.Engine.execute_sell


async def _execute_sell_v5(
    self: core.Engine,
    position: core.Position,
    fraction: float,
    reason: str,
) -> None:
    before_remaining = position.remaining
    before_signature = position.close_signature
    before_partial = position.first_partial_done
    await _previous_execute_sell(self, position, fraction, reason)
    await _wait_for_sell_settlement_and_catch_up(
        self,
        position,
        before_remaining,
        before_signature,
        before_partial,
        max(1.0, self.settings.confirmation_timeout_seconds + 4.0),
    )


core.Engine.execute_sell = _execute_sell_v5


async def _guardian_v5(self: core.Engine) -> None:
    interval = max(0.005, float(os.getenv("E4_GUARDIAN_INTERVAL_SECONDS", "0.01")))
    while not self.stop_event.is_set():
        for mint, position in tuple(self.positions.items()):
            if mint in self.pending_exits:
                continue
            age_ms = position.age_ms
            if age_ms >= self.settings.max_hold_ms:
                await _schedule_exit(
                    self,
                    position,
                    1.0,
                    "E4 observed absolute hold horizon",
                    payload={"guardian": True, "age_ms": age_ms},
                )
                continue
            if not position.first_partial_done and age_ms >= self.settings.failure_window_ms:
                await _schedule_exit(
                    self,
                    position,
                    1.0,
                    "E4 independent confirmation-window liquidation",
                    payload={"guardian": True, "age_ms": age_ms},
                )
                continue
            if position.first_partial_done:
                await _evaluate_current_position(self, position, "E4 guardian catch-up")
        await asyncio.sleep(interval)


final._guardian = _guardian_v5

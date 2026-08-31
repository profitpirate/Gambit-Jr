from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from . import e4_hardening_v3

core = e4_hardening_v3.core
hardening = e4_hardening_v3.hardening
final = hardening.final
LOGGER = logging.getLogger("gambit.e4.hardening.v4")


# ---------------------------------------------------------------------------
# Wall-clock position supervision
# ---------------------------------------------------------------------------
# TokenState.flow() intentionally uses the timestamp of the latest market event.
# That is correct while events are flowing, but it freezes the flow window when a
# token goes quiet. E4 positions live for seconds, so a quiet market must still
# age out even when no new token event arrives. This helper advances the window
# with the wall clock without mutating canonical market state.


def _wall_flow(state: core.TokenState, milliseconds: int, now_ns: int) -> core.Flow:
    result = core.Flow()
    cutoff = now_ns - milliseconds * 1_000_000
    for event in reversed(state.events):
        if event.source_ns < cutoff:
            break
        if event.kind in {core.EventKind.BUY, core.EventKind.PUMPSWAP_BUY}:
            result.buy_sol += max(0.0, event.sol_amount)
            if event.trader:
                result.buyers.add(event.trader)
        elif event.kind in {core.EventKind.SELL, core.EventKind.PUMPSWAP_SELL}:
            result.sell_sol += max(0.0, event.sol_amount)
            if event.trader:
                result.sellers.add(event.trader)
    return result


def _latest_trade_ns(state: core.TokenState) -> int | None:
    for event in reversed(state.events):
        if event.kind in {
            core.EventKind.BUY,
            core.EventKind.SELL,
            core.EventKind.PUMPSWAP_BUY,
            core.EventKind.PUMPSWAP_SELL,
        }:
            return event.source_ns
    return None


def _guardian_liquidation_reason(
    engine: core.Engine,
    position: core.Position,
    now_ns: int,
) -> str | None:
    age_ms = max(0.0, (now_ns - position.opened_ns) / 1_000_000)
    if age_ms >= engine.settings.max_hold_ms:
        return "E4 observed absolute hold horizon"

    state = engine.tokens.get(position.mint)
    if state is None:
        if age_ms >= engine.settings.failure_window_ms:
            return "E4 confirmation watchdog missing token state"
        return None

    price = state.price_sol or position.last_price
    if price and price > 0:
        position.last_price = price
        position.max_price = max(position.max_price, price)
        markout = position.markout_bps(price)
        if age_ms <= engine.settings.failure_window_ms:
            if markout <= engine.settings.failure_markout_bps:
                return "E4 watchdog fast adverse-markout failure"
            flow250 = _wall_flow(state, 250, now_ns)
            flow1s = _wall_flow(state, 1_000, now_ns)
            if (
                (flow250.net < 0 or flow1s.ratio < 0.85)
                and markout <= engine.settings.flow_break_markout_bps
            ):
                return "E4 watchdog fast flow-break failure"

    # The existing event path evaluates every market tick. This independent
    # timer exists for the opposite case: no tick arrives to wake the policy.
    if not position.first_partial_done and age_ms >= engine.settings.failure_window_ms:
        flow250 = _wall_flow(state, 250, now_ns)
        flow1s = _wall_flow(state, 1_000, now_ns)
        last_trade_ns = _latest_trade_ns(state)
        quiet_ms = float(os.getenv("E4_CONFIRMATION_QUIET_MS", "750"))
        quiet = (
            last_trade_ns is None
            or last_trade_ns < position.opened_ns
            or (now_ns - last_trade_ns) / 1_000_000 >= quiet_ms
        )
        broken = flow250.net < 0 or flow1s.ratio < 0.85
        no_confirming_capital = flow1s.buy_sol < max(
            0.0, engine.settings.minimum_sol_inflow_1s
        )
        if quiet or broken or no_confirming_capital:
            return "E4 independent confirmation watchdog failure"

    if position.first_partial_done and price and price > 0:
        flow250 = _wall_flow(state, 250, now_ns)
        flow1s = _wall_flow(state, 1_000, now_ns)
        broken = flow250.net < 0 or flow1s.ratio < 0.85
        if broken and position.drawdown_bps(price) >= 350:
            return "E4 independent runner flow-break liquidation"
        if position.drawdown_bps(price) >= engine.settings.runner_drawdown_bps:
            return "E4 independent runner peak-drawdown liquidation"

    return None


async def _guardian_v4(self: core.Engine) -> None:
    interval = max(0.005, float(os.getenv("E4_GUARDIAN_INTERVAL_SECONDS", "0.01")))
    retry_base_ms = max(25, int(os.getenv("E4_EXIT_RETRY_BASE_MS", "100")))
    retry_cap_ms = max(retry_base_ms, int(os.getenv("E4_EXIT_RETRY_CAP_MS", "2000")))
    retry_state: dict[str, tuple[int, int]] = {}

    while not self.stop_event.is_set():
        now_ns = time.time_ns()
        live_mints = set(self.positions)
        for mint in tuple(retry_state):
            if mint not in live_mints:
                retry_state.pop(mint, None)

        for mint, position in tuple(self.positions.items()):
            if mint in self.pending_exits:
                continue
            attempts, retry_after_ns = retry_state.get(mint, (0, 0))
            if now_ns < retry_after_ns:
                continue
            reason = _guardian_liquidation_reason(self, position, now_ns)
            if reason is None:
                continue

            attempts += 1
            delay_ms = min(retry_cap_ms, retry_base_ms * (2 ** min(5, attempts - 1)))
            retry_state[mint] = (attempts, now_ns + delay_ms * 1_000_000)
            self.pending_exits.add(mint)
            self.store.decision(
                mint,
                None,
                "SELL_ALL",
                None,
                reason,
                {
                    "fraction": 1.0,
                    "guardian": True,
                    "guardian_version": "v4",
                    "attempt": attempts,
                    "retry_delay_ms": delay_ms,
                },
            )
            self.spawn(self.execute_sell(position, 1.0, reason))

        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


# _final_run resolves this module global when the engine starts, so replacing it
# here upgrades the production runtime without starting a second competing
# guardian task.
final._guardian = _guardian_v4


# ---------------------------------------------------------------------------
# Rapid-market duplicate partial protection
# ---------------------------------------------------------------------------

_previous_exit = core.E4Policy.exit
_partial_requested_ns: dict[str, int] = {}


def _exit_with_partial_cooldown(
    self: core.E4Policy,
    position: core.Position,
    state: core.TokenState,
) -> tuple[str, float, str]:
    action, fraction, reason = _previous_exit(self, position, state)
    if action != "SELL_PARTIAL":
        return action, fraction, reason

    now_ns = time.time_ns()
    cooldown_ms = max(25, int(os.getenv("E4_PARTIAL_COOLDOWN_MS", "200")))
    previous_ns = _partial_requested_ns.get(position.position_id, 0)
    if now_ns - previous_ns < cooldown_ms * 1_000_000:
        return "HOLD", 0.0, "E4 partial request cooldown"
    _partial_requested_ns[position.position_id] = now_ns
    return action, fraction, reason


core.E4Policy.exit = _exit_with_partial_cooldown


# ---------------------------------------------------------------------------
# Background-task supervision
# ---------------------------------------------------------------------------


def _spawn_supervised(self: core.Engine, coro: Any) -> None:
    task = asyncio.create_task(coro)
    self.tasks.add(task)

    def done(completed: asyncio.Task[Any]) -> None:
        self.tasks.discard(completed)
        if completed.cancelled():
            return
        try:
            error = completed.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            LOGGER.error(
                "E4 background task failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    task.add_done_callback(done)


core.Engine.spawn = _spawn_supervised

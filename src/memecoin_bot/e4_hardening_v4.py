from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Coroutine
from typing import Any, Mapping

from . import e4_hardening_v3

core = e4_hardening_v3.core
hardening = e4_hardening_v3.hardening
final = hardening.final

LOGGER = logging.getLogger("gambit.e4.v4")

# The v3 microburst gate was useful as one observed pattern, but a same-window
# holdout proved it is not a universal E4 entry rule. Until a calibrated
# selected-vs-ignored model is installed, funded execution must fail closed.
_previous_entry = core.E4Policy.entry


def _selection_is_calibrated(policy: core.E4Policy) -> bool:
    model = getattr(policy, "model", None)
    if not isinstance(model, Mapping):
        return False
    status = str(model.get("status") or model.get("model_status") or "").upper()
    kind = str(model.get("kind") or "").lower()
    return status in {"CALIBRATED", "APPROVED", "PRODUCTION"} and kind in {
        "logistic",
        "gradient_boosted",
        "ruleset",
    }


def _v4_entry(
    self: core.E4Policy,
    state: core.TokenState,
) -> tuple[bool, float, float, str, dict[str, float]]:
    if self.settings.live and not _selection_is_calibrated(self):
        return (
            False,
            0.0,
            0.0,
            "E4 selected-vs-ignored entry model is not calibrated",
            state.features(),
        )
    return _previous_entry(self, state)


core.E4Policy.entry = _v4_entry


_original_engine_init = core.Engine.__init__


def _engine_init_v4(self: core.Engine, settings: core.Settings) -> None:
    _original_engine_init(self, settings)
    self.e4_position_locks: dict[str, asyncio.Lock] = {}
    self.e4_pending_exit_started_ns: dict[str, int] = {}
    self.e4_pending_exit_fraction: dict[str, float] = {}
    self.e4_exit_escalations: dict[str, str] = {}
    self.e4_task_failures = 0


core.Engine.__init__ = _engine_init_v4


def ensure_runtime_state(engine: core.Engine) -> None:
    """Populate v4 runtime fields for recovered engines and isolated tests."""
    if not hasattr(engine, "e4_position_locks"):
        engine.e4_position_locks = {}
    if not hasattr(engine, "e4_pending_exit_started_ns"):
        engine.e4_pending_exit_started_ns = {}
    if not hasattr(engine, "e4_pending_exit_fraction"):
        engine.e4_pending_exit_fraction = {}
    if not hasattr(engine, "e4_exit_escalations"):
        engine.e4_exit_escalations = {}
    if not hasattr(engine, "e4_task_failures"):
        engine.e4_task_failures = 0


def _supervised_spawn(self: core.Engine, coro: Coroutine[Any, Any, Any]) -> None:
    ensure_runtime_state(self)
    task = asyncio.create_task(coro)
    self.tasks.add(task)

    def completed(value: asyncio.Task[Any]) -> None:
        self.tasks.discard(value)
        if value.cancelled():
            return
        try:
            error = value.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            self.e4_task_failures += 1
            LOGGER.exception(
                "Uncaught E4 background task failure",
                exc_info=(type(error), error, error.__traceback__),
            )

    task.add_done_callback(completed)


core.Engine.spawn = _supervised_spawn


def _queue_exit(
    engine: core.Engine,
    position: core.Position,
    fraction: float,
    reason: str,
    *,
    guardian: bool = False,
) -> bool:
    ensure_runtime_state(engine)
    mint = position.mint
    fraction = min(1.0, max(0.0, fraction))
    if fraction <= 0:
        return False
    if mint not in engine.positions:
        return False
    if mint in engine.pending_exits:
        if fraction >= 0.999:
            engine.e4_exit_escalations[mint] = reason
        return False
    engine.pending_exits.add(mint)
    engine.e4_pending_exit_started_ns[mint] = time.time_ns()
    engine.e4_pending_exit_fraction[mint] = fraction
    engine.store.decision(
        mint,
        None,
        "SELL_ALL" if fraction >= 0.999 else "SELL_PARTIAL",
        None,
        reason,
        {"fraction": fraction, "guardian": guardian, "v4": True},
    )
    engine.spawn(engine.execute_sell(position, fraction, reason))
    return True


_previous_on_event = core.Engine.on_event


async def _on_event_v4(self: core.Engine, event: core.Event) -> None:
    await _previous_on_event(self, event)
    ensure_runtime_state(self)
    position = self.positions.get(event.mint)
    if position is None or event.mint not in self.pending_exits:
        return
    state = self.tokens.get(event.mint)
    if state is None:
        return
    action, fraction, reason = self.policy.exit(position, state)
    if action == "SELL_ALL" or fraction >= 0.999:
        current = self.e4_pending_exit_fraction.get(event.mint, 0.0)
        if current < 0.999:
            self.e4_exit_escalations[event.mint] = (
                f"Escalated during in-flight partial: {reason}"
            )


core.Engine.on_event = _on_event_v4


_previous_execute_sell = core.Engine.execute_sell


async def _execute_sell_serialized(
    self: core.Engine,
    position: core.Position,
    fraction: float,
    reason: str,
) -> None:
    ensure_runtime_state(self)
    mint = position.mint
    lock = self.e4_position_locks.setdefault(mint, asyncio.Lock())
    self.e4_pending_exit_started_ns.setdefault(mint, time.time_ns())
    self.e4_pending_exit_fraction[mint] = max(
        self.e4_pending_exit_fraction.get(mint, 0.0),
        min(1.0, max(0.0, fraction)),
    )
    async with lock:
        await _previous_execute_sell(self, position, fraction, reason)

    # Rejected or synchronously failed sells clear pending_exits in the lower
    # layer. If a full liquidation was requested while a partial was in flight,
    # immediately continue with the remaining balance.
    if mint not in self.pending_exits:
        self.e4_pending_exit_started_ns.pop(mint, None)
        self.e4_pending_exit_fraction.pop(mint, None)
        escalation = self.e4_exit_escalations.pop(mint, None)
        remaining = self.positions.get(mint)
        if escalation and remaining is not None:
            _queue_exit(self, remaining, 1.0, escalation, guardian=True)


core.Engine.execute_sell = _execute_sell_serialized


_previous_recover_confirmed_sell = hardening._recover_confirmed_sell


async def _recover_confirmed_sell_v4(
    engine: core.Engine,
    position: core.Position,
    live_tokens: float,
    fraction: float,
    signature: str,
    before_sol: float,
) -> None:
    ensure_runtime_state(engine)
    lock = engine.e4_position_locks.setdefault(position.mint, asyncio.Lock())
    async with lock:
        await _previous_recover_confirmed_sell(
            engine,
            position,
            live_tokens,
            fraction,
            signature,
            before_sol,
        )

    mint = position.mint
    engine.e4_pending_exit_started_ns.pop(mint, None)
    engine.e4_pending_exit_fraction.pop(mint, None)
    escalation = engine.e4_exit_escalations.pop(mint, None)
    remaining = engine.positions.get(mint)
    if escalation and remaining is not None and mint not in engine.pending_exits:
        _queue_exit(engine, remaining, 1.0, escalation, guardian=True)


hardening._recover_confirmed_sell = _recover_confirmed_sell_v4


def _last_trade_ns(state: core.TokenState | None) -> int:
    if state is None:
        return 0
    for event in reversed(state.events):
        if event.kind in {
            core.EventKind.BUY,
            core.EventKind.SELL,
            core.EventKind.PUMPSWAP_BUY,
            core.EventKind.PUMPSWAP_SELL,
        }:
            return int(event.source_ns)
    return 0


async def _guardian_v4(self: core.Engine) -> None:
    """Independent position clock.

    E4 positions may go quiet after entry. Exit guarantees therefore cannot
    depend on another trade event arriving for the mint.
    """
    ensure_runtime_state(self)
    interval = max(0.005, float(os.getenv("E4_GUARDIAN_INTERVAL_SECONDS", "0.01")))
    confirmation_quiet_ms = max(
        50,
        int(os.getenv("E4_CONFIRMATION_QUIET_MS", "750")),
    )
    stale_exit_ms = max(
        2_000,
        int(os.getenv("E4_PENDING_EXIT_STALE_MS", "15000")),
    )

    while not self.stop_event.is_set():
        now = time.time_ns()
        for mint, position in tuple(self.positions.items()):
            state = self.tokens.get(mint)
            age_ms = max(0, int((now - position.opened_ns) / 1_000_000))
            lock = self.e4_position_locks.setdefault(mint, asyncio.Lock())

            if mint in self.pending_exits:
                started = self.e4_pending_exit_started_ns.setdefault(mint, now)
                pending_ms = max(0, int((now - started) / 1_000_000))
                if pending_ms >= stale_exit_ms:
                    self.e4_exit_escalations[mint] = "E4 stale exit retry"
                    # All execution/RPC calls are bounded. Once the per-mint
                    # lock is free, a stale pending marker is safe to repair.
                    if not lock.locked():
                        self.pending_exits.discard(mint)
                        self.e4_pending_exit_started_ns.pop(mint, None)
                        self.e4_pending_exit_fraction.pop(mint, None)
                        _queue_exit(
                            self,
                            position,
                            1.0,
                            "E4 stale exit retry",
                            guardian=True,
                        )
                continue

            escalation = self.e4_exit_escalations.pop(mint, None)
            if escalation:
                _queue_exit(self, position, 1.0, escalation, guardian=True)
                continue

            if age_ms >= self.settings.max_hold_ms:
                _queue_exit(
                    self,
                    position,
                    1.0,
                    "E4 observed absolute hold horizon",
                    guardian=True,
                )
                continue

            if not position.first_partial_done and age_ms >= self.settings.failure_window_ms:
                last_trade_ns = _last_trade_ns(state)
                quiet_ms = (
                    max(0, int((now - last_trade_ns) / 1_000_000))
                    if last_trade_ns
                    else age_ms
                )
                no_post_entry_trade = last_trade_ns <= position.opened_ns
                if no_post_entry_trade or quiet_ms >= confirmation_quiet_ms:
                    _queue_exit(
                        self,
                        position,
                        1.0,
                        "E4 confirmation window expired without fresh flow",
                        guardian=True,
                    )
                    continue
                if state is not None:
                    action, fraction, reason = self.policy.exit(position, state)
                    if action.startswith("SELL"):
                        _queue_exit(
                            self,
                            position,
                            fraction,
                            reason,
                            guardian=True,
                        )

        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


# e4_hardening's run wrapper calls e4_final._final_run, which resolves this
# module-global guardian at runtime.
final._guardian = _guardian_v4

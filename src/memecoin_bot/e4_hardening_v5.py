from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from collections import Counter
from typing import Any

from . import e4_hardening_v4

core = e4_hardening_v4.core
final = e4_hardening_v4.final
LOGGER = logging.getLogger("gambit.e4.hardening.v5")


# ---------------------------------------------------------------------------
# Selection correction
# ---------------------------------------------------------------------------
# The V3 same-signature bundled-microburst gate selected none of the three E4
# entries observed in the 20-minute holdout. It remains available only for
# reproducible non-live tests. A funded runtime now fails closed until a real
# selected-vs-ignored model exists. RESEARCH_COHORT is explicit and can only be
# used for hypothesis-only live-market capture.

_previous_entry = core.E4Policy.entry


def _selection_mode(policy: core.E4Policy) -> str:
    configured = os.getenv("E4_SELECTION_MODE", "").strip().upper()
    if configured:
        return configured
    return "MODEL_REQUIRED" if policy.settings.live else "LEGACY_MICROBURST"


def _selection_features(state: core.TokenState) -> dict[str, float]:
    values = state.features()
    events = list(state.events)
    create = next((event for event in events if event.kind == core.EventKind.CREATE), None)
    buys = [
        event
        for event in events
        if event.kind in {core.EventKind.BUY, core.EventKind.PUMPSWAP_BUY}
    ]
    sells = [
        event
        for event in events
        if event.kind in {core.EventKind.SELL, core.EventKind.PUMPSWAP_SELL}
    ]
    creator = state.creator or (create.creator if create else None)
    creator_buys = [event for event in buys if creator and event.trader == creator]
    external_buys = [event for event in buys if not creator or event.trader != creator]
    signatures = Counter(event.signature for event in buys if event.signature)
    create_slot = create.slot if create else None
    latest_slot = max((event.slot or 0 for event in events), default=0)
    first_price = next((event.price_sol for event in events if event.price_sol), None)
    age_ms = (
        max(0.0, (state.latest_ns - state.created_ns) / 1_000_000)
        if state.created_ns is not None
        else math.inf
    )
    values.update(
        {
            "token_age_ms": age_ms,
            "entry_fdv_usd": float(state.fdv_usd or 0.0),
            "pre_buy_count": float(len(buys)),
            "pre_sell_count": float(len(sells)),
            "pre_buy_sol": sum(max(0.0, event.sol_amount) for event in buys),
            "pre_sell_sol": sum(max(0.0, event.sol_amount) for event in sells),
            "pre_unique_buyers": float(len({event.trader for event in buys if event.trader})),
            "creator_buy_sol": sum(max(0.0, event.sol_amount) for event in creator_buys),
            "external_buy_count": float(len(external_buys)),
            "external_unique_buyers": float(
                len({event.trader for event in external_buys if event.trader})
            ),
            "external_buy_sol": sum(max(0.0, event.sol_amount) for event in external_buys),
            "same_slot": float(
                bool(create_slot is not None and latest_slot and create_slot == latest_slot)
            ),
            "creator_buy_in_create_tx": float(
                bool(
                    create
                    and create.signature
                    and any(event.signature == create.signature for event in creator_buys)
                )
            ),
            "multi_buy_signature_count": float(
                sum(count for count in signatures.values() if count > 1)
            ),
            "max_buys_same_signature": float(max(signatures.values(), default=0)),
            "price_multiple_from_create": (
                float((state.price_sol or 0.0) / first_price)
                if first_price and state.price_sol
                else 0.0
            ),
        }
    )
    return values


def _model_entry(
    policy: core.E4Policy,
    features: dict[str, float],
) -> tuple[bool, float, float, str, dict[str, float]]:
    model = getattr(policy, "model", None)
    if not isinstance(model, dict):
        return (
            False,
            0.0,
            0.0,
            "E4 live entry blocked: calibrated selected-vs-ignored model unavailable",
            features,
        )
    coefficients = model.get("coefficients") or {}
    logit = float(model.get("intercept", 0.0))
    for name, coefficient in coefficients.items():
        logit += float(coefficient) * float(features.get(str(name), 0.0))
    score = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, logit))))
    threshold = float(model.get("threshold", 0.5))
    if score < threshold:
        return False, score, 0.0, "E4 selected-vs-ignored model rejected", features
    fraction = min(
        policy.settings.max_position_fraction,
        max(
            0.0,
            float(model.get("base_position_fraction", 0.05))
            + score * float(model.get("score_position_fraction", 0.15)),
        ),
    )
    return True, score, fraction, "E4 calibrated selection model accepted", features


def _research_cohort_entry(
    policy: core.E4Policy,
    state: core.TokenState,
    features: dict[str, float],
) -> tuple[bool, float, float, str, dict[str, float]]:
    if state.complete or state.migrated or state.wallet_touched:
        return False, 0.0, 0.0, "not an untouched live Pump curve", features
    if state.created_ns is None:
        return False, 0.0, 0.0, "creation event not observed", features
    max_age_ms = float(os.getenv("E4_RESEARCH_MAX_ENTRY_AGE_MS", "250"))
    fdv = features["entry_fdv_usd"]
    if features["token_age_ms"] > max_age_ms:
        return False, 0.0, 0.0, "outside E4 same-slot research window", features
    if not 2_500.0 <= fdv <= min(policy.settings.max_entry_fdv_usd, 10_000.0):
        return False, 0.0, 0.0, "outside observed E4 birth FDV", features
    if features["same_slot"] < 1:
        return False, 0.0, 0.0, "not in creation slot", features
    if features["pre_sell_count"] > 0 or features["pre_sell_sol"] > 0:
        return False, 0.0, 0.0, "sell appeared before E4 research entry", features
    if features["creator_buy_in_create_tx"] < 1 or features["creator_buy_sol"] < 0.02:
        return False, 0.0, 0.0, "creator did not seed the launch transaction", features

    creator_follow = fdv <= 4_600 and features["creator_buy_sol"] >= 0.02
    funded_creator = fdv <= 6_500 and features["creator_buy_sol"] >= 2.0
    capital_burst = (
        features["external_unique_buyers"] >= 2
        and features["pre_buy_sol"] >= 8.0
        and fdv <= 10_000.0
    )
    if not (creator_follow or funded_creator or capital_burst):
        return False, 0.0, 0.0, "outside observed E4 creator/capital cohorts", features

    fdv_score = max(0.0, 1.0 - abs(fdv - 4_878.0) / 7_500.0)
    creator_score = min(1.0, features["creator_buy_sol"] / 5.0)
    external_score = min(1.0, features["external_buy_sol"] / 7.0)
    speed_score = max(0.0, 1.0 - features["token_age_ms"] / max_age_ms)
    score = (
        0.30 * fdv_score
        + 0.25 * creator_score
        + 0.25 * external_score
        + 0.20 * speed_score
    )
    fraction = min(policy.settings.max_position_fraction, 0.05 + 0.15 * score)
    cohort = (
        "capital_burst"
        if capital_burst
        else ("funded_creator" if funded_creator else "creator_follow")
    )
    return True, score, fraction, f"E4 research-only {cohort} cohort accepted", features


def _entry_v5(
    self: core.E4Policy,
    state: core.TokenState,
) -> tuple[bool, float, float, str, dict[str, float]]:
    mode = _selection_mode(self)
    if mode == "LEGACY_MICROBURST":
        return _previous_entry(self, state)
    features = _selection_features(state)
    if mode == "RESEARCH_COHORT":
        if self.settings.live and not core._bool("E4_ALLOW_RESEARCH_LIVE", False):
            return (
                False,
                0.0,
                0.0,
                "E4 research cohort is forbidden in funded live mode",
                features,
            )
        return _research_cohort_entry(self, state, features)
    if mode == "DISABLED":
        return False, 0.0, 0.0, "E4 entry disabled", features
    if mode != "MODEL_REQUIRED":
        return False, 0.0, 0.0, f"unsupported E4_SELECTION_MODE={mode}", features
    return _model_entry(self, features)


core.E4Policy.entry = _entry_v5


# ---------------------------------------------------------------------------
# Concurrent-position mutation safety
# ---------------------------------------------------------------------------

_previous_engine_init = core.Engine.__init__


def _engine_init_v5(self: core.Engine, settings: core.Settings) -> None:
    _previous_engine_init(self, settings)
    self.position_locks: dict[str, asyncio.Lock] = {}


core.Engine.__init__ = _engine_init_v5

_previous_execute_sell = core.Engine.execute_sell


async def _execute_sell_serialized(
    self: core.Engine,
    position: core.Position,
    fraction: float,
    reason: str,
) -> None:
    lock = self.position_locks.setdefault(position.mint, asyncio.Lock())
    async with lock:
        await _previous_execute_sell(self, position, fraction, reason)


core.Engine.execute_sell = _execute_sell_serialized


# ---------------------------------------------------------------------------
# Pending-exit stall observability
# ---------------------------------------------------------------------------

async def _pending_exit_monitor(self: core.Engine) -> None:
    first_seen: dict[str, int] = {}
    reported: set[str] = set()
    interval = max(0.05, float(os.getenv("E4_PENDING_EXIT_MONITOR_SECONDS", "0.10")))
    stall_seconds = max(5.0, float(os.getenv("E4_PENDING_EXIT_STALL_SECONDS", "25")))
    while not self.stop_event.is_set():
        now = time.time_ns()
        pending = set(self.pending_exits)
        for mint in tuple(first_seen):
            if mint not in pending:
                first_seen.pop(mint, None)
                reported.discard(mint)
        for mint in pending:
            first_seen.setdefault(mint, now)
            elapsed = (now - first_seen[mint]) / 1_000_000_000
            if elapsed < stall_seconds or mint in reported:
                continue
            reported.add(mint)
            LOGGER.critical(
                "E4 exit remains pending beyond %.2fs mint=%s",
                stall_seconds,
                mint,
            )
            self.store.decision(
                mint,
                None,
                "EXIT_STALLED",
                None,
                "E4 pending exit exceeded bounded execution window",
                {"pending_seconds": elapsed, "guardian_version": "v5"},
            )
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


_previous_guardian = final._guardian


async def _guardian_v5(self: core.Engine) -> None:
    monitor = asyncio.create_task(_pending_exit_monitor(self), name="e4-v5-exit-monitor")
    try:
        await _previous_guardian(self)
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)


final._guardian = _guardian_v5

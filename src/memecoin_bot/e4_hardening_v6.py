from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from . import e4_hardening_v5

core = e4_hardening_v5.core
final = e4_hardening_v5.final
v4 = e4_hardening_v5.e4_hardening_v4
LOGGER = logging.getLogger("gambit.e4.hardening.v6")

# Empirical E4 constraints from the reconciled wallet sample.
EMPIRICAL_MAX_RELATIVE_FRACTION = 0.115
HIGH_CONVICTION_PARTIAL_FRACTION = 0.027
DEFAULT_RUNNER_EMERGENCY_MS = 300_000

_ENTRY_FRACTION_BY_MINT: dict[str, float] = {}
_ENTRY_FAMILY_BY_MINT: dict[str, str] = {}
_ENTRY_SCORE_BY_MINT: dict[str, float] = {}


def _json_mapping(env_name: str, path_env_name: str) -> dict[str, Any]:
    inline = os.getenv(env_name, "").strip()
    path = os.getenv(path_env_name, "").strip()
    payload: Any = {}
    try:
        if inline:
            payload = json.loads(inline)
        elif path:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        LOGGER.error("Could not load %s/%s: %s", env_name, path_env_name, exc)
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _profile_score(value: Any) -> float:
    if isinstance(value, Mapping):
        raw = (
            value.get("confidence")
            or value.get("score")
            or value.get("quality")
            or value.get("win_probability")
            or 0.0
        )
    else:
        raw = value
    try:
        score = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if score > 1.0 and score <= 100.0:
        score /= 100.0
    return max(0.0, min(1.0, score)) if math.isfinite(score) else 0.0


PREARMED_MINTS = _json_mapping("E4_PREARMED_MINTS_JSON", "E4_PREARMED_MINTS_PATH")
CREATOR_PROFILES = _json_mapping("E4_CREATOR_PROFILES_JSON", "E4_CREATOR_PROFILES_PATH")
WALLET_PROFILES = _json_mapping("E4_WALLET_PROFILES_JSON", "E4_WALLET_PROFILES_PATH")


def reload_identity_caches() -> None:
    PREARMED_MINTS.clear()
    PREARMED_MINTS.update(
        _json_mapping("E4_PREARMED_MINTS_JSON", "E4_PREARMED_MINTS_PATH")
    )
    CREATOR_PROFILES.clear()
    CREATOR_PROFILES.update(
        _json_mapping("E4_CREATOR_PROFILES_JSON", "E4_CREATOR_PROFILES_PATH")
    )
    WALLET_PROFILES.clear()
    WALLET_PROFILES.update(
        _json_mapping("E4_WALLET_PROFILES_JSON", "E4_WALLET_PROFILES_PATH")
    )


def _trade_events(state: core.TokenState, milliseconds: int | None = None) -> list[core.Event]:
    events = [
        event
        for event in state.events
        if event.kind
        in {
            core.EventKind.BUY,
            core.EventKind.SELL,
            core.EventKind.PUMPSWAP_BUY,
            core.EventKind.PUMPSWAP_SELL,
        }
    ]
    if milliseconds is None or not events:
        return events
    cutoff = state.latest_ns - milliseconds * 1_000_000
    return [event for event in events if event.source_ns >= cutoff]


def _create_event(state: core.TokenState) -> core.Event | None:
    return next(
        (event for event in state.events if event.kind == core.EventKind.CREATE),
        None,
    )


def _price_multiple(state: core.TokenState) -> float:
    prices = [
        event.price_sol
        for event in state.events
        if event.price_sol and event.price_sol > 0
    ]
    if not prices or not state.price_sol:
        return 1.0
    return max(0.0, state.price_sol / prices[0])


def _relative_fraction(score: float, family: str) -> float:
    # Observed wallet-fraction quantiles, represented as a monotonic confidence
    # ladder rather than copied absolute SOL sizes.
    if score < 0.68:
        fraction = 0.0075
    elif score < 0.75:
        fraction = 0.0090
    elif score < 0.82:
        fraction = 0.0125
    elif score < 0.88:
        fraction = 0.0180
    elif score < 0.93:
        fraction = 0.0250
    elif score < 0.97:
        fraction = 0.0400
    elif score < 0.995:
        fraction = 0.0800
    else:
        fraction = EMPIRICAL_MAX_RELATIVE_FRACTION

    family_caps = {
        "seeded_acceleration": 0.0180,
        "bundled_microburst": 0.0400,
        "public_capital_burst": 0.0400,
        "known_creator": 0.0800,
        "elite_creator": EMPIRICAL_MAX_RELATIVE_FRACTION,
        "prearmed_launch": EMPIRICAL_MAX_RELATIVE_FRACTION,
    }
    configured = float(
        os.getenv(
            "E4_MAX_RELATIVE_POSITION_FRACTION",
            str(EMPIRICAL_MAX_RELATIVE_FRACTION),
        )
    )
    return min(
        fraction,
        family_caps.get(family, 0.0250),
        max(0.0, min(EMPIRICAL_MAX_RELATIVE_FRACTION, configured)),
    )


def _likely_e4_entry(
    self: core.E4Policy,
    state: core.TokenState,
) -> tuple[bool, float, float, str, dict[str, float]]:
    if state.complete or state.migrated or state.wallet_touched:
        return False, 0.0, 0.0, "not an untouched live Pump curve", {}
    create = _create_event(state)
    if create is None or state.created_ns is None:
        return False, 0.0, 0.0, "creation event not observed", {}

    age_ms = max(0.0, (state.latest_ns - state.created_ns) / 1_000_000)
    max_age_ms = float(os.getenv("E4_LIKELY_ENTRY_MAX_AGE_MS", "500"))
    if age_ms > max_age_ms:
        return False, 0.0, 0.0, "outside E4 birth-entry window", {}

    fdv = state.fdv_usd or 0.0
    min_fdv = float(os.getenv("E4_LIKELY_ENTRY_MIN_FDV_USD", "2500"))
    max_fdv = min(
        float(os.getenv("E4_LIKELY_ENTRY_MAX_FDV_USD", "10000")),
        self.settings.max_entry_fdv_usd,
    )
    if fdv <= 0 or fdv < min_fdv or fdv > max_fdv:
        return False, 0.0, 0.0, "outside observed E4 entry FDV", {}

    events = _trade_events(state)
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
    if sells:
        return False, 0.0, 0.0, "sell appeared before E4 entry", state.features()

    creator = state.creator or create.creator
    creator_buys = [event for event in buys if creator and event.trader == creator]
    noncreator_buys = [
        event
        for event in buys
        if not creator or event.trader != creator
    ]
    buyers = {event.trader for event in buys if event.trader}
    noncreator_buyers = {
        event.trader for event in noncreator_buys if event.trader
    }
    creator_buy_sol = sum(max(0.0, event.sol_amount) for event in creator_buys)
    total_buy_sol = sum(max(0.0, event.sol_amount) for event in buys)
    noncreator_buy_sol = sum(
        max(0.0, event.sol_amount) for event in noncreator_buys
    )
    price_multiple = _price_multiple(state)

    signatures = Counter(event.signature for event in buys if event.signature)
    bundled_buys = sum(count for count in signatures.values() if count > 1)
    max_same_signature = max(signatures.values(), default=0)

    creator_score = _profile_score(CREATOR_PROFILES.get(creator or ""))
    prearmed_score = _profile_score(PREARMED_MINTS.get(state.mint))
    smart_wallet_scores = [
        _profile_score(WALLET_PROFILES.get(event.trader or ""))
        for event in noncreator_buys
    ]
    best_wallet_score = max(smart_wallet_scores, default=0.0)
    smart_wallet_count = sum(score >= 0.75 for score in smart_wallet_scores)

    same_slot = bool(
        create.slot is not None
        and state.events
        and state.events[-1].slot == create.slot
    )
    features = state.features()
    features.update(
        {
            "token_age_ms": age_ms,
            "creator_buy_sol": creator_buy_sol,
            "total_buy_sol": total_buy_sol,
            "unique_buyers": float(len(buyers)),
            "noncreator_buy_sol": noncreator_buy_sol,
            "noncreator_buyers": float(len(noncreator_buyers)),
            "price_multiple": price_multiple,
            "bundled_buys": float(bundled_buys),
            "max_same_signature_buys": float(max_same_signature),
            "creator_score": creator_score,
            "prearmed_score": prearmed_score,
            "best_early_wallet_score": best_wallet_score,
            "smart_early_wallets": float(smart_wallet_count),
            "same_slot": float(same_slot),
            # Compatibility aliases retained for the original E4 fixture suite.
            "microburst_buyers": float(len(buyers)),
            "microburst_buy_count": float(len(buys)),
            "microburst_buy_sol": total_buy_sol,
            "microburst_sell_sol": 0.0,
            "microburst_bundled_buys": float(bundled_buys),
            "microburst_max_same_signature": float(max_same_signature),
            "microburst_price_multiple": price_multiple,
        }
    )

    family = ""
    score = 0.0

    # Explicit/pre-authorized launch intent is the only family allowed to act
    # without visible public capital, matching E4's 29-36ms observations.
    if prearmed_score >= 0.85 and age_ms <= 250:
        family = "prearmed_launch"
        score = min(0.999, 0.905 + 0.094 * prearmed_score)
    elif (
        creator_score >= 0.90
        and creator_buy_sol >= 0.04
        and age_ms <= 350
        and (len(noncreator_buyers) >= 1 or price_multiple >= 1.025)
    ):
        family = "elite_creator"
        score = min(
            0.995,
            0.84
            + 0.10 * creator_score
            + 0.025 * min(1.0, noncreator_buy_sol / 2.0)
            + 0.02 * min(1.0, max(0.0, price_multiple - 1.0) / 0.15),
        )
    elif (
        creator_score >= 0.75
        and creator_buy_sol >= 0.04
        and age_ms <= 450
        and (
            len(noncreator_buyers) >= 1
            or best_wallet_score >= 0.80
            or price_multiple >= 1.04
        )
    ):
        family = "known_creator"
        score = min(
            0.96,
            0.72
            + 0.14 * creator_score
            + 0.06 * best_wallet_score
            + 0.04 * min(1.0, total_buy_sol / 5.0),
        )
    elif (
        age_ms <= 350
        and creator_buy_sol >= 2.0
        and len(noncreator_buyers) >= 3
        and noncreator_buy_sol >= 8.0
        and total_buy_sol >= 12.0
        and price_multiple >= 1.08
    ):
        family = "public_capital_burst"
        score = min(
            0.95,
            0.70
            + 0.06 * min(1.0, len(noncreator_buyers) / 8.0)
            + 0.08 * min(1.0, noncreator_buy_sol / 20.0)
            + 0.06 * min(1.0, max(0.0, price_multiple - 1.0) / 0.50)
            + 0.05 * best_wallet_score,
        )
    elif (
        age_ms <= 120
        and len(buyers) >= 5
        and total_buy_sol >= 10.0
        and bundled_buys >= 3
        and max_same_signature >= 2
        and price_multiple >= 1.25
    ):
        family = "bundled_microburst"
        score = min(
            0.92,
            0.68
            + 0.07 * min(1.0, total_buy_sol / 18.0)
            + 0.07 * min(1.0, bundled_buys / 8.0)
            + 0.05 * min(1.0, max_same_signature / 4.0)
            + 0.05 * best_wallet_score,
        )
    elif (
        age_ms <= 400
        and creator_buy_sol >= 1.0
        and len(noncreator_buyers) >= 2
        and total_buy_sol >= 4.0
        and price_multiple >= 1.15
        and (smart_wallet_count >= 1 or noncreator_buy_sol >= 3.0)
    ):
        family = "seeded_acceleration"
        score = min(
            0.86,
            0.64
            + 0.06 * min(1.0, creator_buy_sol / 4.0)
            + 0.07 * min(1.0, noncreator_buy_sol / 8.0)
            + 0.05 * best_wallet_score
            + 0.04 * min(1.0, max(0.0, price_multiple - 1.0) / 0.35),
        )

    if not family:
        return False, 0.0, 0.0, "no observed E4 entry family matched", features

    fraction = _relative_fraction(score, family)
    if fraction <= 0:
        return False, score, 0.0, "E4 relative sizing returned zero", features
    features["entry_family_code"] = float(
        {
            "prearmed_launch": 5,
            "elite_creator": 4,
            "known_creator": 3,
            "public_capital_burst": 2,
            "bundled_microburst": 1,
            "seeded_acceleration": 0,
        }[family]
    )
    _ENTRY_FRACTION_BY_MINT[state.mint] = fraction
    _ENTRY_FAMILY_BY_MINT[state.mint] = family
    _ENTRY_SCORE_BY_MINT[state.mint] = score
    return True, score, fraction, f"E4 likely-entry family={family}", features


core.E4Policy.entry = _likely_e4_entry


def _entry_fraction(position: core.Position) -> float:
    fraction = _ENTRY_FRACTION_BY_MINT.get(position.mint)
    if fraction is not None:
        return fraction
    return 0.027 if position.entry_sol >= 4.2 else 0.0125


def _e4_exit_v6(
    self: core.E4Policy,
    position: core.Position,
    state: core.TokenState,
) -> tuple[str, float, str]:
    price = state.price_sol or position.last_price
    if not price:
        return "HOLD", 0.0, "no price"
    position.last_price = price
    position.max_price = max(position.max_price, price)
    markout = position.markout_bps(price)
    flow250 = state.flow(250)
    flow1s = state.flow(1000)
    broken = flow250.net < 0 or flow1s.ratio < 0.85

    if position.age_ms <= self.settings.failure_window_ms:
        if markout <= self.settings.failure_markout_bps:
            return "SELL_ALL", 1.0, "E4 fast adverse-markout failure"
        if broken and markout <= self.settings.flow_break_markout_bps:
            return "SELL_ALL", 1.0, "E4 fast flow-break failure"

    if not position.first_partial_done:
        fraction = _entry_fraction(position)
        has_v6_context = position.mint in _ENTRY_FRACTION_BY_MINT
        high_conviction = (
            fraction
            >= float(
                os.getenv(
                    "E4_HIGH_CONVICTION_FRACTION",
                    str(HIGH_CONVICTION_PARTIAL_FRACTION),
                )
            )
            or (
                not has_v6_context
                and markout >= self.settings.acceleration_partial_markout_bps
            )
        )
        threshold = (
            self.settings.acceleration_partial_markout_bps
            if high_conviction
            else self.settings.normal_partial_markout_bps
        )
        if markout >= threshold:
            return (
                "SELL_PARTIAL",
                0.20 if high_conviction else 0.30,
                "E4 high-conviction first partial"
                if high_conviction
                else "E4 normal first partial",
            )
        if position.age_ms >= self.settings.failure_window_ms and broken:
            return "SELL_ALL", 1.0, "E4 confirmation failed"
        return "HOLD", 0.0, "awaiting E4 confirmation"

    if broken and position.drawdown_bps(price) >= 350:
        return "SELL_ALL", 1.0, "E4 runner flow broke"
    if position.drawdown_bps(price) >= self.settings.runner_drawdown_bps:
        return "SELL_ALL", 1.0, "E4 runner peak drawdown"
    if markout >= 3000 and flow250.net <= 0:
        return "SELL_PARTIAL", 0.25, "E4 runner distribution"

    # Positions opened before V6 did not persist an empirical confidence
    # fraction. Preserve their original bounded horizon during rolling upgrades;
    # all V6 positions use the observed longer runner horizon below.
    if (
        position.mint not in _ENTRY_FRACTION_BY_MINT
        and position.age_ms >= self.settings.max_hold_ms
    ):
        return "SELL_ALL", 1.0, "E4 legacy observed hold horizon"

    runner_emergency_ms = int(
        os.getenv(
            "E4_RUNNER_EMERGENCY_HOLD_MS",
            str(DEFAULT_RUNNER_EMERGENCY_MS),
        )
    )
    if position.age_ms >= runner_emergency_ms:
        return "SELL_ALL", 1.0, "E4 runner emergency horizon"
    return "HOLD", 0.0, "E4 runner confirmed"


_V6_PARTIAL_REQUESTED_NS: dict[str, int] = {}


def _e4_exit_v6_with_cooldown(
    self: core.E4Policy,
    position: core.Position,
    state: core.TokenState,
) -> tuple[str, float, str]:
    action, fraction, reason = _e4_exit_v6(self, position, state)
    if action != "SELL_PARTIAL":
        return action, fraction, reason
    now_ns = time.time_ns()
    cooldown_ms = max(25, int(os.getenv("E4_PARTIAL_COOLDOWN_MS", "200")))
    previous_ns = _V6_PARTIAL_REQUESTED_NS.get(position.position_id, 0)
    if now_ns - previous_ns < cooldown_ms * 1_000_000:
        return "HOLD", 0.0, "E4 partial request cooldown"
    _V6_PARTIAL_REQUESTED_NS[position.position_id] = now_ns
    return action, fraction, reason


core.E4Policy.exit = _e4_exit_v6_with_cooldown


_previous_engine_init = core.Engine.__init__


def _engine_init_v6(self: core.Engine, settings: core.Settings) -> None:
    settings.max_position_fraction = min(
        settings.max_position_fraction,
        float(
            os.getenv(
                "E4_MAX_RELATIVE_POSITION_FRACTION",
                str(EMPIRICAL_MAX_RELATIVE_FRACTION),
            )
        ),
        EMPIRICAL_MAX_RELATIVE_FRACTION,
    )
    _previous_engine_init(self, settings)
    reload_identity_caches()
    try:
        rows = self.store.conn.execute(
            "SELECT mint,payload_json FROM e4_decisions "
            "WHERE action='BUY' ORDER BY id"
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"] or "{}")
            value = payload.get("fraction")
            if value is not None:
                _ENTRY_FRACTION_BY_MINT[str(row["mint"])] = float(value)
    except Exception:
        LOGGER.exception("Could not restore E4 entry fractions")


core.Engine.__init__ = _engine_init_v6


_previous_execute_buy = core.Engine.execute_buy


async def _execute_buy_v6(
    self: core.Engine,
    state: core.TokenState,
    score: float,
    fraction: float,
    reason: str,
) -> None:
    _ENTRY_FRACTION_BY_MINT[state.mint] = min(
        float(fraction),
        EMPIRICAL_MAX_RELATIVE_FRACTION,
    )
    await _previous_execute_buy(self, state, score, fraction, reason)


core.Engine.execute_buy = _execute_buy_v6


async def _guardian_v6(self: core.Engine) -> None:
    interval = max(0.005, float(os.getenv("E4_GUARDIAN_INTERVAL_SECONDS", "0.01")))
    runner_emergency_ms = max(
        self.settings.failure_window_ms + 1,
        int(
            os.getenv(
                "E4_RUNNER_EMERGENCY_HOLD_MS",
                str(DEFAULT_RUNNER_EMERGENCY_MS),
            )
        ),
    )
    while not self.stop_event.is_set():
        now_ns = time.time_ns()
        for mint, position in tuple(self.positions.items()):
            if mint in self.pending_exits:
                continue
            age_ms = max(0, int((now_ns - position.opened_ns) / 1_000_000))
            if not position.first_partial_done:
                reason = v4._guardian_liquidation_reason(self, position, now_ns)
                if reason is not None or age_ms >= self.settings.failure_window_ms:
                    await e4_hardening_v5._schedule_exit(
                        self,
                        position,
                        1.0,
                        reason or "E4 independent confirmation-window liquidation",
                        payload={"guardian": True, "age_ms": age_ms, "version": "v6"},
                    )
                continue

            state = self.tokens.get(mint)
            if state is not None:
                price = state.price_sol or position.last_price
                if price and price > 0:
                    position.last_price = price
                    position.max_price = max(position.max_price, price)
                    flow250 = v4._wall_flow(state, 250, now_ns)
                    flow1s = v4._wall_flow(state, 1000, now_ns)
                    broken = flow250.net < 0 or flow1s.ratio < 0.85
                    if broken and position.drawdown_bps(price) >= 350:
                        await e4_hardening_v5._schedule_exit(
                            self,
                            position,
                            1.0,
                            "E4 independent runner flow-break liquidation",
                            payload={"guardian": True, "age_ms": age_ms, "version": "v6"},
                        )
                        continue
                    if position.drawdown_bps(price) >= self.settings.runner_drawdown_bps:
                        await e4_hardening_v5._schedule_exit(
                            self,
                            position,
                            1.0,
                            "E4 independent runner peak-drawdown liquidation",
                            payload={"guardian": True, "age_ms": age_ms, "version": "v6"},
                        )
                        continue

            if age_ms >= runner_emergency_ms:
                await e4_hardening_v5._schedule_exit(
                    self,
                    position,
                    1.0,
                    "E4 runner emergency horizon",
                    payload={"guardian": True, "age_ms": age_ms, "version": "v6"},
                )
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


final._guardian = _guardian_v6

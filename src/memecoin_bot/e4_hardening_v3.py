from __future__ import annotations

import math
import os
from collections import Counter
from dataclasses import replace
from typing import Any, Mapping

from . import e4_hardening_v2

core = e4_hardening_v2.core
hardening = e4_hardening_v2.e4_hardening


def _value(mapping: Mapping[str, Any], *keys: str) -> Any:
    lowered = {str(key).lower(): value for key, value in mapping.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return None


# V1.5's Anchor source timestamp is second-resolution. E4's edge is measured in
# milliseconds, so microstructure state must use the provider receipt clock,
# while received_ns records the point the event became available to Gambit.
_previous_from_row = core.Event.from_row.__func__


def _receipt_clock_from_row(cls: type[core.Event], row: Mapping[str, Any]) -> core.Event:
    event = _previous_from_row(cls, row)
    merged = dict(row)
    for key in ("payload_json", "event_json", "raw_json", "data_json", "payload"):
        for nested_key, nested_value in hardening._parse_json_mapping(merged.get(key)).items():
            merged.setdefault(str(nested_key), nested_value)
    receipt_ns = hardening._timestamp_ns(
        _value(
            merged,
            "received_timestamp",
            "source_received_at",
            "received_at",
            "available_timestamp",
        )
    )
    available_ns = hardening._timestamp_ns(
        _value(
            merged,
            "available_timestamp",
            "received_timestamp",
            "source_received_at",
        )
    )
    return replace(
        event,
        source_ns=receipt_ns or event.source_ns,
        received_ns=available_ns or receipt_ns or event.received_ns,
    )


core.Event.from_row = classmethod(_receipt_clock_from_row)


def _recent_trade_events(state: core.TokenState, milliseconds: int) -> list[core.Event]:
    cutoff = state.latest_ns - milliseconds * 1_000_000
    return [
        event
        for event in state.events
        if event.source_ns >= cutoff
        and event.kind
        in {
            core.EventKind.BUY,
            core.EventKind.SELL,
            core.EventKind.PUMPSWAP_BUY,
            core.EventKind.PUMPSWAP_SELL,
        }
    ]


def _observed_microburst_entry(
    self: core.E4Policy,
    state: core.TokenState,
) -> tuple[bool, float, float, str, dict[str, float]]:
    if state.complete or state.migrated or state.wallet_touched:
        return False, 0.0, 0.0, "not an untouched live Pump curve", {}
    if state.created_ns is None:
        return False, 0.0, 0.0, "creation event not observed", {}
    age_ms = max(0.0, (state.latest_ns - state.created_ns) / 1_000_000)
    max_age_ms = float(os.getenv("E4_MICROBURST_MAX_AGE_MS", "25"))
    if age_ms > max_age_ms:
        return False, 0.0, 0.0, "outside observed E4 same-slot entry window", {}
    fdv = state.fdv_usd
    max_fdv = min(
        self.settings.max_entry_fdv_usd,
        float(os.getenv("E4_MICROBURST_MAX_FDV_USD", "8000")),
    )
    if not fdv or fdv > max_fdv:
        return False, 0.0, 0.0, "outside observed E4 microburst FDV", {}

    window_ms = int(os.getenv("E4_MICROBURST_WINDOW_MS", "10"))
    trades = _recent_trade_events(state, window_ms)
    buys = [
        event
        for event in trades
        if event.kind in {core.EventKind.BUY, core.EventKind.PUMPSWAP_BUY}
    ]
    sells = [
        event
        for event in trades
        if event.kind in {core.EventKind.SELL, core.EventKind.PUMPSWAP_SELL}
    ]
    buyers = {event.trader for event in buys if event.trader}
    signatures = Counter(event.signature for event in buys if event.signature)
    bundled_buys = sum(count for count in signatures.values() if count > 1)
    max_same_signature = max(signatures.values(), default=0)
    buy_sol = sum(max(0.0, event.sol_amount) for event in buys)
    sell_sol = sum(max(0.0, event.sol_amount) for event in sells)
    first_price = next((event.price_sol for event in state.events if event.price_sol), None)
    price_multiple = (
        (state.price_sol or 0.0) / first_price if first_price and state.price_sol else 0.0
    )
    features = state.features()
    features.update(
        {
            "token_age_ms": age_ms,
            "microburst_window_ms": float(window_ms),
            "microburst_buyers": float(len(buyers)),
            "microburst_buy_count": float(len(buys)),
            "microburst_buy_sol": buy_sol,
            "microburst_sell_sol": sell_sol,
            "microburst_unique_signatures": float(len(signatures)),
            "microburst_bundled_buys": float(bundled_buys),
            "microburst_max_same_signature": float(max_same_signature),
            "microburst_price_multiple": price_multiple,
        }
    )

    minimum_buyers = int(os.getenv("E4_MICROBURST_MIN_BUYERS", "7"))
    minimum_buy_sol = float(os.getenv("E4_MICROBURST_MIN_BUY_SOL", "12"))
    minimum_bundled = int(os.getenv("E4_MICROBURST_MIN_BUNDLED_BUYS", "6"))
    minimum_same_signature = int(
        os.getenv("E4_MICROBURST_MIN_SAME_SIGNATURE_BUYS", "3")
    )
    minimum_acceleration = float(
        os.getenv("E4_MICROBURST_MIN_PRICE_MULTIPLE", "1.55")
    )
    if sells or sell_sol > 0:
        return False, 0.0, 0.0, "sell appeared before E4 microburst confirmation", features
    if len(buyers) < minimum_buyers or buy_sol < minimum_buy_sol:
        return False, 0.0, 0.0, "insufficient observed E4 same-slot capital burst", features
    if bundled_buys < minimum_bundled or max_same_signature < minimum_same_signature:
        return False, 0.0, 0.0, "missing observed E4 multi-buy transaction structure", features
    if price_multiple < minimum_acceleration:
        return False, 0.0, 0.0, "insufficient observed E4 launch acceleration", features

    buyer_score = min(1.0, len(buyers) / 9.0)
    capital_score = min(1.0, buy_sol / 16.0)
    bundle_score = min(1.0, bundled_buys / 8.0)
    acceleration_score = min(1.0, price_multiple / 1.95)
    fdv_score = max(0.0, 1.0 - abs(fdv - 6_400.0) / 6_400.0)
    score = (
        0.20 * buyer_score
        + 0.25 * capital_score
        + 0.25 * bundle_score
        + 0.20 * acceleration_score
        + 0.10 * fdv_score
    )
    fraction = min(self.settings.max_position_fraction, 0.05 + score * 0.15)
    return True, score, fraction, "E4 observed same-slot microburst accepted", features


core.E4Policy.entry = _observed_microburst_entry

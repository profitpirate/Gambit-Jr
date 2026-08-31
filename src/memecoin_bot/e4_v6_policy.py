from __future__ import annotations

import os
import time
from typing import Any

from . import e4_v6_state as s

core = s.core


def entry(self: Any, state: Any):
    if state.complete or state.migrated or state.wallet_touched or state.created_ns is None:
        return False, 0.0, 0.0, "not an untouched live Pump curve", {}
    fdv = state.fdv_usd
    if not fdv or fdv <= 0 or fdv > min(self.settings.max_entry_fdv_usd, float(os.getenv("E4_V6_MAX_FDV_USD", "10000"))):
        return False, 0.0, 0.0, "outside observed E4 entry FDV", {}
    micro = s.microstructure(state)
    if not micro:
        return False, 0.0, 0.0, "launch microstructure unavailable", {}
    creator_score, creator_features = s.creator_signal(state.creator)
    funder_score, funder_features = s.funder_signal(state.creator)
    features: dict[str, Any] = {**state.features(), **micro, **creator_features, **funder_features, "fdv_usd": fdv}
    if micro["sell_count"] > 0:
        return False, 0.0, 0.0, "sell appeared before entry confirmation", features
    if creator_score <= -0.75 or funder_score <= -0.55:
        return False, 0.0, 0.0, "creator/funder history rejected", features

    age = micro["token_age_ms"]
    dev = micro["creator_buy_sol"]
    buyers = micro["public_buyers"]
    total = micro["total_buy_sol"]
    pm = micro["price_multiple"]
    efficiency = micro["capital_per_trade_sol"]
    identity = max(-1.0, min(1.0, creator_score + funder_score))

    # Three evidence-backed families. E4's same-window selections disproved a
    # universal 7-buyer/12-SOL/bundle gate, while current snipers commonly use
    # creator history, funder history, dev-buy bands and capital efficiency.
    identity_fast = age <= float(os.getenv("E4_IDENTITY_FAST_MAX_AGE_MS", "90")) and identity >= 0.45 and 0.03 <= dev <= 5.0
    public_burst = age <= float(os.getenv("E4_PUBLIC_BURST_MAX_AGE_MS", "300")) and dev >= 0.5 and buyers >= 3 and total >= 8.0 and pm >= 1.20
    capital_eff = age <= float(os.getenv("E4_CAPITAL_EFFICIENCY_MAX_AGE_MS", "500")) and buyers >= 2 and total >= 3.0 and efficiency >= 0.75 and pm >= 1.08 and identity >= -0.10
    if not (identity_fast or public_burst or capital_eff):
        return False, 0.0, 0.0, "no E4 V6 entry family confirmed", features

    identity01 = (identity + 1.0) / 2.0
    dev_score = 1.0 if 0.05 <= dev <= 4.5 else 0.35 if dev <= 6.0 else 0.0
    score = (0.24 * identity01 + 0.16 * dev_score + 0.16 * min(1.0, buyers / 8.0)
             + 0.17 * min(1.0, total / 12.0) + 0.15 * min(1.0, efficiency / 1.75)
             + 0.12 * min(1.0, max(0.0, pm - 1.0) / 0.75))
    if identity_fast:
        score = max(score, 0.76 + max(0.0, identity) * 0.16)
    if public_burst:
        score = max(score, 0.79 + 0.05 * min(1.0, total / 16.0))
    if capital_eff:
        score = max(score, 0.745 + 0.06 * min(1.0, efficiency / 1.75))
    score = max(0.0, min(0.995, score))
    if score < float(os.getenv("E4_V6_ENTRY_SCORE_MIN", "0.74")):
        return False, score, 0.0, "E4 V6 confidence below minimum", features

    tier, fraction = s.size_tier(score)
    fraction = min(fraction, self.settings.max_position_fraction, s.MAX_POSITION_FRACTION)
    family = "IDENTITY_FAST" if identity_fast else "PUBLIC_BURST" if public_burst else "CAPITAL_EFFICIENCY"
    first_partial = 0.20 if tier in s.HIGH_CONVICTION else 0.30
    s.POLICY_BY_MINT[state.mint] = {
        "score": score, "tier": tier, "target_fraction": fraction,
        "first_partial_fraction": first_partial, "family": family,
        "decided_ns": time.time_ns(),
    }
    features.update({
        "e4_v6_score": score, "e4_v6_size_fraction": fraction,
        "e4_v6_first_partial": first_partial,
        "e4_v6_identity_fast": float(identity_fast),
        "e4_v6_public_burst": float(public_burst),
        "e4_v6_capital_efficiency": float(capital_eff),
    })
    return True, score, fraction, f"E4 V6 {family} accepted ({tier})", features


_previous_exit = core.E4Policy.exit


def exit(self: Any, position: Any, state: Any):
    action, fraction, reason = _previous_exit(self, position, state)
    if action == "SELL_PARTIAL" and not position.first_partial_done:
        target = float((s.POLICY_BY_MINT.get(position.mint) or {}).get("first_partial_fraction") or 0.30)
        return action, target, f"{reason}; E4 confidence-tier first partial {target:.0%}"
    return action, fraction, reason


core.E4Policy.entry = entry
core.E4Policy.exit = exit

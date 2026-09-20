"""Post-certification V12 risk ladder.

The fresh 100-trade causal test remains frozen at 10%. This policy only becomes
eligible after the causal 100 passes and an explicit operator env flag is set.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from memecoin_bot.v12_creator_library import CreatorLibrary, causal_100_passed


FROZEN_FRACTION = 0.10
BASE_POSTCERT_FRACTION = 0.125
PROMOTED_FRACTION = 0.15
ELITE_FRACTION = 0.175
HARD_MAX_FRACTION = 0.20
MAX_CONCURRENT_POSITIONS = 2


@dataclass(frozen=True, slots=True)
class RiskDecision:
    fraction: float
    maximum_concurrent_positions: int
    blocked: bool
    reason: str
    postcert_active: bool


def _enabled(environment: Mapping[str, str]) -> bool:
    return str(environment.get("V12_POSTCERT_AGGRESSIVE_RISK_ENABLED", "")).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def decide_fraction(
    *,
    creator: str,
    causal_state: Mapping[str, object],
    library: CreatorLibrary,
    closed_equity_drawdown_fraction: float,
    environment: Mapping[str, str] | None = None,
) -> RiskDecision:
    env = environment if environment is not None else os.environ
    if not (_enabled(env) and causal_100_passed(causal_state)):
        return RiskDecision(
            fraction=FROZEN_FRACTION,
            maximum_concurrent_positions=MAX_CONCURRENT_POSITIONS,
            blocked=False,
            reason="frozen_100_trade_risk_or_postcert_not_enabled",
            postcert_active=False,
        )

    drawdown = max(0.0, float(closed_equity_drawdown_fraction))
    if drawdown >= 0.15:
        return RiskDecision(
            fraction=0.0,
            maximum_concurrent_positions=0,
            blocked=True,
            reason="postcert_drawdown_halt_15pct",
            postcert_active=True,
        )

    record = library.promoted.get(str(creator or ""))
    if record and record.elite:
        fraction = ELITE_FRACTION
        reason = "elite_promoted_creator"
    elif record and record.promoted:
        fraction = PROMOTED_FRACTION
        reason = "promoted_creator"
    else:
        fraction = BASE_POSTCERT_FRACTION
        reason = "base_postcert"

    if drawdown >= 0.10:
        fraction *= 0.50
        reason += "_drawdown_half"
    elif drawdown >= 0.05:
        fraction *= 0.75
        reason += "_drawdown_75pct"

    return RiskDecision(
        fraction=min(HARD_MAX_FRACTION, max(0.0, fraction)),
        maximum_concurrent_positions=MAX_CONCURRENT_POSITIONS,
        blocked=False,
        reason=reason,
        postcert_active=True,
    )

"""Liquidity/capacity sizing for V12 live entries."""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CapacityDecision:
    requested_sol: float
    allowed_sol: float
    price_impact_bps: float
    reserve_fraction: float
    blocked: bool
    reason: str


def constant_product_price_impact_bps(
    *,
    virtual_sol: float,
    virtual_tokens: float,
    spend_sol: float,
) -> float:
    if virtual_sol <= 0 or virtual_tokens <= 0 or spend_sol <= 0:
        return math.inf
    initial_price = virtual_sol / virtual_tokens
    k = virtual_sol * virtual_tokens
    tokens_after = k / (virtual_sol + spend_sol)
    tokens_out = virtual_tokens - tokens_after
    if tokens_out <= 0:
        return math.inf
    average_price = spend_sol / tokens_out
    return max(0.0, (average_price / initial_price - 1.0) * 10_000.0)


def max_spend_for_impact(
    *,
    virtual_sol: float,
    virtual_tokens: float,
    max_impact_bps: float,
    hard_cap_sol: float,
) -> float:
    if virtual_sol <= 0 or virtual_tokens <= 0 or max_impact_bps <= 0 or hard_cap_sol <= 0:
        return 0.0
    low, high = 0.0, hard_cap_sol
    for _ in range(64):
        middle = (low + high) / 2.0
        impact = constant_product_price_impact_bps(
            virtual_sol=virtual_sol,
            virtual_tokens=virtual_tokens,
            spend_sol=middle,
        )
        if impact <= max_impact_bps:
            low = middle
        else:
            high = middle
    return low


def decide_capacity(
    *,
    requested_sol: float,
    virtual_sol: float,
    virtual_tokens: float,
    min_virtual_sol: float = 5.0,
    max_price_impact_bps: float = 600.0,
    max_virtual_sol_fraction: float = 0.08,
    absolute_cap_sol: float = 20.0,
    minimum_trade_sol: float = 0.01,
) -> CapacityDecision:
    requested = max(0.0, float(requested_sol))
    if requested <= 0:
        return CapacityDecision(requested, 0.0, 0.0, 0.0, True, "non_positive_request")
    if virtual_sol < min_virtual_sol or virtual_tokens <= 0:
        return CapacityDecision(requested, 0.0, math.inf, 0.0, True, "insufficient_curve_liquidity")

    reserve_cap = max(0.0, virtual_sol * max_virtual_sol_fraction)
    impact_cap = max_spend_for_impact(
        virtual_sol=virtual_sol,
        virtual_tokens=virtual_tokens,
        max_impact_bps=max_price_impact_bps,
        hard_cap_sol=min(absolute_cap_sol, reserve_cap),
    )
    allowed = min(requested, reserve_cap, impact_cap, absolute_cap_sol)
    if allowed < minimum_trade_sol:
        return CapacityDecision(
            requested,
            0.0,
            math.inf,
            0.0,
            True,
            "capacity_below_minimum_trade",
        )
    impact = constant_product_price_impact_bps(
        virtual_sol=virtual_sol,
        virtual_tokens=virtual_tokens,
        spend_sol=allowed,
    )
    return CapacityDecision(
        requested,
        allowed,
        impact,
        allowed / virtual_sol,
        False,
        "capacity_ok" if allowed >= requested - 1e-12 else "capacity_clamped",
    )

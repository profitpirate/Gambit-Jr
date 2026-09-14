#!/usr/bin/env python3
"""Golden Management v2 — E4-informed, independently testable position management.

This module does NOT copy E4 trades and does NOT claim to recover E4's private
exit formula. It turns the behaviors that were strongly observed across E4's
recent and older wallet history into an aggressive paper-research management
policy for Golden's own selected trades.

Confirmed observations used as architecture:
- one-shot entry;
- variable/tiered conviction sizing;
- fast structural initial reduction (normally 30% standard / 20% high tier);
- losers usually live for less time / fewer sell legs than winners;
- strong winners are progressively scaled out and allowed to run;
- eventual full flattening.

Unconfirmed details (exact invalidation trigger, exact later scale-out trigger)
remain experimental and are deliberately isolated in RunnerGuardian so they can
be replaced without changing the Golden entry thesis.

IMPORTANT: position fractions in this module are for simulated/paper research.
The purpose of v2 is to measure both upside and drawdown under aggressive
conviction sizing rather than constrain the experiment to the old 1.85% stake.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque
from typing import Any, Mapping
import math

VERSION = "golden-management-v2-aggressive"


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(x)))


@dataclass(frozen=True)
class TierConfig:
    name: str
    position_fraction: float
    initial_fraction: float
    initial_target_ms: float
    max_hold_ms: float
    strength_steps: tuple[tuple[float, float], ...]
    trail_drawdown: float


TIERS: dict[str, TierConfig] = {
    # Aggressive PAPER-RESEARCH ladder. The experiment is explicitly intended
    # to learn what happens when conviction meaningfully changes capital at risk.
    # 1.85% remains only as the frozen v1 control and is not used here.
    "BASE": TierConfig(
        "BASE", 0.03, 0.30, 350.0, 4_000.0,
        ((0.15, 0.10), (0.30, 0.10)), 0.10,
    ),
    "MEDIUM": TierConfig(
        "MEDIUM", 0.075, 0.30, 350.0, 6_000.0,
        ((0.15, 0.10), (0.30, 0.10), (0.50, 0.10)), 0.11,
    ),
    "HIGH": TierConfig(
        "HIGH", 0.125, 0.20, 325.0, 10_000.0,
        ((0.15, 0.10), (0.30, 0.10), (0.50, 0.15), (0.80, 0.10)), 0.13,
    ),
    "EXCEPTIONAL": TierConfig(
        "EXCEPTIONAL", 0.20, 0.20, 300.0, 16_000.0,
        ((0.15, 0.10), (0.30, 0.10), (0.50, 0.15), (0.80, 0.10), (1.20, 0.10)), 0.15,
    ),
}


def conviction(decision: Mapping[str, Any]) -> dict[str, Any]:
    """Entry-time-only conviction score.

    Uses only causal information available before entry: buyer historical WR,
    causal sample size, and buyer diversity. No future P&L, E4 activity, or
    post-entry path enters the score.
    """
    rate = float(decision.get("prior_win_rate") or 0.0)
    appearances = max(0, int(decision.get("prior_appearances") or 0))
    buyers = max(0, int(decision.get("buyer_count") or len(decision.get("buyers") or [])))

    rate_component = 25.0 * _clip((rate - 0.70) / 0.20)
    sample_component = 0.0 if appearances <= 10 else 15.0 * _clip(
        math.log(appearances / 10.0) / math.log(10.0)
    )
    diversity_component = 10.0 * _clip((buyers - 1.0) / 4.0)
    score = round(_clip(50.0 + rate_component + sample_component + diversity_component, 0.0, 100.0), 3)

    if score >= 85.0:
        tier = "EXCEPTIONAL"
    elif score >= 75.0:
        tier = "HIGH"
    elif score >= 65.0:
        tier = "MEDIUM"
    else:
        tier = "BASE"
    cfg = TIERS[tier]
    return {
        "management_version": VERSION,
        "conviction_score": score,
        "conviction_tier": tier,
        "position_fraction": cfg.position_fraction,
        "initial_fraction": cfg.initial_fraction,
        "score_inputs": {
            "prior_win_rate": rate,
            "prior_appearances": appearances,
            "buyer_count": buyers,
        },
    }


@dataclass(frozen=True)
class Mark:
    t_ms: float
    return_fraction: float
    state_ns: int = 0
    vsol: float | None = None
    vtok: float | None = None


@dataclass(frozen=True)
class Action:
    kind: str  # INITIAL | SCALE_OUT | EXIT
    fraction_of_entry: float
    reason: str
    t_ms: float
    mark_return: float


@dataclass
class RunnerGuardian:
    """Stateful runner policy using only the observed post-entry return path.

    It deliberately avoids a single fixed stop. A temporary deep drawdown does
    not automatically kill a trade; failure requires either catastrophic loss
    or sustained deterioration. That design is motivated by E4 path evidence
    where some strong winners experienced large adverse excursions before
    recovering.
    """

    config: TierConfig
    initial_done: bool = False
    remaining_fraction: float = 1.0
    peak_return: float = -1e9
    trough_return: float = 1e9
    marks: deque[Mark] = field(default_factory=lambda: deque(maxlen=128))
    strength_taken: set[float] = field(default_factory=set)
    trail_reductions: int = 0
    closed: bool = False

    def _bounded_action(self, kind: str, requested: float, reason: str, mark: Mark) -> Action | None:
        amount = min(max(0.0, requested), max(0.0, self.remaining_fraction))
        if amount <= 1e-12:
            return None
        if kind == "EXIT":
            amount = self.remaining_fraction
        self.remaining_fraction = max(0.0, self.remaining_fraction - amount)
        if self.remaining_fraction <= 1e-9:
            self.closed = True
        return Action(kind, amount, reason, mark.t_ms, mark.return_fraction)

    def _trend(self, horizon_ms: float = 300.0) -> float | None:
        if len(self.marks) < 2:
            return None
        last = self.marks[-1]
        prior = None
        for item in reversed(self.marks):
            if last.t_ms - item.t_ms >= horizon_ms:
                prior = item
                break
        if prior is None:
            prior = self.marks[0]
        dt = max(1.0, last.t_ms - prior.t_ms)
        return (last.return_fraction - prior.return_fraction) * (horizon_ms / dt)

    def _strictly_deteriorating(self, n: int = 4) -> bool:
        if len(self.marks) < n:
            return False
        vals = [m.return_fraction for m in list(self.marks)[-n:]]
        return all(b < a for a, b in zip(vals, vals[1:]))

    def on_mark(self, mark: Mark) -> Action | None:
        if self.closed:
            return None
        self.marks.append(mark)
        self.peak_return = max(self.peak_return, mark.return_fraction)
        self.trough_return = min(self.trough_return, mark.return_fraction)

        # 1) Structural de-risking. This is intentionally not conditioned on
        # profitability; recent E4 path evidence shows initials both above and
        # below break-even.
        if not self.initial_done and mark.t_ms >= self.config.initial_target_ms:
            self.initial_done = True
            return self._bounded_action(
                "INITIAL", self.config.initial_fraction,
                f"STRUCTURAL_INITIAL_{int(round(self.config.initial_fraction * 100))}PCT",
                mark,
            )

        if not self.initial_done:
            return None

        trend = self._trend(300.0)
        drawdown_from_peak = self.peak_return - mark.return_fraction

        # 2) Absolute emergency guardrail. Wide by design: this is a catastrophic
        # fail-safe, not the normal exit policy. Aggressive sizing remains useful
        # research only if catastrophic path behavior is still observable.
        if mark.return_fraction <= -0.35:
            return self._bounded_action("EXIT", self.remaining_fraction, "EMERGENCY_CATASTROPHIC_DRAWDOWN", mark)

        # 3) Persistent failure. A single -10/-15% print is not enough.
        if (
            mark.t_ms >= 900.0
            and mark.return_fraction <= -0.18
            and (trend is not None and trend <= -0.04)
            and self._strictly_deteriorating(4)
        ):
            return self._bounded_action("EXIT", self.remaining_fraction, "PERSISTENT_DETERIORATION_DEEP", mark)

        if (
            mark.t_ms >= 1_200.0
            and mark.return_fraction <= -0.10
            and drawdown_from_peak >= 0.14
            and (trend is not None and trend <= -0.025)
            and self._strictly_deteriorating(4)
        ):
            return self._bounded_action("EXIT", self.remaining_fraction, "PERSISTENT_DETERIORATION", mark)

        # 4) Strength scale-outs. These are experimental Golden rules, not
        # claimed E4 thresholds. They create an initial-plus-runner structure.
        for level, fraction in self.config.strength_steps:
            if self.peak_return >= level and level not in self.strength_taken:
                self.strength_taken.add(level)
                return self._bounded_action(
                    "SCALE_OUT", fraction,
                    f"STRENGTH_MILESTONE_{int(round(level * 100))}PCT",
                    mark,
                )

        # 5) Peak give-back protection. Reduce, do not automatically flatten,
        # while the runner is still profitable. This lets strong trades breathe.
        if (
            self.peak_return >= 0.10
            and mark.return_fraction > 0.0
            and drawdown_from_peak >= self.config.trail_drawdown
            and self.trail_reductions < 2
        ):
            self.trail_reductions += 1
            return self._bounded_action(
                "SCALE_OUT", 0.15,
                f"PEAK_GIVEBACK_{self.trail_reductions}",
                mark,
            )

        # 6) Time is only a final ceiling, not the primary exit reason.
        if mark.t_ms >= self.config.max_hold_ms:
            return self._bounded_action("EXIT", self.remaining_fraction, "MAX_RUNNER_HOLD", mark)

        return None

    def summary(self) -> dict[str, Any]:
        return {
            "management_version": VERSION,
            "tier": self.config.name,
            "position_fraction": self.config.position_fraction,
            "initial_done": self.initial_done,
            "remaining_fraction": self.remaining_fraction,
            "peak_return": None if self.peak_return < -1e8 else self.peak_return,
            "trough_return": None if self.trough_return > 1e8 else self.trough_return,
            "strength_levels_taken": sorted(self.strength_taken),
            "trail_reductions": self.trail_reductions,
            "closed": self.closed,
            "marks_seen": len(self.marks),
        }


def self_test() -> None:
    low = conviction({"prior_win_rate": 0.70, "prior_appearances": 10, "buyer_count": 1})
    med = conviction({"prior_win_rate": 0.76, "prior_appearances": 30, "buyer_count": 2})
    high = conviction({"prior_win_rate": 0.84, "prior_appearances": 60, "buyer_count": 4})
    exceptional = conviction({"prior_win_rate": 0.90, "prior_appearances": 100, "buyer_count": 5})
    assert low["conviction_tier"] == "BASE" and math.isclose(low["position_fraction"], 0.03)
    assert exceptional["conviction_tier"] == "EXCEPTIONAL" and math.isclose(exceptional["position_fraction"], 0.20)
    assert TIERS["MEDIUM"].position_fraction == 0.075
    assert TIERS["HIGH"].position_fraction == 0.125
    assert TIERS["EXCEPTIONAL"].initial_fraction == 0.20

    # A temporary -17% drawdown must not be killed solely because it crossed -10%.
    g = RunnerGuardian(TIERS["HIGH"])
    assert g.on_mark(Mark(325, -0.05)) is not None  # initial
    assert g.on_mark(Mark(500, -0.17)) is None
    assert not g.closed

    # Sustained deep deterioration eventually exits.
    g.on_mark(Mark(900, -0.19))
    g.on_mark(Mark(1000, -0.21))
    g.on_mark(Mark(1100, -0.23))
    action = g.on_mark(Mark(1200, -0.26))
    assert action is not None and action.kind == "EXIT"

    # Strong runner gets progressive scale-out rather than forced 2s flatten.
    g2 = RunnerGuardian(TIERS["EXCEPTIONAL"])
    assert g2.on_mark(Mark(300, 0.02)).kind == "INITIAL"
    a = g2.on_mark(Mark(1000, 0.16))
    assert a and a.kind == "SCALE_OUT"
    assert not g2.closed
    print("GOLDEN_MANAGEMENT_V2_AGGRESSIVE_SELF_TEST_OK")


if __name__ == "__main__":
    self_test()

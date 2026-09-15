#!/usr/bin/env python3
"""Golden Management v2.1 experimental tournament policies.

This module is isolated from the frozen Phase-3 v2 campaign. It defines two new
paper-only management arms for the next true-online comparison:

1. PARTIALS_2S: keep the new 30/20% structural initial, then flatten the entire
   remaining position at 2 seconds.
2. FLOW_V2_1: keep the structural initial, but after 2 seconds continue holding
   only while live market participation/flow remains healthy.

E4 is an observational benchmark only and is never used as an entry signal.
Views are optional because the current Solana websocket feed does not expose a
reliable view counter; v2.1 must never invent or backfill unavailable view data.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any
import math

import golden_management_v2 as v2

VERSION = "golden-management-v2.1-flow-aware-v1"
ANCHOR_MS = 2_000.0
REEVALUATE_MS = 250.0


@dataclass(frozen=True)
class FlowEvent:
    t_ms: float
    kind: str              # BUY | SELL
    trader: str
    sol_amount: float
    token_amount: float
    return_fraction: float
    views: int | None = None


@dataclass
class FlowTracker:
    """Causal live-flow state from events observed after this launch arrived."""

    events: deque[FlowEvent] = field(default_factory=lambda: deque(maxlen=4096))
    wallet_tokens: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    holder_history: deque[tuple[float, int]] = field(default_factory=lambda: deque(maxlen=1024))
    return_history: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=2048))
    view_history: deque[tuple[float, int]] = field(default_factory=lambda: deque(maxlen=512))

    def ingest(self, event: FlowEvent) -> None:
        kind = event.kind.upper()
        if kind not in {"BUY", "SELL", "PUMPSWAP_BUY", "PUMPSWAP_SELL"}:
            return
        self.events.append(event)
        if event.trader and event.token_amount > 0:
            if "BUY" in kind:
                self.wallet_tokens[event.trader] += event.token_amount
            else:
                self.wallet_tokens[event.trader] = max(0.0, self.wallet_tokens[event.trader] - event.token_amount)
        holders = sum(1 for x in self.wallet_tokens.values() if x > 1e-12)
        self.holder_history.append((event.t_ms, holders))
        self.return_history.append((event.t_ms, event.return_fraction))
        if event.views is not None:
            self.view_history.append((event.t_ms, int(event.views)))

    @staticmethod
    def _value_at_or_before(rows: deque[tuple[float, Any]], target_ms: float, default: Any) -> Any:
        found = default
        for t, value in rows:
            if t <= target_ms:
                found = value
            else:
                break
        return found

    def _window_events(self, now_ms: float, horizon_ms: float) -> list[FlowEvent]:
        lo = now_ms - horizon_ms
        return [e for e in self.events if lo < e.t_ms <= now_ms]

    def snapshot(self, now_ms: float) -> dict[str, Any]:
        current_holders = self.holder_history[-1][1] if self.holder_history else 0
        h500 = self._value_at_or_before(self.holder_history, now_ms - 500.0, current_holders)
        h1000 = self._value_at_or_before(self.holder_history, now_ms - 1_000.0, current_holders)

        e500 = self._window_events(now_ms, 500.0)
        e1000 = self._window_events(now_ms, 1_000.0)
        prev500 = [e for e in self.events if now_ms - 1_000.0 < e.t_ms <= now_ms - 500.0]

        def flow(rows: list[FlowEvent]) -> tuple[float, float, float, int, int]:
            buy = sum(max(0.0, e.sol_amount) for e in rows if "BUY" in e.kind.upper())
            sell = sum(max(0.0, e.sol_amount) for e in rows if "SELL" in e.kind.upper())
            total = buy + sell
            buyers = len({e.trader for e in rows if e.trader and "BUY" in e.kind.upper()})
            sellers = len({e.trader for e in rows if e.trader and "SELL" in e.kind.upper()})
            return buy, sell, total, buyers, sellers

        b500, s500, v500, ub500, us500 = flow(e500)
        b1000, s1000, v1000, ub1000, us1000 = flow(e1000)
        _, _, vprev500, _, _ = flow(prev500)

        latest_ret = self.return_history[-1][1] if self.return_history else 0.0
        ret500 = self._value_at_or_before(self.return_history, now_ms - 500.0, latest_ret)
        ret1000 = self._value_at_or_before(self.return_history, now_ms - 1_000.0, latest_ret)

        view_growth_1000: int | None = None
        if self.view_history:
            latest_views = self.view_history[-1][1]
            old_views = self._value_at_or_before(self.view_history, now_ms - 1_000.0, latest_views)
            view_growth_1000 = latest_views - old_views

        return {
            "holder_count": current_holders,
            "holder_growth_500ms": current_holders - h500,
            "holder_growth_1000ms": current_holders - h1000,
            "buy_sol_500ms": b500,
            "sell_sol_500ms": s500,
            "net_buy_sol_500ms": b500 - s500,
            "buy_sol_1000ms": b1000,
            "sell_sol_1000ms": s1000,
            "net_buy_sol_1000ms": b1000 - s1000,
            "volume_sol_500ms": v500,
            "volume_sol_1000ms": v1000,
            "volume_acceleration_500ms": v500 - vprev500,
            "unique_buyers_500ms": ub500,
            "unique_sellers_500ms": us500,
            "unique_buyers_1000ms": ub1000,
            "unique_sellers_1000ms": us1000,
            "buy_ratio_1000ms": (b1000 / v1000) if v1000 > 0 else None,
            "return_fraction": latest_ret,
            "return_momentum_500ms": latest_ret - ret500,
            "return_momentum_1000ms": latest_ret - ret1000,
            "view_growth_1000ms": view_growth_1000,
        }


def continuation_score(snapshot: dict[str, Any]) -> tuple[int, list[str]]:
    """Transparent causal score. No future path and no E4 inputs."""
    score = 0
    reasons: list[str] = []

    hg1 = int(snapshot.get("holder_growth_1000ms") or 0)
    hg5 = int(snapshot.get("holder_growth_500ms") or 0)
    if hg1 > 0:
        score += 2; reasons.append("HOLDERS_GROWING_1S")
    elif hg1 < 0:
        score -= 2; reasons.append("HOLDERS_SHRINKING_1S")
    if hg5 > 0:
        score += 1; reasons.append("HOLDERS_GROWING_500MS")

    net = float(snapshot.get("net_buy_sol_1000ms") or 0.0)
    if net > 0:
        score += 2; reasons.append("NET_BUY_FLOW_POSITIVE")
    elif net < 0:
        score -= 2; reasons.append("NET_SELL_FLOW")

    ratio = snapshot.get("buy_ratio_1000ms")
    if ratio is not None:
        ratio = float(ratio)
        if ratio >= 0.60:
            score += 1; reasons.append("BUY_DOMINANCE")
        elif ratio <= 0.40:
            score -= 1; reasons.append("SELL_DOMINANCE")

    if float(snapshot.get("volume_acceleration_500ms") or 0.0) > 0:
        score += 1; reasons.append("VOLUME_ACCELERATING")
    if int(snapshot.get("unique_buyers_500ms") or 0) > int(snapshot.get("unique_sellers_500ms") or 0):
        score += 1; reasons.append("BUYER_BREADTH")

    mom = float(snapshot.get("return_momentum_500ms") or 0.0)
    if mom > 0.02:
        score += 1; reasons.append("PRICE_MOMENTUM_POSITIVE")
    elif mom < -0.05:
        score -= 1; reasons.append("PRICE_MOMENTUM_NEGATIVE")

    views = snapshot.get("view_growth_1000ms")
    if views is not None and int(views) > 0:
        score += 1; reasons.append("VIEWS_GROWING")

    return score, reasons


@dataclass
class TwoSecondPartialsGuardian:
    """New partial structure + hard 2-second remainder exit."""

    config: v2.TierConfig
    initial_done: bool = False
    closed: bool = False
    remaining_fraction: float = 1.0

    def on_mark(self, mark: v2.Mark) -> v2.Action | None:
        if self.closed:
            return None
        if not self.initial_done and mark.t_ms >= self.config.initial_target_ms:
            self.initial_done = True
            amount = min(self.config.initial_fraction, self.remaining_fraction)
            self.remaining_fraction -= amount
            return v2.Action("INITIAL", amount, f"STRUCTURAL_INITIAL_{int(round(amount*100))}PCT", mark.t_ms, mark.return_fraction)
        if mark.t_ms >= ANCHOR_MS:
            amount = self.remaining_fraction
            self.remaining_fraction = 0.0
            self.closed = True
            return v2.Action("EXIT", amount, "HARD_2S_REMAINDER_EXIT", mark.t_ms, mark.return_fraction)
        return None


@dataclass
class FlowAwareGuardian:
    """V2.1: 2 seconds is a decision anchor, not an automatic hold duration."""

    config: v2.TierConfig
    tracker: FlowTracker
    initial_done: bool = False
    anchor_passed: bool = False
    closed: bool = False
    remaining_fraction: float = 1.0
    peak_return: float = -math.inf
    last_eval_ms: float = 0.0
    decisions: list[dict[str, Any]] = field(default_factory=list)

    def _act(self, kind: str, amount: float, reason: str, mark: v2.Mark) -> v2.Action | None:
        amount = min(max(0.0, amount), self.remaining_fraction)
        if amount <= 1e-12:
            return None
        if kind == "EXIT":
            amount = self.remaining_fraction
        self.remaining_fraction = max(0.0, self.remaining_fraction - amount)
        if self.remaining_fraction <= 1e-9:
            self.closed = True
        return v2.Action(kind, amount, reason, mark.t_ms, mark.return_fraction)

    def on_mark(self, mark: v2.Mark) -> v2.Action | None:
        if self.closed:
            return None
        self.peak_return = max(self.peak_return, mark.return_fraction)

        if not self.initial_done and mark.t_ms >= self.config.initial_target_ms:
            self.initial_done = True
            return self._act("INITIAL", self.config.initial_fraction,
                             f"STRUCTURAL_INITIAL_{int(round(self.config.initial_fraction*100))}PCT", mark)

        if mark.t_ms < ANCHOR_MS:
            return None

        if not self.anchor_passed or mark.t_ms - self.last_eval_ms >= REEVALUATE_MS:
            self.anchor_passed = True
            self.last_eval_ms = mark.t_ms
            snap = self.tracker.snapshot(mark.t_ms)
            score, reasons = continuation_score(snap)
            self.decisions.append({"t_ms": mark.t_ms, "score": score, "reasons": reasons, "snapshot": snap})

            # At 2s the runner must earn continuation. Weak participation exits.
            if len(self.decisions) == 1 and score < 3:
                return self._act("EXIT", self.remaining_fraction, f"FLOW_2S_EXIT_SCORE_{score}", mark)

            # After continuation, sell-flow/holder deterioration can terminate it.
            if score <= -2:
                return self._act("EXIT", self.remaining_fraction, f"FLOW_BREAKDOWN_SCORE_{score}", mark)

            # Protect proven winners if live participation no longer supports the peak.
            drawdown = self.peak_return - mark.return_fraction
            if self.peak_return >= 0.15 and drawdown >= 0.12 and score < 2:
                return self._act("SCALE_OUT", 0.20, f"FLOW_PEAK_GIVEBACK_SCORE_{score}", mark)

        # Time remains only a safety ceiling.
        if mark.t_ms >= self.config.max_hold_ms:
            return self._act("EXIT", self.remaining_fraction, "MAX_RUNNER_HOLD_SAFETY_CEILING", mark)
        return None


TOURNAMENT_ARMS = (
    "FULL_2S_CONTROL",       # existing no-partial 2s benchmark
    "PARTIALS_2S",           # new 20/30% initial + remainder at 2s
    "V2_CURRENT",            # frozen current RunnerGuardian
    "FLOW_V2_1",             # participation-aware v2.1
    "E4_OBSERVATIONAL",      # benchmark only; never an entry signal
)


def self_test() -> None:
    cfg = v2.TIERS["HIGH"]
    g = TwoSecondPartialsGuardian(cfg)
    assert g.on_mark(v2.Mark(325, -0.05)).kind == "INITIAL"
    assert g.on_mark(v2.Mark(1_000, 0.10)) is None
    a = g.on_mark(v2.Mark(2_001, 0.12))
    assert a and a.kind == "EXIT" and g.closed

    ft = FlowTracker()
    ft.ingest(FlowEvent(1_100, "BUY", "a", 1.0, 100.0, 0.02))
    ft.ingest(FlowEvent(1_500, "BUY", "b", 1.0, 100.0, 0.06))
    ft.ingest(FlowEvent(1_900, "BUY", "c", 1.5, 100.0, 0.12))
    snap = ft.snapshot(2_000)
    score, _ = continuation_score(snap)
    assert score >= 3, (score, snap)
    print("GOLDEN_MANAGEMENT_V2_1_SELF_TEST_OK")


if __name__ == "__main__":
    self_test()

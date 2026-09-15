#!/usr/bin/env python3
"""Golden Management v2.1 tournament policies (paper research only).

The frozen Golden entry rule remains independent of E4.  V2.1 improves only
conviction/sizing and management using causal information available at the
moment of each decision.  Nothing in this file signs or broadcasts a trade.

Tournament arms:
- FULL_2S_CONTROL: same current-v2 conviction, full remainder at 2s, no partial.
- PARTIALS_2S: current-v2 conviction, 30/20% structural initial, remainder at 2s.
- V2_CURRENT: frozen v2 RunnerGuardian.
- FLOW_V2_1: cluster-adjusted conviction + path/participation-aware management.

E4 is observational only and never enters Golden qualification or sizing.
"""
from __future__ import annotations

import gzip
import json
import math
import statistics
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import golden_management_v2 as v2

VERSION = "golden-management-v2.1-flow-cluster-loss-hardened-v2"
ANCHOR_MS = 2_000.0
REEVALUATE_MS = 250.0
CORRELATION_MIN_COOCCURRENCES = 5
CORRELATION_MIN_SHARE = 0.60


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(x)))


def _finite(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


class PriorEvidenceBook:
    """Causal priors built only from launches that pre-date the live campaign.

    Pair co-occurrence is used only to avoid treating coordinated wallets as
    independent evidence.  Creator history is a weak modifier, never an entry
    signal by itself.
    """

    def __init__(self) -> None:
        self.wallet_seen: Counter[str] = Counter()
        self.pair_seen: Counter[tuple[str, str]] = Counter()
        self.creator_seen: Counter[str] = Counter()
        self.creator_wins: Counter[str] = Counter()

    @classmethod
    def from_frozen_corpus(cls, path: Path) -> "PriorEvidenceBook":
        book = cls()
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                row = json.loads(line)
                buyers = sorted(set(str(x) for x in (row.get("first_outside_buyers_10ms") or []) if x))
                for w in buyers:
                    book.wallet_seen[w] += 1
                for i, a in enumerate(buyers):
                    for b in buyers[i + 1:]:
                        book.pair_seen[(a, b)] += 1
                creator = str(row.get("creator") or "")
                if creator:
                    won = _finite(row.get("paper_10ms_hold_2000ms_pnl_sol")) > 0
                    book.creator_seen[creator] += 1
                    book.creator_wins[creator] += int(won)
        return book

    def correlated(self, a: str, b: str) -> bool:
        if a == b:
            return True
        x, y = sorted((a, b))
        pair = self.pair_seen[(x, y)]
        denom = min(self.wallet_seen[a], self.wallet_seen[b])
        return bool(pair >= CORRELATION_MIN_COOCCURRENCES and denom > 0 and pair / denom >= CORRELATION_MIN_SHARE)

    def cluster_profile(self, buyers: Iterable[str]) -> dict[str, Any]:
        rows = list(dict.fromkeys(str(x) for x in buyers if x))
        parent = {w: w for w in rows}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for i, a in enumerate(rows):
            for b in rows[i + 1:]:
                if self.correlated(a, b):
                    union(a, b)
        groups: dict[str, list[str]] = defaultdict(list)
        for w in rows:
            groups[find(w)].append(w)
        clusters = list(groups.values())
        return {
            "raw_buyer_count": len(rows),
            "independent_groups": len(clusters),
            "redundant_wallets": max(0, len(rows) - len(clusters)),
            "largest_cluster": max((len(x) for x in clusters), default=0),
            "clusters": clusters,
        }

    def creator_profile(self, creator: str) -> dict[str, Any]:
        n = int(self.creator_seen[creator]) if creator else 0
        w = int(self.creator_wins[creator]) if creator else 0
        return {"appearances": n, "wins": w, "win_rate": (w / n if n else None)}


@dataclass(frozen=True)
class FlowEvent:
    t_ms: float
    kind: str
    trader: str
    sol_amount: float
    token_amount: float
    return_fraction: float
    is_creator: bool = False
    vsol: float | None = None
    vtok: float | None = None
    views: int | None = None


@dataclass
class FlowTracker:
    """Causal participation/flow state observed after the launch arrived."""

    events: deque[FlowEvent] = field(default_factory=lambda: deque(maxlen=8192))
    wallet_tokens: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    holder_history: deque[tuple[float, int]] = field(default_factory=lambda: deque(maxlen=2048))
    return_history: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=4096))
    reserve_history: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=4096))
    view_history: deque[tuple[float, int]] = field(default_factory=lambda: deque(maxlen=1024))

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
        self.holder_history.append((event.t_ms, sum(1 for x in self.wallet_tokens.values() if x > 1e-12)))
        self.return_history.append((event.t_ms, event.return_fraction))
        if event.vsol is not None and math.isfinite(float(event.vsol)):
            self.reserve_history.append((event.t_ms, float(event.vsol)))
        if event.views is not None:
            self.view_history.append((event.t_ms, int(event.views)))

    @staticmethod
    def _at(rows: deque[tuple[float, Any]], target: float, default: Any) -> Any:
        value = default
        for t, v in rows:
            if t <= target:
                value = v
            else:
                break
        return value

    def _window(self, now: float, horizon: float) -> list[FlowEvent]:
        lo = now - horizon
        return [e for e in self.events if lo < e.t_ms <= now]

    @staticmethod
    def _flow(rows: list[FlowEvent]) -> dict[str, Any]:
        buys = [e for e in rows if "BUY" in e.kind.upper()]
        sells = [e for e in rows if "SELL" in e.kind.upper()]
        buy_sol = sum(max(0.0, e.sol_amount) for e in buys)
        sell_sol = sum(max(0.0, e.sol_amount) for e in sells)
        sizes = [max(0.0, e.sol_amount) for e in buys if e.sol_amount > 0]
        total = buy_sol + sell_sol
        by_buyer: Counter[str] = Counter()
        for e in buys:
            if e.trader:
                by_buyer[e.trader] += max(0.0, e.sol_amount)
        top_share = (max(by_buyer.values()) / buy_sol) if buy_sol and by_buyer else None
        hhi = (sum((x / buy_sol) ** 2 for x in by_buyer.values())) if buy_sol else None
        return {
            "buy_sol": buy_sol,
            "sell_sol": sell_sol,
            "volume_sol": total,
            "net_buy_sol": buy_sol - sell_sol,
            "unique_buyers": len({e.trader for e in buys if e.trader}),
            "unique_sellers": len({e.trader for e in sells if e.trader}),
            "buy_ratio": (buy_sol / total) if total > 0 else None,
            "median_buy_sol": statistics.median(sizes) if sizes else None,
            "max_buy_sol": max(sizes) if sizes else None,
            "top_buyer_share": top_share,
            "buyer_hhi": hhi,
            "events": len(rows),
            "creator_sell_sol": sum(max(0.0, e.sol_amount) for e in sells if e.is_creator),
        }

    def snapshot(self, now_ms: float) -> dict[str, Any]:
        holders = self.holder_history[-1][1] if self.holder_history else 0
        h500 = self._at(self.holder_history, now_ms - 500.0, holders)
        h1000 = self._at(self.holder_history, now_ms - 1_000.0, holders)
        f500 = self._flow(self._window(now_ms, 500.0))
        f1000 = self._flow(self._window(now_ms, 1_000.0))
        prev500 = self._flow([e for e in self.events if now_ms - 1_000.0 < e.t_ms <= now_ms - 500.0])
        latest_ret = self.return_history[-1][1] if self.return_history else 0.0
        r500 = self._at(self.return_history, now_ms - 500.0, latest_ret)
        r1000 = self._at(self.return_history, now_ms - 1_000.0, latest_ret)
        latest_vsol = self.reserve_history[-1][1] if self.reserve_history else None
        v500 = self._at(self.reserve_history, now_ms - 500.0, latest_vsol) if latest_vsol is not None else None
        v1000 = self._at(self.reserve_history, now_ms - 1_000.0, latest_vsol) if latest_vsol is not None else None
        view_growth = None
        if self.view_history:
            latest = self.view_history[-1][1]
            old = self._at(self.view_history, now_ms - 1_000.0, latest)
            view_growth = latest - old
        return {
            "holder_count": holders,
            "holder_growth_500ms": holders - h500,
            "holder_growth_1000ms": holders - h1000,
            "buy_sol_500ms": f500["buy_sol"],
            "sell_sol_500ms": f500["sell_sol"],
            "net_buy_sol_500ms": f500["net_buy_sol"],
            "buy_sol_1000ms": f1000["buy_sol"],
            "sell_sol_1000ms": f1000["sell_sol"],
            "net_buy_sol_1000ms": f1000["net_buy_sol"],
            "volume_sol_500ms": f500["volume_sol"],
            "volume_sol_1000ms": f1000["volume_sol"],
            "volume_acceleration_500ms": f500["volume_sol"] - prev500["volume_sol"],
            "unique_buyers_500ms": f500["unique_buyers"],
            "unique_sellers_500ms": f500["unique_sellers"],
            "unique_buyers_1000ms": f1000["unique_buyers"],
            "unique_sellers_1000ms": f1000["unique_sellers"],
            "buy_ratio_1000ms": f1000["buy_ratio"],
            "top_buyer_share_1000ms": f1000["top_buyer_share"],
            "buyer_hhi_1000ms": f1000["buyer_hhi"],
            "median_buy_sol_1000ms": f1000["median_buy_sol"],
            "max_buy_sol_1000ms": f1000["max_buy_sol"],
            "creator_sell_sol_1000ms": f1000["creator_sell_sol"],
            "event_velocity_1000ms": f1000["events"],
            "return_fraction": latest_ret,
            "return_momentum_500ms": latest_ret - r500,
            "return_momentum_1000ms": latest_ret - r1000,
            "vsol_acceleration_500ms": (latest_vsol - v500) if latest_vsol is not None and v500 is not None else None,
            "vsol_acceleration_1000ms": (latest_vsol - v1000) if latest_vsol is not None and v1000 is not None else None,
            "view_growth_1000ms": view_growth,
        }


def continuation_score(s: Mapping[str, Any]) -> tuple[int, list[str]]:
    """Transparent live continuation score; positive = demand is earning time."""
    score = 0
    reasons: list[str] = []
    hg1 = int(s.get("holder_growth_1000ms") or 0)
    hg5 = int(s.get("holder_growth_500ms") or 0)
    if hg1 > 0: score += 2; reasons.append("HOLDERS_GROWING_1S")
    elif hg1 < 0: score -= 2; reasons.append("HOLDERS_SHRINKING_1S")
    if hg5 > 0: score += 1; reasons.append("HOLDERS_GROWING_500MS")

    net = _finite(s.get("net_buy_sol_1000ms"))
    if net > 0: score += 2; reasons.append("NET_BUY_FLOW_POSITIVE")
    elif net < 0: score -= 2; reasons.append("NET_SELL_FLOW")

    ratio = s.get("buy_ratio_1000ms")
    if ratio is not None:
        if float(ratio) >= 0.60: score += 1; reasons.append("BUY_DOMINANCE")
        elif float(ratio) <= 0.40: score -= 1; reasons.append("SELL_DOMINANCE")

    if _finite(s.get("volume_acceleration_500ms")) > 0: score += 1; reasons.append("VOLUME_ACCELERATING")
    if int(s.get("unique_buyers_500ms") or 0) > int(s.get("unique_sellers_500ms") or 0):
        score += 1; reasons.append("BUYER_BREADTH")
    if int(s.get("event_velocity_1000ms") or 0) >= 5: score += 1; reasons.append("ACTIVE_TAPE")

    mom = _finite(s.get("return_momentum_500ms"))
    if mom > 0.02: score += 1; reasons.append("PRICE_MOMENTUM_POSITIVE")
    elif mom < -0.05: score -= 1; reasons.append("PRICE_MOMENTUM_NEGATIVE")

    top = s.get("top_buyer_share_1000ms")
    hhi = s.get("buyer_hhi_1000ms")
    if top is not None and float(top) >= 0.70: score -= 2; reasons.append("WHALE_CONCENTRATION")
    elif hhi is not None and float(hhi) <= 0.35 and int(s.get("unique_buyers_1000ms") or 0) >= 3:
        score += 1; reasons.append("DISTRIBUTED_BUY_FLOW")

    if _finite(s.get("creator_sell_sol_1000ms")) > 0:
        score -= 3; reasons.append("CREATOR_SELLING")
    accel = s.get("vsol_acceleration_500ms")
    if accel is not None and float(accel) > 0: score += 1; reasons.append("RESERVES_ACCELERATING")
    views = s.get("view_growth_1000ms")
    if views is not None and int(views) > 0: score += 1; reasons.append("VIEWS_GROWING")
    return score, reasons


def enhanced_conviction(decision: Mapping[str, Any], book: PriorEvidenceBook | None) -> dict[str, Any]:
    """V2.1 conviction with correlated-wallet and creator adjustments.

    The original Golden qualification is left untouched.  This function only
    determines how much simulated capital FLOW_V2_1 assigns after qualification.
    """
    rate = _finite(decision.get("prior_win_rate"))
    appearances = max(0, int(decision.get("prior_appearances") or 0))
    buyers = list(dict.fromkeys(str(x) for x in (decision.get("buyers") or []) if x))
    creator = str(decision.get("creator") or "")
    clusters = book.cluster_profile(buyers) if book else {
        "raw_buyer_count": len(buyers), "independent_groups": len(buyers),
        "redundant_wallets": 0, "largest_cluster": 1 if buyers else 0, "clusters": [[x] for x in buyers],
    }
    cp = book.creator_profile(creator) if book else {"appearances": 0, "wins": 0, "win_rate": None}
    independent = int(clusters["independent_groups"])

    rate_component = 25.0 * _clip((rate - 0.70) / 0.20, 0.0, 1.0)
    sample_component = 0.0 if appearances <= 10 else 15.0 * _clip(math.log(appearances / 10.0) / math.log(10.0), 0.0, 1.0)
    diversity_component = 10.0 * _clip((independent - 1.0) / 4.0, 0.0, 1.0)
    cluster_penalty = min(15.0, 4.0 * int(clusters["redundant_wallets"]))
    creator_component = 0.0
    if int(cp["appearances"]) >= 5 and cp["win_rate"] is not None:
        cwr = float(cp["win_rate"])
        if cwr >= 0.60: creator_component += 4.0
        if cwr <= 0.30: creator_component -= 8.0
    early_reserve_delta = _finite(decision.get("early_vsol_delta"))
    reserve_component = 3.0 if early_reserve_delta > 0.50 else (-3.0 if early_reserve_delta < -0.20 else 0.0)
    score = round(_clip(50.0 + rate_component + sample_component + diversity_component + creator_component + reserve_component - cluster_penalty, 0.0, 100.0), 3)

    if score >= 85: tier = "EXCEPTIONAL"
    elif score >= 75: tier = "HIGH"
    elif score >= 65: tier = "MEDIUM"
    else: tier = "BASE"
    cfg = v2.TIERS[tier]
    return {
        "management_version": VERSION,
        "conviction_score": score,
        "conviction_tier": tier,
        "position_fraction": cfg.position_fraction,
        "initial_fraction": cfg.initial_fraction,
        "cluster_profile": clusters,
        "creator_profile": cp,
        "score_inputs": {
            "prior_win_rate": rate,
            "prior_appearances": appearances,
            "raw_buyer_count": len(buyers),
            "independent_groups": independent,
            "cluster_penalty": cluster_penalty,
            "creator_component": creator_component,
            "early_vsol_delta": early_reserve_delta,
            "reserve_component": reserve_component,
        },
    }


@dataclass
class FullTwoSecondGuardian:
    closed: bool = False
    remaining_fraction: float = 1.0
    def on_mark(self, mark: v2.Mark) -> v2.Action | None:
        if self.closed or mark.t_ms < ANCHOR_MS:
            return None
        self.closed = True; self.remaining_fraction = 0.0
        return v2.Action("EXIT", 1.0, "FULL_2S_EXIT", mark.t_ms, mark.return_fraction)


@dataclass
class TwoSecondPartialsGuardian:
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
            amount = self.remaining_fraction; self.remaining_fraction = 0.0; self.closed = True
            return v2.Action("EXIT", amount, "HARD_2S_REMAINDER_EXIT", mark.t_ms, mark.return_fraction)
        return None


@dataclass
class FlowAwareGuardian:
    config: v2.TierConfig
    tracker: FlowTracker
    initial_done: bool = False
    initial_deferred: bool = False
    closed: bool = False
    remaining_fraction: float = 1.0
    peak_return: float = -math.inf
    last_eval_ms: float = 0.0
    decisions: list[dict[str, Any]] = field(default_factory=list)

    def _act(self, kind: str, amount: float, reason: str, mark: v2.Mark) -> v2.Action | None:
        amount = min(max(0.0, amount), self.remaining_fraction)
        if amount <= 1e-12: return None
        if kind == "EXIT": amount = self.remaining_fraction
        self.remaining_fraction = max(0.0, self.remaining_fraction - amount)
        if self.remaining_fraction <= 1e-9: self.closed = True
        return v2.Action(kind, amount, reason, mark.t_ms, mark.return_fraction)

    @staticmethod
    def _profit_floor(peak: float) -> float | None:
        if peak >= 1.00: return max(0.55, peak - 0.30)
        if peak >= 0.50: return max(0.25, peak - 0.20)
        if peak >= 0.30: return max(0.12, peak - 0.15)
        if peak >= 0.15: return max(0.03, peak - 0.12)
        return None

    def on_mark(self, mark: v2.Mark) -> v2.Action | None:
        if self.closed: return None
        self.peak_return = max(self.peak_return, mark.return_fraction)
        snap = self.tracker.snapshot(mark.t_ms)
        score, reasons = continuation_score(snap)

        # Early loss control: bad path + bad participation is not given extra time.
        if mark.t_ms >= 500 and mark.return_fraction <= -0.12 and score <= -1:
            return self._act("EXIT", self.remaining_fraction, f"EARLY_FLOW_INVALIDATION_{score}", mark)
        if mark.return_fraction <= -0.28:
            return self._act("EXIT", self.remaining_fraction, "CATASTROPHIC_DRAWDOWN_28PCT", mark)

        # Path-aware initial.  Do not mechanically crystallise a deep red print
        # when participation is still strong enough to justify recovery time.
        if not self.initial_done and mark.t_ms >= self.config.initial_target_ms:
            if mark.return_fraction <= -0.05 and score >= 2 and mark.t_ms < ANCHOR_MS:
                self.initial_deferred = True
            else:
                self.initial_done = True
                return self._act("INITIAL", self.config.initial_fraction,
                                 f"PATH_AWARE_INITIAL_{int(round(self.config.initial_fraction*100))}PCT_SCORE_{score}", mark)
        if self.initial_deferred and not self.initial_done and mark.t_ms < ANCHOR_MS and mark.return_fraction >= -0.01:
            self.initial_done = True
            return self._act("INITIAL", self.config.initial_fraction,
                             f"RECOVERY_INITIAL_{int(round(self.config.initial_fraction*100))}PCT", mark)

        if mark.t_ms < ANCHOR_MS:
            return None

        if mark.t_ms - self.last_eval_ms >= REEVALUATE_MS or not self.decisions:
            self.last_eval_ms = mark.t_ms
            self.decisions.append({"t_ms": mark.t_ms, "score": score, "reasons": reasons, "snapshot": snap})

            # 2s is the default cash-out point.  Continuation must be earned.
            if len(self.decisions) == 1:
                if mark.return_fraction < -0.10 and score < 5:
                    return self._act("EXIT", self.remaining_fraction, f"FLOW_2S_RED_EXIT_SCORE_{score}", mark)
                if score < 3:
                    return self._act("EXIT", self.remaining_fraction, f"FLOW_2S_EXIT_SCORE_{score}", mark)

            # Once extended, loss containment is much tighter than v2.
            if mark.return_fraction <= -0.08 and score <= 0:
                return self._act("EXIT", self.remaining_fraction, f"POST2S_LOSS_CONTAINMENT_{score}", mark)
            if score <= -2:
                return self._act("EXIT", self.remaining_fraction, f"FLOW_BREAKDOWN_SCORE_{score}", mark)

            floor = self._profit_floor(self.peak_return)
            if floor is not None and mark.return_fraction <= floor and score < 3:
                return self._act("EXIT", self.remaining_fraction,
                                 f"PROFIT_FLOOR_PEAK_{self.peak_return:.3f}_FLOOR_{floor:.3f}_SCORE_{score}", mark)

            # Take modest strength profits only when the tape is not accelerating.
            if self.peak_return >= 0.30 and self.remaining_fraction > 0.55 and score < 4:
                return self._act("SCALE_OUT", 0.10, f"FLOW_STRENGTH_30_SCORE_{score}", mark)
            if self.peak_return >= 0.60 and self.remaining_fraction > 0.40 and score < 5:
                return self._act("SCALE_OUT", 0.10, f"FLOW_STRENGTH_60_SCORE_{score}", mark)

        if mark.t_ms >= self.config.max_hold_ms:
            return self._act("EXIT", self.remaining_fraction, "MAX_RUNNER_HOLD_SAFETY_CEILING", mark)
        return None


TOURNAMENT_ARMS = ("FULL_2S_CONTROL", "PARTIALS_2S", "V2_CURRENT", "FLOW_V2_1")
E4_BENCHMARK = "E4_OBSERVATIONAL_ONLY"


def self_test() -> None:
    cfg = v2.TIERS["HIGH"]
    g = TwoSecondPartialsGuardian(cfg)
    assert g.on_mark(v2.Mark(325, -0.05)).kind == "INITIAL"
    assert g.on_mark(v2.Mark(1_000, 0.10)) is None
    assert g.on_mark(v2.Mark(2_001, 0.12)).kind == "EXIT"

    ft = FlowTracker()
    ft.ingest(FlowEvent(1_100, "BUY", "a", 1.0, 100, 0.01, vsol=30.5))
    ft.ingest(FlowEvent(1_450, "BUY", "b", 1.2, 100, 0.05, vsol=31.0))
    ft.ingest(FlowEvent(1_800, "BUY", "c", 1.5, 100, 0.11, vsol=32.0))
    score, _ = continuation_score(ft.snapshot(2_000))
    assert score >= 3

    bad = FlowTracker()
    bad.ingest(FlowEvent(1_100, "BUY", "a", .2, 100, .02))
    bad.ingest(FlowEvent(1_500, "SELL", "a", 1.0, 100, -.04))
    bad.ingest(FlowEvent(1_900, "SELL", "b", 1.0, 100, -.10))
    gg = FlowAwareGuardian(cfg, bad)
    action = gg.on_mark(v2.Mark(2_000, -.10))
    assert action is not None and action.kind == "EXIT"

    # Correlated wallets count as one independent source.
    b = PriorEvidenceBook();
    for w in ("a", "b"): b.wallet_seen[w] = 10
    b.pair_seen[("a", "b")] = 8
    cp = b.cluster_profile(["a", "b"])
    assert cp["independent_groups"] == 1 and cp["redundant_wallets"] == 1
    print("GOLDEN_MANAGEMENT_V2_1_HARDENED_SELF_TEST_OK")


if __name__ == "__main__":
    self_test()

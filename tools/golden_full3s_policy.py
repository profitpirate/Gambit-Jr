"""Causal FULL_3S_V2 selection. Research hypotheses, not validated alpha.

No RPC, signing, network calls, E4 signals or timer changes. Outcomes are from
FULL_3S paper executions at the SAME cost configuration and a comparable size.
Never seed this index with the older 2-second, 0.0555-SOL reputation labels.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from typing import Any, Mapping

E4 = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"
DAY_NS = 86_400_000_000_000


def finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def clean_buyers(d: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(sorted({str(w) for w in d.get("buyers", []) if w and w not in (d.get("creator"), E4)}))


def size_bucket(budget: float) -> int:
    if not finite(budget) or budget <= 0:
        raise ValueError("invalid evidence notional")
    return math.floor(math.log2(budget / .15))


def cohort_key(buyers: tuple[str, ...]) -> str:
    return hashlib.sha256(json.dumps(buyers, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class PolicyConfig:
    version: str = "FULL_3S_V2_SELECTION_1"
    buyer_min_unique_coins: int = 20
    cohort_min_unique_coins: int = 10
    creator_min_unique_coins: int = 10
    max_history_per_key: int = 256
    max_age_days: int = 7
    half_life_hours: int = 24
    shrinkage_observations: int = 10
    max_quote_age_ms: int = 500
    max_impact_bps: int = 300
    max_roundtrip_drag_bps: int = 1500
    min_expected_net_bps: int = 25
    tail_loss_return: float = -.15
    max_tail_frequency: float = .35
    minimum_history_mode: str = "FROZEN_GOLDEN_FALLBACK"

    def __post_init__(self) -> None:
        for k, v in asdict(self).items():
            if isinstance(v, (int, float)) and (not finite(v) or (k != "tail_loss_return" and v < 0)):
                raise ValueError(f"invalid policy {k}")
        if min(self.buyer_min_unique_coins, self.cohort_min_unique_coins, self.creator_min_unique_coins, self.max_history_per_key, self.max_age_days, self.half_life_hours) <= 0:
            raise ValueError("history bounds must be positive")
        if not -1 <= self.tail_loss_return < 0 or not 0 < self.max_tail_frequency <= 1:
            raise ValueError("invalid downside settings")
        if self.minimum_history_mode != "FROZEN_GOLDEN_FALLBACK":
            raise ValueError("unfrozen cold-start policy")

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Outcome:
    mint: str
    buyers: tuple[str, ...]
    creator: str
    decision_ns: int
    closed_ns: int
    available_ns: int
    budget_sol: float
    net_pnl_sol: float
    gross_pnl_sol: float
    cost_hash: str
    source: str = "FORWARD_OBSERVED_PAPER"
    recovered: bool = False
    horizon_ms: int = 3000
    family: str = "FULL"


class EvidenceIndex:
    """Bounded per-key index; unique-coin pooling avoids wallet vote inflation.

    Wallet co-appearance is NOT a claim about common ownership. Exact co-buyer
    sets get separate cohort statistics; wallet pooling counts each coin once.
    Future or incident-reconstructed outcomes cannot enter a live decision.
    """
    def __init__(self, cost_hash: str, config: PolicyConfig | None = None, *, fixture_mode: bool = False):
        self.cost_hash = cost_hash
        self.c = config or PolicyConfig()
        self.fixture_mode = fixture_mode
        self.rows: dict[str, Outcome] = {}
        self.index: dict[tuple[str, str, int], deque[str]] = defaultdict(deque)
        self.refs: dict[str, int] = defaultdict(int)
        self.last_available_ns = -1
        self.generation = 0

    def add(self, row: Outcome) -> None:
        expected_source = "SYNTHETIC_FIXTURE" if self.fixture_mode else "FORWARD_OBSERVED_PAPER"
        if row.source != expected_source or row.recovered:
            raise ValueError("non-forward or wrong-mode evidence")
        if row.family != "FULL" or row.horizon_ms != 3000 or row.cost_hash != self.cost_hash:
            raise ValueError("label economics mismatch")
        if not (0 <= row.decision_ns < row.closed_ns <= row.available_ns) or row.available_ns < self.last_available_ns:
            raise ValueError("noncausal outcome publication")
        if not row.mint or any(not finite(v) for v in (row.budget_sol, row.net_pnl_sol, row.gross_pnl_sol)):
            raise ValueError("invalid label")
        if row.budget_sol <= 0 or row.net_pnl_sol > row.gross_pnl_sol + 1e-8:
            raise ValueError("invalid label costs")
        if not row.buyers or tuple(sorted(set(row.buyers))) != row.buyers or E4 in row.buyers or row.creator in row.buyers:
            raise ValueError("invalid buyer cohort")
        if row.mint in self.rows:
            if self.rows[row.mint] != row:
                raise ValueError("conflicting evidence for same coin")
            return
        b = size_bucket(row.budget_sol)
        keys = [("buyer", w, b) for w in row.buyers]
        keys.append(("cohort", cohort_key(row.buyers), b))
        if row.creator:
            keys.append(("creator", row.creator, b))
        self.rows[row.mint] = row
        for key in keys:
            q = self.index[key]
            q.append(row.mint); self.refs[row.mint] += 1
            while len(q) > self.c.max_history_per_key:
                old = q.popleft(); self.refs[old] -= 1
                if not self.refs[old]:
                    self.refs.pop(old); self.rows.pop(old, None)
        self.last_available_ns = row.available_ns
        self.generation += 1

    def prune(self, asof_ns: int) -> None:
        """Call on the cold path. Do not remove history needed for old decisions."""
        cutoff = asof_ns - self.c.max_age_days * DAY_NS
        for key, q in list(self.index.items()):
            while q and self.rows[q[0]].available_ns < cutoff:
                old = q.popleft(); self.refs[old] -= 1
                if not self.refs[old]:
                    self.refs.pop(old); self.rows.pop(old, None)
            if not q:
                del self.index[key]
        self.generation += 1

    def stats(self, kind: str, names: tuple[str, ...], budget: float, asof_ns: int, exclude_mint: str = "") -> dict[str, Any]:
        bucket = size_bucket(budget)
        ids: set[str] = set()
        for name in names:
            ids.update(self.index.get((kind, name, bucket), ()))
        rows = [self.rows[i] for i in sorted(ids) if i != exclude_mint and
                asof_ns - self.c.max_age_days * DAY_NS <= self.rows[i].available_ns < asof_ns]
        if not rows:
            return {"n": 0, "status": "UNKNOWN", "mean_net_return": None, "effective_n": 0.0}
        ws = [2 ** (-(asof_ns - r.available_ns) / (self.c.half_life_hours * 3_600_000_000_000)) for r in rows]
        ys = [r.net_pnl_sol / r.budget_sol for r in rows]
        sw = sum(ws); neff = sw * sw / sum(w*w for w in ws)
        mean = sum(w*y for w, y in zip(ws, ys)) / sw
        variance = sum(w*(y-mean)**2 for w, y in zip(ws, ys)) / sw
        se = math.sqrt(variance / max(1.0, neff))
        shrunk = mean * neff / (neff + self.c.shrinkage_observations)
        ordered = sorted(ys)
        return {"n": len(rows), "status": "AVAILABLE", "effective_n": neff,
                "mean_net_return": mean, "shrunk_mean_net_return": shrunk,
                "heuristic_lower_net": shrunk - se, "heuristic_upper_net": mean + 1.96*se,
                "win_rate": sum(y > 0 for y in ys) / len(ys),
                "tail_frequency": sum(y <= self.c.tail_loss_return for y in ys) / len(ys),
                "worst_return": min(ys), "worst_quintile_mean": sum(ordered[:max(1, math.ceil(len(ys)*.2))])/max(1, math.ceil(len(ys)*.2)),
                "unique_coin_count": len(ids), "label": "NET_FULL_3S_SAME_COST_SAME_LOG2_SIZE_BUCKET",
                "uncertainty_note": "Heuristic shrinkage/SE only; not a confidence guarantee for dependent coins"}

    def snapshot(self) -> dict[str, Any]:
        return {"cost_hash": self.cost_hash, "config": asdict(self.c), "fixture_mode": self.fixture_mode,
                "rows": [asdict(r) for r in sorted(self.rows.values(), key=lambda r: (r.available_ns, r.mint))]}

    @classmethod
    def restore(cls, raw: Mapping[str, Any]) -> EvidenceIndex:
        obj = cls(raw["cost_hash"], PolicyConfig(**raw["config"]), fixture_mode=raw["fixture_mode"])
        for r in raw["rows"]:
            r = dict(r); r["buyers"] = tuple(r["buyers"]); obj.add(Outcome(**r))
        return obj


class Selector:
    def __init__(self, evidence: EvidenceIndex):
        self.e = evidence
        self.c = evidence.c
        self.policy_hash = self.c.digest()

    def evaluate(self, decision: Mapping[str, Any], quote: Mapping[str, Any], budget_sol: float, asof_ns: int) -> dict[str, Any]:
        reasons: list[str] = []; notes: list[str] = []
        buyers = clean_buyers(decision)
        n = decision.get("prior_appearances"); wr = decision.get("prior_win_rate")
        baseline = bool(buyers and finite(n) and n >= 10 and finite(wr) and .70 <= wr <= 1)
        if not baseline: reasons.append("FROZEN_GOLDEN_NOT_QUALIFIED")
        if not finite(budget_sol) or budget_sol <= 0:
            return {"accept": False, "baseline_accept": baseline, "vetoes": reasons + ["INVALID_BUDGET"], "notes": []}
        t = quote.get("ns"); d = decision.get("decision_ns"); created = decision.get("create_ns")
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (t, d, created, asof_ns)) or not 0 <= created <= d <= asof_ns or t > asof_ns:
            reasons.append("NONCAUSAL_INPUT")
        elif asof_ns - t > self.c.max_quote_age_ms * 1_000_000:
            reasons.append("STALE_COIN_QUOTE")
        for field, ceiling in (("impact_bps", self.c.max_impact_bps), ("roundtrip_drag_bps", self.c.max_roundtrip_drag_bps)):
            x = quote.get(field)
            if not finite(x) or x < 0:
                reasons.append("MISSING_" + field.upper())
            elif x > ceiling:
                reasons.append("EXCESSIVE_" + field.upper())
        if quote.get("supported") is not True: reasons.append("UNSUPPORTED_EXECUTABLE_ROUTE")
        mint = str(decision.get("mint", ""))
        pooled = self.e.stats("buyer", buyers, budget_sol, asof_ns, mint)
        cohort = self.e.stats("cohort", (cohort_key(buyers),), budget_sol, asof_ns, mint)
        creator = self.e.stats("creator", (str(decision.get("creator", "")),), budget_sol, asof_ns, mint)
        if pooled["n"] >= self.c.buyer_min_unique_coins:
            if pooled["heuristic_lower_net"] < self.c.min_expected_net_bps / 10000:
                reasons.append("INSUFFICIENT_COST_ALIGNED_NET_EDGE")
        else:
            notes.append("NET_REPUTATION_WARMUP_FROZEN_GOLDEN_FALLBACK")
        for label, stats, minimum in (("COHORT", cohort, self.c.cohort_min_unique_coins), ("CREATOR", creator, self.c.creator_min_unique_coins)):
            if stats["n"] >= minimum:
                if stats["heuristic_upper_net"] < 0: reasons.append(label + "_NEGATIVE_EXPECTANCY")
                if stats["tail_frequency"] > self.c.max_tail_frequency: reasons.append(label + "_EXCESSIVE_TAIL_LOSSES")
            else: notes.append(label + "_QUALITY_UNKNOWN")
        return {"accept": baseline and not reasons, "baseline_accept": baseline,
                "vetoes": reasons, "notes": notes, "asof_ns": asof_ns,
                "cost_hash": self.e.cost_hash, "policy_hash": self.policy_hash,
                "budget_sol": budget_sol, "size_bucket": size_bucket(budget_sol),
                "pooled_unique_coin_stats": pooled, "exact_cohort_stats": cohort, "creator_stats": creator,
                "no_wallet_ownership_inference": True, "future_performance_proven": False}

# FULL3S_HARDENING_20260916_1

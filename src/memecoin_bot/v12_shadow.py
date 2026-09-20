from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "v12-shadow-suite-v1"


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(high, max(low, value))


def _mean(values: Sequence[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _safe_div(num: float, den: float) -> float:
    return num / den if den else 0.0


def _profit_factor(pnls: Sequence[float]) -> float:
    gains = sum(v for v in pnls if v > 0)
    losses = abs(sum(v for v in pnls if v <= 0))
    if not losses:
        return float("inf") if gains > 0 else 0.0
    return gains / losses


def _max_drawdown_fraction(pnls: Sequence[float], starting_bankroll: float) -> float:
    equity = starting_bankroll
    peak = equity
    worst = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return _safe_div(worst, max(starting_bankroll, 1e-12))


def _wilson_lower(wins: int, total: int, z: float = 1.959963984540054) -> float:
    if total <= 0:
        return 0.0
    p = wins / total
    z2 = z * z
    denom = 1.0 + z2 / total
    centre = p + z2 / (2 * total)
    margin = z * math.sqrt((p * (1 - p) + z2 / (4 * total)) / total)
    return max(0.0, (centre - margin) / denom)


@dataclass(frozen=True, slots=True)
class MetricSnapshot:
    trades: int
    wins: int
    win_rate: float
    wilson_lower: float
    net_pnl_sol: float
    profit_factor: float
    max_drawdown_fraction: float
    expectancy_sol: float

    @classmethod
    def from_pnls(cls, pnls: Sequence[float], starting_bankroll: float = 3.0) -> "MetricSnapshot":
        wins = sum(v > 0 for v in pnls)
        trades = len(pnls)
        return cls(
            trades=trades,
            wins=wins,
            win_rate=_safe_div(wins, trades),
            wilson_lower=_wilson_lower(wins, trades),
            net_pnl_sol=sum(pnls),
            profit_factor=_profit_factor(pnls),
            max_drawdown_fraction=_max_drawdown_fraction(pnls, starting_bankroll),
            expectancy_sol=_mean(list(pnls)),
        )


class CreatorConcentrationAnalyzer:
    def analyze(
        self,
        ledger: Sequence[Mapping[str, Any]],
        *,
        starting_bankroll: float = 3.0,
        top_k: Sequence[int] = (1, 3, 5),
    ) -> dict[str, Any]:
        by_creator: dict[str, list[float]] = defaultdict(list)
        for row in ledger:
            by_creator[str(row.get("creator") or "UNKNOWN")].append(_finite(row.get("pnl_sol")))
        creators = []
        for creator, pnls in by_creator.items():
            creators.append({"creator": creator, **asdict(MetricSnapshot.from_pnls(pnls, starting_bankroll))})
        creators.sort(key=lambda x: (x["trades"], x["net_pnl_sol"]), reverse=True)
        full = MetricSnapshot.from_pnls([_finite(row.get("pnl_sol")) for row in ledger], starting_bankroll)
        exclusions = {}
        ordered = [row["creator"] for row in creators]
        for k in top_k:
            removed = set(ordered[:k])
            remaining = [_finite(row.get("pnl_sol")) for row in ledger if str(row.get("creator") or "UNKNOWN") not in removed]
            exclusions[f"exclude_top_{k}_by_trade_count"] = asdict(MetricSnapshot.from_pnls(remaining, starting_bankroll))
        total = max(len(ledger), 1)
        shares = sorted((len(v) / total for v in by_creator.values()), reverse=True)
        hhi = sum(x * x for x in shares)
        max_share = shares[0] if shares else 0.0
        return {
            "full": asdict(full),
            "creator_count": len(by_creator),
            "top_creator_trade_share": max_share,
            "creator_hhi": hhi,
            "creator_diversity_index": 1.0 - hhi,
            "creators": creators,
            "exclusions": exclusions,
            "flags": {
                "top_creator_over_35pct": max_share > 0.35,
                "hhi_concentrated": hhi > 0.25,
                "fewer_than_10_creators": len(by_creator) < 10,
            },
        }


class RegimeProfiler:
    def _bucket(self, row: Mapping[str, Any]) -> dict[str, str]:
        decision_ns = int(row.get("decision_ns") or 0)
        dt = datetime.fromtimestamp(decision_ns / 1_000_000_000, tz=UTC) if decision_ns else None
        hour = dt.hour if dt else -1
        daypart = "UNKNOWN" if hour < 0 else "00_06_UTC" if hour < 6 else "06_12_UTC" if hour < 12 else "12_18_UTC" if hour < 18 else "18_24_UTC"
        output = _finite(row.get("output_ratio"), 1.0)
        output_band = ">=0.90" if output >= 0.90 else "0.80-0.90" if output >= 0.80 else "0.70-0.80" if output >= 0.70 else "0.65-0.70"
        chase = _finite(row.get("create_to_fill_price_multiple"), 1.0)
        chase_band = "<=1.15x" if chase <= 1.15 else "1.15-1.30x" if chase <= 1.30 else "1.30-1.40x" if chase <= 1.40 else "1.40-1.50x"
        seed = _finite(row.get("creator_seed_sol"))
        seed_band = "2-3_SOL" if seed < 3 else "3-6_SOL" if seed < 6 else "6-12_SOL" if seed < 12 else ">=12_SOL"
        age = _finite(row.get("tweet_age_seconds"))
        age_band = "0-1s" if age <= 1 else "1-3s" if age <= 3 else "3-6s" if age <= 6 else "6-10s"
        return {
            "daypart": daypart,
            "output_band": output_band,
            "chase_band": chase_band,
            "seed_band": seed_band,
            "social_age_band": age_band,
            "exit_reason": str(row.get("exit_reason") or "UNKNOWN"),
        }

    def analyze(self, ledger: Sequence[Mapping[str, Any]], *, starting_bankroll: float = 3.0) -> dict[str, Any]:
        dimensions: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        for row in ledger:
            pnl = _finite(row.get("pnl_sol"))
            for dim, bucket in self._bucket(row).items():
                dimensions[dim][bucket].append(pnl)
        return {
            dim: {name: asdict(MetricSnapshot.from_pnls(pnls, starting_bankroll)) for name, pnls in sorted(buckets.items())}
            for dim, buckets in dimensions.items()
        }


class GuardCounterfactualAnalyzer:
    def analyze(self, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        enriched = []
        correct = missed = unresolved = 0
        avoided = missed_sol = 0.0
        for row in rows:
            cf = row.get("counterfactual_pnl_sol")
            if cf is None:
                unresolved += 1
                enriched.append({**dict(row), "counterfactual_class": "UNRESOLVED"})
                continue
            pnl = _finite(cf)
            if pnl <= 0:
                correct += 1
                avoided += abs(pnl)
                label = "CORRECT_REJECTION"
            else:
                missed += 1
                missed_sol += pnl
                label = "MISSED_WINNER"
            enriched.append({**dict(row), "counterfactual_class": label})
        resolved = correct + missed
        return {
            "rows": enriched,
            "resolved": resolved,
            "unresolved": unresolved,
            "correct_rejections": correct,
            "missed_winners": missed,
            "correct_rejection_rate": _safe_div(correct, resolved),
            "avoided_loss_sol": avoided,
            "missed_profit_sol": missed_sol,
            "net_guard_value_sol": avoided - missed_sol,
        }


class RiskOfRuinSimulator:
    def simulate(
        self,
        pnls: Sequence[float],
        *,
        starting_bankroll: float = 3.0,
        paths: int = 20_000,
        horizon: int = 200,
        ruin_fraction: float = 0.5,
        seed: int = 12002026,
    ) -> dict[str, Any]:
        if not pnls:
            return {"paths": 0, "status": "INSUFFICIENT_DATA"}
        rng = random.Random(seed)
        finals, drawdowns, streaks = [], [], []
        ruined = 0
        threshold = starting_bankroll * ruin_fraction
        for _ in range(paths):
            equity = peak = starting_bankroll
            max_dd = 0.0
            streak = worst_streak = 0
            hit_ruin = False
            for _ in range(horizon):
                pnl = rng.choice(pnls)
                equity += pnl
                if pnl <= 0:
                    streak += 1
                    worst_streak = max(worst_streak, streak)
                else:
                    streak = 0
                peak = max(peak, equity)
                max_dd = max(max_dd, _safe_div(peak - equity, max(peak, 1e-12)))
                hit_ruin = hit_ruin or equity <= threshold
            ruined += int(hit_ruin)
            finals.append(equity)
            drawdowns.append(max_dd)
            streaks.append(worst_streak)

        def pct(values: Sequence[float], q: float) -> float:
            ordered = sorted(values)
            return ordered[min(len(ordered) - 1, max(0, int((len(ordered) - 1) * q)))]

        return {
            "paths": paths,
            "horizon_trades": horizon,
            "ruin_threshold_fraction": ruin_fraction,
            "risk_of_ruin": ruined / paths,
            "ending_equity": {"p05": pct(finals, 0.05), "median": pct(finals, 0.50), "p95": pct(finals, 0.95)},
            "max_drawdown": {"median": pct(drawdowns, 0.50), "p95": pct(drawdowns, 0.95), "p99": pct(drawdowns, 0.99)},
            "losing_streak": {"median": pct(streaks, 0.50), "p95": pct(streaks, 0.95), "p99": pct(streaks, 0.99)},
            "seed": seed,
        }


class DriftMonitor:
    def evaluate(self, baseline_pnls: Sequence[float], current_pnls: Sequence[float], *, window: int = 20) -> dict[str, Any]:
        base = MetricSnapshot.from_pnls(baseline_pnls)
        recent_values = list(current_pnls[-window:])
        recent = MetricSnapshot.from_pnls(recent_values)
        base_mean = _mean(list(baseline_pnls))
        recent_mean = _mean(recent_values)
        base_std = statistics.pstdev(baseline_pnls) if len(baseline_pnls) > 1 else 0.0
        z = _safe_div(recent_mean - base_mean, base_std / math.sqrt(max(len(recent_values), 1))) if base_std else 0.0
        wr_delta = recent.win_rate - base.win_rate
        pf_ratio = _safe_div(recent.profit_factor, base.profit_factor) if math.isfinite(base.profit_factor) else 1.0
        expectancy_ratio = _safe_div(recent.expectancy_sol, base.expectancy_sol) if base.expectancy_sol else 0.0
        reasons = []
        if len(recent_values) >= 10 and wr_delta <= -0.20:
            reasons.append("WIN_RATE_COLLAPSE")
        if len(recent_values) >= 10 and pf_ratio < 0.50:
            reasons.append("PROFIT_FACTOR_COLLAPSE")
        if len(recent_values) >= 10 and expectancy_ratio < 0.40:
            reasons.append("EXPECTANCY_COLLAPSE")
        if len(recent_values) >= 10 and z < -2.0:
            reasons.append("EXPECTANCY_ZSCORE")
        severity = "HALT_REVIEW" if len(reasons) >= 2 else "WATCH" if reasons else "OK"
        return {
            "severity": severity,
            "reasons": reasons,
            "baseline": asdict(base),
            "recent": asdict(recent),
            "recent_window": len(recent_values),
            "win_rate_delta": wr_delta,
            "profit_factor_ratio": pf_ratio,
            "expectancy_ratio": expectancy_ratio,
            "expectancy_z": z,
        }


@dataclass(frozen=True, slots=True)
class ShadowDecision:
    module: str
    score: float
    action: str
    reasons: tuple[str, ...]
    shadow_only: bool = True


class NewCreatorAnalogueScorer:
    weights = {
        "prelaunch_social_quality": 0.22,
        "funding_quality": 0.18,
        "creator_seed_quality": 0.14,
        "buyer_diversity": 0.15,
        "clean_bundle_score": 0.12,
        "known_funder_similarity": 0.10,
        "early_flow_quality": 0.09,
    }

    def score(self, features: Mapping[str, Any]) -> ShadowDecision:
        parts = {key: _clamp(_finite(features.get(key))) for key in self.weights}
        score = sum(parts[k] * w for k, w in self.weights.items())
        hard_fail = bool(features.get("hard_negative")) or bool(features.get("mayhem_mode"))
        action = "REJECT" if hard_fail else "SHADOW_TIER_B" if score >= 0.82 else "OBSERVE" if score >= 0.68 else "REJECT"
        return ShadowDecision("NEW_CREATOR_ANALOGUE", score, action, tuple(f"{k}={parts[k]:.3f}" for k in sorted(parts)))


class HardNegativeScorer:
    penalties = {
        "known_rug_funder": 0.35,
        "insider_concentration": 0.18,
        "bundle_probability": 0.16,
        "wash_probability": 0.14,
        "creator_dump_risk": 0.12,
        "data_staleness": 0.20,
        "provider_disagreement": 0.15,
    }

    def score(self, features: Mapping[str, Any]) -> ShadowDecision:
        applied, risk = [], 0.0
        for key, weight in self.penalties.items():
            value = _clamp(_finite(features.get(key)))
            risk += value * weight
            if value >= 0.5:
                applied.append(f"{key}={value:.3f}")
        risk = _clamp(risk)
        action = "SHADOW_VETO" if risk >= 0.55 else "WATCH" if risk >= 0.35 else "PASS"
        return ShadowDecision("HARD_NEGATIVE", risk, action, tuple(applied))


class SellAbsorptionScorer:
    def score(self, features: Mapping[str, Any]) -> ShadowDecision:
        absorption = _clamp(_finite(features.get("buy_after_sell_ratio")))
        depth = _clamp(_finite(features.get("liquidity_resilience")))
        buyer_quality = _clamp(_finite(features.get("buyer_quality")))
        creator_sell = _clamp(_finite(features.get("creator_sell_pressure")))
        acceleration = _clamp(_finite(features.get("flow_acceleration")))
        score = _clamp(0.28 * absorption + 0.24 * depth + 0.18 * buyer_quality + 0.16 * acceleration + 0.14 * (1.0 - creator_sell))
        action = "SHADOW_HOLD" if score >= 0.70 else "SHADOW_TIGHTEN" if score >= 0.45 else "SHADOW_EXIT"
        return ShadowDecision("SELL_ABSORPTION", score, action, (f"absorption={absorption:.3f}", f"depth={depth:.3f}", f"buyer_quality={buyer_quality:.3f}", f"creator_sell={creator_sell:.3f}", f"acceleration={acceleration:.3f}"))


class FailedIntentScorer:
    def score(self, features: Mapping[str, Any]) -> ShadowDecision:
        failed_buy = max(0.0, _finite(features.get("failed_buy_notional")))
        landed_buy = max(0.0, _finite(features.get("landed_buy_notional")))
        failed_sell = max(0.0, _finite(features.get("failed_sell_notional")))
        landed_sell = max(0.0, _finite(features.get("landed_sell_notional")))
        sophisticated = _clamp(_finite(features.get("sophisticated_failed_buy_share")))
        buy_intent = _safe_div(failed_buy, failed_buy + landed_buy)
        sell_intent = _safe_div(failed_sell, failed_sell + landed_sell)
        score = _clamp(0.55 * buy_intent + 0.30 * sophisticated + 0.15 * (1.0 - sell_intent))
        action = "SHADOW_SUPPORT" if score >= 0.65 else "NEUTRAL" if score >= 0.40 else "SHADOW_WARNING"
        return ShadowDecision("FAILED_INTENT", score, action, (f"failed_buy_share={buy_intent:.3f}", f"failed_sell_share={sell_intent:.3f}", f"sophisticated_share={sophisticated:.3f}"))


class ShadowEnsemble:
    def __init__(self) -> None:
        self.new_creator = NewCreatorAnalogueScorer()
        self.hard_negative = HardNegativeScorer()
        self.sell_absorption = SellAbsorptionScorer()
        self.failed_intent = FailedIntentScorer()

    def evaluate(self, feature_groups: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        decisions = [
            self.new_creator.score(feature_groups.get("new_creator", {})),
            self.hard_negative.score(feature_groups.get("hard_negative", {})),
            self.sell_absorption.score(feature_groups.get("sell_absorption", {})),
            self.failed_intent.score(feature_groups.get("failed_intent", {})),
        ]
        veto = next(d for d in decisions if d.module == "HARD_NEGATIVE")
        recommendation = "SHADOW_VETO" if veto.action == "SHADOW_VETO" else "SHADOW_SUPPORT" if all(d.action not in {"REJECT", "SHADOW_EXIT", "SHADOW_WARNING"} for d in decisions) else "SHADOW_REVIEW"
        payload = {"schema_version": SCHEMA_VERSION, "shadow_only": True, "recommendation": recommendation, "decisions": [asdict(d) for d in decisions]}
        payload["evidence_hash"] = hashlib.sha256(json.dumps(payload["decisions"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return payload


def build_shadow_report(
    state: Mapping[str, Any],
    *,
    baseline_state: Mapping[str, Any] | None = None,
    counterfactual_rows: Sequence[Mapping[str, Any]] = (),
    risk_paths: int = 20_000,
) -> dict[str, Any]:
    ledger = list(state.get("ledger") or [])
    starting = _finite(state.get("starting_bankroll_sol"), 3.0)
    pnls = [_finite(row.get("pnl_sol")) for row in ledger]
    baseline_ledger = list((baseline_state or {}).get("ledger") or [])
    baseline_pnls = [_finite(row.get("pnl_sol")) for row in baseline_ledger] or pnls
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "shadow_only": True,
        "production_mutations": 0,
        "source_model_sha256": state.get("model_sha256"),
        "source_status": state.get("status"),
        "concentration": CreatorConcentrationAnalyzer().analyze(ledger, starting_bankroll=starting),
        "regimes": RegimeProfiler().analyze(ledger, starting_bankroll=starting),
        "guard_counterfactuals": GuardCounterfactualAnalyzer().analyze(counterfactual_rows),
        "risk_of_ruin": RiskOfRuinSimulator().simulate(pnls, starting_bankroll=starting, paths=risk_paths),
        "drift": DriftMonitor().evaluate(baseline_pnls, pnls),
    }
    report["integrity"] = {
        "ledger_rows": len(ledger),
        "ledger_sha256": hashlib.sha256(json.dumps(ledger, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "baseline_rows": len(baseline_ledger),
    }
    return report

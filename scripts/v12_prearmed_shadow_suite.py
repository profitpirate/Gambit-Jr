"""Operational sidecar intelligence for the frozen V12 Pre-Armed forward test.

This module is intentionally incapable of changing V12 Pre-Armed decisions.
It consumes the already-frozen paper ledger and the raw capture for the current
window, then persists replayable shadow analytics.

Subsystems:
- creator concentration / leave-out robustness;
- rejected-entry counterfactuals;
- causal market-regime labelling;
- latency + fee execution stress;
- deterministic bootstrap bankroll / drawdown risk;
- rolling drift detection against the locked 50-trade baseline;
- new-creator analogue discovery (shadow-only);
- hard-negative veto scoring (shadow-only);
- sell-absorption position telemetry at causal horizons (shadow-only);
- failed-intent evidence coverage (shadow-only);
- integrity, idempotency and provenance checks.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import math
import random
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import e4_v12_axiom_paper_live as paper
import e4_v12_failed_intent_registry as failed_registry
import e4_v12_profit_survival_search as base
import e4_v12_true_latency_replay as replay
import v12_prearmed_guardrail_policy as guardrail_policy

SCHEMA_VERSION = "v12-pre-armed-shadow-suite-v1"
NEW_CREATOR_VERSION = "v12-new-creator-analogue-shadow-v1"
HARD_NEGATIVE_VERSION = "v12-hard-negative-shadow-v1"
SELL_ABSORPTION_VERSION = "v12-sell-absorption-shadow-v1"
FAILED_AWARE_VERSION = "v12-failed-aware-shadow-v1"

LATENCY_MS = (5, 10, 25, 50, 100)
FEE_MULTIPLIERS = (1.0, 1.5, 2.0)
ABSORPTION_HORIZONS_MS = (50, 100, 250, 500)
MAX_NEW_CREATOR_METADATA_FETCHES = 250
MONTE_CARLO_PATHS = 10_000
MONTE_CARLO_HORIZON = 200


def finite(value: Any, default: float = 0.0) -> float:
    return paper.finite(value, default)


def integer(value: Any, default: int = 0) -> int:
    return paper.integer(value, default)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def metric_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    pnls = [finite(row.get("pnl_sol")) for row in rows]
    wins = sum(value > 0 for value in pnls)
    gains = sum(value for value in pnls if value > 0)
    losses = abs(sum(value for value in pnls if value <= 0))
    return {
        "trades": len(rows),
        "wins": wins,
        "losses": len(rows) - wins,
        "win_rate": wins / len(rows) if rows else 0.0,
        "net_pnl_sol": sum(pnls),
        "expectancy_sol": sum(pnls) / len(rows) if rows else 0.0,
        "profit_factor": gains / max(losses, 1e-12),
        "wilson_lower_bound": paper.wilson_lower(wins, len(rows)),
    }


def creator_concentration(ledger: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_creator: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in ledger:
        by_creator[str(row.get("creator") or "UNKNOWN")].append(row)

    total = max(1, len(ledger))
    total_pnl = sum(finite(row.get("pnl_sol")) for row in ledger)
    groups = []
    for creator, rows in by_creator.items():
        summary = metric_summary(rows)
        groups.append(
            {
                "creator": creator,
                **summary,
                "trade_share": len(rows) / total,
                "pnl_share": summary["net_pnl_sol"] / max(abs(total_pnl), 1e-12),
            }
        )
    groups.sort(key=lambda row: (-row["trades"], row["creator"]))
    hhi = sum(row["trade_share"] ** 2 for row in groups)
    top1 = groups[0]["creator"] if groups else None
    top3 = {row["creator"] for row in groups[:3]}
    without_top1 = [
        row for row in ledger if str(row.get("creator") or "UNKNOWN") != top1
    ]
    without_top3 = [
        row for row in ledger if str(row.get("creator") or "UNKNOWN") not in top3
    ]
    return {
        "creator_count": len(groups),
        "trade_hhi": hhi,
        "effective_creator_count": 1.0 / hhi if hhi > 0 else 0.0,
        "top_creator_trade_share": groups[0]["trade_share"] if groups else 0.0,
        "top3_trade_share": sum(row["trade_share"] for row in groups[:3]),
        "overall": metric_summary(ledger),
        "without_top_creator": metric_summary(without_top1),
        "without_top3_creators": metric_summary(without_top3),
        "by_creator": groups,
    }


def _distribution(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, float]:
    counts = Counter(str(row.get(key) or "UNKNOWN") for row in rows)
    total = sum(counts.values())
    return {name: count / total for name, count in counts.items()} if total else {}


def _js_divergence(left: Mapping[str, float], right: Mapping[str, float]) -> float:
    keys = set(left) | set(right)
    midpoint = {
        key: (left.get(key, 0.0) + right.get(key, 0.0)) / 2.0 for key in keys
    }

    def kl(source: Mapping[str, float]) -> float:
        value = 0.0
        for key in keys:
            p = source.get(key, 0.0)
            q = midpoint.get(key, 0.0)
            if p > 0 and q > 0:
                value += p * math.log2(p / q)
        return value

    return 0.5 * kl(left) + 0.5 * kl(right)


def drift_telemetry(
    current: Sequence[Mapping[str, Any]],
    baseline: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    baseline_metrics = metric_summary(baseline)
    current_metrics = metric_summary(current)
    recent_10 = metric_summary(current[-10:])
    recent_20 = metric_summary(current[-20:])
    creator_js = _js_divergence(
        _distribution(baseline, "creator"),
        _distribution(current, "creator"),
    )
    concentration = creator_concentration(current)

    flags: list[str] = []
    if len(current) >= 10 and recent_10["expectancy_sol"] < 0:
        flags.append("RECENT_10_NEGATIVE_EXPECTANCY")
    if current_metrics["profit_factor"] < 1.25:
        flags.append("PROFIT_FACTOR_BELOW_1_25")
    if (
        baseline_metrics["trades"]
        and current_metrics["win_rate"] < baseline_metrics["win_rate"] - 0.15
    ):
        flags.append("WIN_RATE_DOWN_GT_15PP")
    if concentration["top_creator_trade_share"] > 0.50:
        flags.append("TOP_CREATOR_OVER_50_PERCENT")
    if creator_js > 0.35:
        flags.append("CREATOR_MIX_DRIFT")

    if len(current) < 20:
        status = "OBSERVING"
    elif any(
        flag in flags
        for flag in ("PROFIT_FACTOR_BELOW_1_25", "RECENT_10_NEGATIVE_EXPECTANCY")
    ):
        status = "RED"
    elif flags:
        status = "YELLOW"
    else:
        status = "GREEN"

    return {
        "status": status,
        "flags": flags,
        "baseline": baseline_metrics,
        "current": current_metrics,
        "recent_10": recent_10,
        "recent_20": recent_20,
        "creator_js_divergence_bits": creator_js,
        "top_creator_trade_share": concentration["top_creator_trade_share"],
    }


def risk_of_ruin(
    ledger: Sequence[Mapping[str, Any]],
    *,
    starting_bankroll: float,
    position_fraction: float,
    model_hash: str,
    paths: int = MONTE_CARLO_PATHS,
    horizon: int = MONTE_CARLO_HORIZON,
) -> dict[str, Any]:
    returns = [
        finite(row.get("pnl_sol"))
        / max(finite(row.get("entry_cost_sol")), 1e-12)
        for row in ledger
        if finite(row.get("entry_cost_sol")) > 0
    ]
    if not returns:
        return {
            "status": "INSUFFICIENT_DATA",
            "source_trade_samples": 0,
            "paths": 0,
            "horizon_trades": horizon,
        }

    seed = int(hashlib.sha256(model_hash.encode()).hexdigest()[:16], 16)
    rng = random.Random(seed)
    endings: list[float] = []
    max_drawdowns: list[float] = []
    longest_loss_streaks: list[int] = []
    breached_half = 0
    breached_20dd = 0
    breached_one_sol = 0

    for _ in range(paths):
        bankroll = starting_bankroll
        peak = bankroll
        max_dd = 0.0
        streak = 0
        max_streak = 0
        half_hit = False
        dd_hit = False
        one_sol_hit = False
        for _trade in range(horizon):
            sampled_return = returns[rng.randrange(len(returns))]
            pnl = bankroll * position_fraction * sampled_return
            bankroll = max(0.0, bankroll + pnl)
            if pnl <= 0:
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0
            peak = max(peak, bankroll)
            drawdown = (peak - bankroll) / max(peak, 1e-12)
            max_dd = max(max_dd, drawdown)
            half_hit |= bankroll <= starting_bankroll * 0.5
            dd_hit |= drawdown >= 0.20
            one_sol_hit |= bankroll <= 1.0
            if bankroll <= 1e-12:
                break
        endings.append(bankroll)
        max_drawdowns.append(max_dd)
        longest_loss_streaks.append(max_streak)
        breached_half += int(half_hit)
        breached_20dd += int(dd_hit)
        breached_one_sol += int(one_sol_hit)

    endings.sort()
    max_drawdowns.sort()
    longest_loss_streaks.sort()

    def percentile(values: Sequence[float | int], q: float) -> float:
        index = min(len(values) - 1, max(0, int(q * (len(values) - 1))))
        return float(values[index])

    return {
        "status": "SIMULATED",
        "source_trade_samples": len(returns),
        "paths": paths,
        "horizon_trades": horizon,
        "position_fraction": position_fraction,
        "return_fraction_mean": statistics.fmean(returns),
        "return_fraction_median": statistics.median(returns),
        "ending_bankroll_sol": {
            "p05": percentile(endings, 0.05),
            "median": percentile(endings, 0.50),
            "p95": percentile(endings, 0.95),
        },
        "maximum_drawdown_fraction": {
            "median": percentile(max_drawdowns, 0.50),
            "p95": percentile(max_drawdowns, 0.95),
        },
        "longest_loss_streak": {
            "median": percentile(longest_loss_streaks, 0.50),
            "p95": percentile(longest_loss_streaks, 0.95),
        },
        "probability_bankroll_below_half": breached_half / paths,
        "probability_drawdown_ge_20pct": breached_20dd / paths,
        "probability_bankroll_le_1_sol": breached_one_sol / paths,
        "deterministic_seed": seed,
    }


def regime_snapshot(
    run: replay.RunData,
    traces: Mapping[str, base.Trace],
    candidate_counts: Mapping[str, int],
    prior_windows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    create_times: list[int] = []
    launches = 0
    for rows in run.events_by_mint.values():
        create = paper.creation(rows)
        if create is None:
            continue
        launches += 1
        create_times.append(integer(create.get("received_ns")))

    if len(create_times) >= 2:
        duration_minutes = max(
            1.0 / 60.0,
            (max(create_times) - min(create_times)) / 60_000_000_000,
        )
    else:
        duration_minutes = 1.0

    launch_rate = launches / duration_minutes
    history = [
        finite(row.get("launches_per_minute"))
        for row in prior_windows
        if finite(row.get("launches_per_minute")) > 0
    ]
    reference = statistics.median(history) if history else launch_rate
    ratio = launch_rate / max(reference, 1e-12)
    if len(history) < 2:
        regime = "BOOTSTRAP"
    elif ratio < 0.70:
        regime = "QUIET"
    elif ratio > 1.50:
        regime = "HOT"
    else:
        regime = "NORMAL"

    all_points = [
        point
        for trace in traces.values()
        for point in trace.points
    ]
    buys = [
        point
        for point in all_points
        if point.kind.upper() in base.choice_sets.BUY_KINDS
    ]
    sells = [
        point
        for point in all_points
        if point.kind.upper() in base.choice_sets.SELL_KINDS
    ]
    buy_sol = sum(max(0.0, point.sol_amount) for point in buys)
    sell_sol = sum(max(0.0, point.sol_amount) for point in sells)
    flow_ratio = buy_sol / max(sell_sol, 1e-9)
    flow_regime = (
        "BUY_DOMINANT"
        if flow_ratio >= 1.5
        else "SELL_DOMINANT"
        if flow_ratio <= 0.67
        else "BALANCED"
    )
    unique_traders = len(
        {point.trader for point in all_points if point.trader}
    )

    excursions: list[float] = []
    for trace in traces.values():
        prices = [point.price_sol for point in trace.points if point.price_sol > 0]
        if len(prices) < 2:
            continue
        first = prices[0]
        peak = max(prices)
        trough = min(prices)
        excursions.append(
            max(
                abs(peak / max(first, 1e-18) - 1.0),
                abs(trough / max(first, 1e-18) - 1.0),
            )
        )
    median_excursion = statistics.median(excursions) if excursions else 0.0
    prior_excursions = [
        finite(row.get("median_price_excursion"))
        for row in prior_windows
        if finite(row.get("median_price_excursion")) > 0
    ]
    excursion_reference = (
        statistics.median(prior_excursions)
        if prior_excursions
        else median_excursion
    )
    volatility_ratio = (
        median_excursion / max(excursion_reference, 1e-12)
        if median_excursion > 0
        else 0.0
    )
    volatility_regime = (
        "BOOTSTRAP"
        if len(prior_excursions) < 2
        else "HIGH"
        if volatility_ratio >= 1.5
        else "LOW"
        if volatility_ratio <= 0.67
        else "NORMAL"
    )

    median_create_ns = (
        int(statistics.median(create_times)) if create_times else 0
    )
    if median_create_ns > 0:
        hour = datetime.fromtimestamp(
            median_create_ns / 1_000_000_000, tz=UTC
        ).hour
        daypart = (
            "00_06_UTC"
            if hour < 6
            else "06_12_UTC"
            if hour < 12
            else "12_18_UTC"
            if hour < 18
            else "18_24_UTC"
        )
    else:
        daypart = "UNKNOWN"

    unknown = integer(candidate_counts.get("unknown_creator"))
    selected = integer(candidate_counts.get("selected_candidates"))
    return {
        "run_id": run.run_id,
        "launches": launches,
        "duration_minutes": duration_minutes,
        "launches_per_minute": launch_rate,
        "rolling_median_launches_per_minute": reference,
        "relative_launch_pressure": ratio,
        "market_regime": regime,
        "utc_daypart": daypart,
        "buy_sol": buy_sol,
        "sell_sol": sell_sol,
        "buy_sell_ratio": flow_ratio,
        "flow_regime": flow_regime,
        "unique_traders": unique_traders,
        "median_price_excursion": median_excursion,
        "rolling_median_price_excursion": excursion_reference,
        "relative_volatility": volatility_ratio,
        "volatility_regime": volatility_regime,
        "known_creator_rate": max(0.0, 1.0 - unknown / max(launches, 1)),
        "selected_rate": selected / max(launches, 1),
        "candidate_counts": dict(candidate_counts),
    }


def _actual_run_starting_cash(
    paper_state: Mapping[str, Any], run_id: str
) -> float:
    ending = finite(paper_state.get("ending_bankroll_sol"))
    pnl = sum(
        finite(row.get("pnl_sol"))
        for row in paper_state.get("ledger", [])
        if str(row.get("run_id")) == run_id
    )
    rejected_fees = sum(
        finite(row.get("fee_sol"))
        for row in paper_state.get("rejections", [])
        if str(row.get("run_id")) == run_id
    )
    return ending - pnl + rejected_fees


def _candidate_budget_context(
    candidates: Sequence[paper.Candidate],
    paper_state: Mapping[str, Any],
    model: Mapping[str, Any],
    run_id: str,
) -> dict[str, float]:
    accepted = {
        str(row.get("mint")): row
        for row in paper_state.get("ledger", [])
        if str(row.get("run_id")) == run_id
    }
    rejected_rows = {
        str(row.get("mint")): row
        for row in paper_state.get("rejections", [])
        if str(row.get("run_id")) == run_id
    }
    cash = _actual_run_starting_cash(paper_state, run_id)
    active: list[Mapping[str, Any]] = []
    budgets: dict[str, float] = {}
    fraction = finite(model["position_sizing"]["fraction_of_available_cash"])

    for candidate in sorted(
        candidates, key=lambda value: (value.decision_ns, value.mint)
    ):
        remaining = []
        for trade in active:
            if integer(trade.get("exit_ns")) <= candidate.decision_ns:
                cash += finite(trade.get("proceeds_sol"))
            else:
                remaining.append(trade)
        active = remaining
        budgets[candidate.mint] = cash * fraction

        trade = accepted.get(candidate.mint)
        if trade is not None:
            cash -= finite(trade.get("entry_cost_sol"))
            active.append(trade)
        elif candidate.mint in rejected_rows:
            cash -= finite(rejected_rows[candidate.mint].get("fee_sol"))

    return budgets


def rejected_counterfactuals(
    run: replay.RunData,
    traces: Mapping[str, base.Trace],
    candidates: Sequence[paper.Candidate],
    paper_state: Mapping[str, Any],
    model: Mapping[str, Any],
) -> list[dict[str, Any]]:
    rejected = {
        str(row.get("mint")): row
        for row in paper_state.get("rejections", [])
        if str(row.get("run_id")) == run.run_id
    }
    if not rejected:
        return []

    budgets = _candidate_budget_context(
        candidates, paper_state, model, run.run_id
    )
    relaxed = copy.deepcopy(model)
    relaxed["selector"]["minimum_entry_output_ratio"] = 0.0
    relaxed["selector"]["maximum_create_to_entry_price_multiple"] = 1e12
    costs = paper.load_costs(relaxed)
    policy = paper.load_policy(relaxed)
    output = []

    for candidate in candidates:
        observed = rejected.get(candidate.mint)
        if observed is None:
            continue
        trace = traces.get(candidate.mint)
        if trace is None:
            output.append(
                {
                    "run_id": run.run_id,
                    "mint": candidate.mint,
                    "status": "MISSING_TRACE",
                    "observed_rejection": dict(observed),
                }
            )
            continue
        trade, secondary_rejection = paper.simulate_trade(
            run,
            trace,
            candidate,
            budgets.get(candidate.mint, 0.0),
            relaxed,
            costs,
            policy,
        )
        output.append(
            {
                "run_id": run.run_id,
                "mint": candidate.mint,
                "status": (
                    "COUNTERFACTUAL_FILLED"
                    if trade is not None
                    else "COUNTERFACTUAL_UNAVAILABLE"
                ),
                "observed_rejection": dict(observed),
                "counterfactual_pnl_sol": (
                    finite(trade.get("pnl_sol")) if trade else None
                ),
                "counterfactual_win": bool(
                    trade and finite(trade.get("pnl_sol")) > 0
                ),
                "counterfactual_exit_reason": (
                    trade.get("exit_reason") if trade else None
                ),
                "secondary_rejection": secondary_rejection,
            }
        )
    return output


def execution_stress_records(
    run: replay.RunData,
    traces: Mapping[str, base.Trace],
    candidates: Sequence[paper.Candidate],
    paper_state: Mapping[str, Any],
    model: Mapping[str, Any],
) -> list[dict[str, Any]]:
    accepted = {
        str(row.get("mint")): row
        for row in paper_state.get("ledger", [])
        if str(row.get("run_id")) == run.run_id
    }
    output = []

    for candidate in candidates:
        actual = accepted.get(candidate.mint)
        if actual is None:
            continue
        trace = traces.get(candidate.mint)
        if trace is None:
            continue
        budget = finite(actual.get("entry_budget_sol"))

        for latency_ms in LATENCY_MS:
            for fee_multiplier in FEE_MULTIPLIERS:
                stressed = copy.deepcopy(model)
                stressed["selector"]["entry_latency_ms"] = latency_ms
                costs_cfg = stressed["execution_costs"]
                for key in (
                    "axiom_net_fee_bps_each_side",
                    "pump_bonding_curve_fee_bps_each_side",
                    "priority_fee_sol_per_transaction",
                    "mev_bribe_sol_on_buy",
                    "mev_bribe_sol_on_sell",
                ):
                    costs_cfg[key] = (
                        finite(costs_cfg.get(key)) * fee_multiplier
                    )
                costs = paper.load_costs(stressed)
                policy = paper.load_policy(stressed)
                trade, rejection = paper.simulate_trade(
                    run,
                    trace,
                    candidate,
                    budget,
                    stressed,
                    costs,
                    policy,
                )
                output.append(
                    {
                        "run_id": run.run_id,
                        "mint": candidate.mint,
                        "latency_ms": latency_ms,
                        "fee_multiplier": fee_multiplier,
                        "status": "FILLED" if trade is not None else "REJECTED",
                        "pnl_sol": (
                            finite(trade.get("pnl_sol")) if trade else None
                        ),
                        "win": bool(
                            trade and finite(trade.get("pnl_sol")) > 0
                        ),
                        "rejection_reason": (
                            rejection.get("reason") if rejection else None
                        ),
                    }
                )
    return output


def aggregate_execution_stress(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    groups: dict[tuple[int, float], list[Mapping[str, Any]]] = defaultdict(
        list
    )
    for row in rows:
        groups[
            (
                integer(row.get("latency_ms")),
                finite(row.get("fee_multiplier")),
            )
        ].append(row)

    output = {}
    for (latency_ms, multiplier), values in sorted(groups.items()):
        filled = [row for row in values if row.get("status") == "FILLED"]
        metrics = metric_summary(filled)
        key = f"{latency_ms}ms_fee_{multiplier:.1f}x"
        output[key] = {
            **metrics,
            "attempted": len(values),
            "filled": len(filled),
            "rejected": len(values) - len(filled),
            "fill_rate": len(filled) / max(len(values), 1),
        }
    return output


def _baseline_handle_stats(
    baseline_ledger: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, float]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in baseline_ledger:
        handle = str(row.get("social_handle") or "").lower().lstrip("@")
        if handle:
            groups[handle].append(row)

    output = {}
    for handle, rows in groups.items():
        summary = metric_summary(rows)
        output[handle] = {
            "trades": float(len(rows)),
            "win_rate": summary["win_rate"],
            "expectancy_sol": summary["expectancy_sol"],
        }
    return output


def _winner_seed_reference(
    baseline_ledger: Sequence[Mapping[str, Any]],
) -> tuple[float, float]:
    values = [
        finite(row.get("creator_seed_sol"))
        for row in baseline_ledger
        if finite(row.get("pnl_sol")) > 0
        and finite(row.get("creator_seed_sol")) > 0
    ]
    if not values:
        return 3.0, 1.0
    median = statistics.median(values)
    deviations = [abs(value - median) for value in values]
    return median, statistics.median(deviations) or 1.0


def _early_flow_features(
    trace: base.Trace | None,
    *,
    decision_ns: int,
    horizon_ms: int = 250,
) -> dict[str, float]:
    if trace is None:
        return {
            "buyer_diversity": 0.0,
            "buy_imbalance": 0.0,
            "bundle_cleanliness": 0.0,
            "price_resilience": 0.0,
            "early_flow_quality": 0.0,
        }
    cutoff = decision_ns + horizon_ms * 1_000_000
    points = [
        point
        for point in trace.points
        if decision_ns <= point.timestamp_ns <= cutoff
    ]
    buys = [
        point
        for point in points
        if point.kind.upper() in base.choice_sets.BUY_KINDS
    ]
    sells = [
        point
        for point in points
        if point.kind.upper() in base.choice_sets.SELL_KINDS
    ]
    buy_sol = sum(max(0.0, point.sol_amount) for point in buys)
    sell_sol = sum(max(0.0, point.sol_amount) for point in sells)
    unique_buyers = len({point.trader for point in buys if point.trader})
    buyer_totals: Counter[str] = Counter()
    slot_counts: Counter[int] = Counter()
    for point in buys:
        if point.trader:
            buyer_totals[point.trader] += max(0.0, point.sol_amount)
        slot_counts[point.slot] += 1
    largest_buyer_share = (
        max(buyer_totals.values()) / max(buy_sol, 1e-12)
        if buyer_totals and buy_sol > 0
        else 1.0
    )
    largest_slot_share = (
        max(slot_counts.values()) / max(len(buys), 1)
        if slot_counts
        else 1.0
    )
    buyer_diversity = clamp(unique_buyers / 5.0)
    buy_imbalance = clamp(buy_sol / max(buy_sol + sell_sol, 1e-12))
    bundle_cleanliness = 1.0 - clamp(
        max(largest_buyer_share, largest_slot_share)
    )
    prices = [point.price_sol for point in points if point.price_sol > 0]
    if len(prices) >= 2:
        price_resilience = clamp(
            (prices[-1] / max(prices[0], 1e-18) - 0.80) / 0.40
        )
    elif prices:
        price_resilience = 0.5
    else:
        price_resilience = 0.0
    early_flow_quality = clamp(
        0.35 * buyer_diversity
        + 0.30 * buy_imbalance
        + 0.20 * bundle_cleanliness
        + 0.15 * price_resilience
    )
    return {
        "buyer_diversity": buyer_diversity,
        "buy_imbalance": buy_imbalance,
        "bundle_cleanliness": bundle_cleanliness,
        "price_resilience": price_resilience,
        "early_flow_quality": early_flow_quality,
    }


def new_creator_score(
    *,
    creator_seed_sol: float,
    tweet_age_seconds: float,
    handle: str,
    handle_stats: Mapping[str, Mapping[str, float]],
    seed_reference: tuple[float, float],
    metadata_quality: float,
    early_flow: Mapping[str, Any] | None = None,
) -> tuple[float, dict[str, float]]:
    seed_score = clamp((creator_seed_sol - 2.0) / 6.0)
    age_score = clamp(1.0 - tweet_age_seconds / 10.0)
    reputation = handle_stats.get(handle.lower().lstrip("@"))
    if reputation:
        rep_score = clamp(
            0.5
            + (finite(reputation.get("win_rate")) - 0.5) * 0.8
            + clamp(
                finite(reputation.get("expectancy_sol")) / 0.10,
                -0.25,
                0.25,
            )
        )
    else:
        rep_score = 0.35

    median, mad = seed_reference
    similarity = math.exp(
        -abs(creator_seed_sol - median) / max(1.0, 2.0 * mad)
    )
    flow = dict(early_flow or {})
    buyer_diversity = clamp(finite(flow.get("buyer_diversity"), 0.5))
    bundle_cleanliness = clamp(finite(flow.get("bundle_cleanliness"), 0.5))
    early_flow_quality = clamp(finite(flow.get("early_flow_quality"), 0.5))
    components = {
        "creator_seed": seed_score,
        "social_recency": age_score,
        "handle_reputation": rep_score,
        "winner_seed_similarity": similarity,
        "metadata_quality": clamp(metadata_quality),
        "buyer_diversity": buyer_diversity,
        "bundle_cleanliness": bundle_cleanliness,
        "early_flow_quality": early_flow_quality,
    }
    score = (
        0.18 * seed_score
        + 0.15 * age_score
        + 0.18 * rep_score
        + 0.12 * similarity
        + 0.07 * clamp(metadata_quality)
        + 0.12 * buyer_diversity
        + 0.08 * bundle_cleanliness
        + 0.10 * early_flow_quality
    )
    return clamp(score), components


async def new_creator_shadow(
    run: replay.RunData,
    traces: Mapping[str, base.Trace],
    model: Mapping[str, Any],
    cache: dict[str, dict[str, Any]],
    baseline_ledger: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    prior_attempts = {
        str(key): integer(value)
        for key, value in model["creator_prior_e4_attempts"].items()
    }
    selector = model["selector"]
    preliminaries = []
    counters: Counter[str] = Counter()

    for mint, rows in run.events_by_mint.items():
        create = paper.creation(rows)
        if create is None:
            continue
        raw = (
            create.get("raw")
            if isinstance(create.get("raw"), Mapping)
            else {}
        )
        creator = str(
            create.get("creator")
            or raw.get("creator")
            or create.get("trader")
            or ""
        )
        if prior_attempts.get(creator, 0) >= integer(
            selector["minimum_prior_e4_attempts"]
        ):
            continue

        counters["unknown_creators_seen"] += 1
        if bool(raw.get("is_mayhem_mode")):
            counters["mayhem_rejected"] += 1
            continue

        seed, decision_ns, decision_sequence = paper.creator_seed(
            rows, create, creator
        )
        if seed < finite(selector["minimum_creator_seed_sol"]):
            counters["seed_rejected"] += 1
            continue

        uri = str(raw.get("uri") or create.get("uri") or "")
        if not uri:
            counters["missing_metadata_uri"] += 1
            continue

        preliminaries.append(
            {
                "mint": mint,
                "creator": creator,
                "seed": seed,
                "create_ns": integer(create.get("received_ns")),
                "decision_ns": decision_ns,
                "decision_sequence": decision_sequence,
                "uri": uri,
            }
        )

    preliminaries.sort(
        key=lambda row: (-row["seed"], row["create_ns"], row["mint"])
    )
    deferred = preliminaries[MAX_NEW_CREATOR_METADATA_FETCHES:]
    preliminaries = preliminaries[:MAX_NEW_CREATOR_METADATA_FETCHES]
    counters["metadata_budget_deferred"] = len(deferred)

    missing = [
        row["uri"] for row in preliminaries if row["uri"] not in cache
    ]
    if missing:
        cache.update(await paper.social.fetch_metadata(missing))

    handle_stats = _baseline_handle_stats(baseline_ledger)
    seed_reference = _winner_seed_reference(baseline_ledger)
    costs = paper.load_costs(model)
    policy = paper.load_policy(model)
    scored: list[tuple[paper.Candidate, dict[str, Any]]] = []

    for row in preliminaries:
        metadata = cache.get(row["uri"], {})
        if metadata.get("error"):
            counters[f"metadata_{metadata['error']}"] += 1
            continue

        handle = str(
            metadata.get("handle")
            or paper.social.social_handle(
                str(metadata.get("twitter") or "")
            )
        ).lower().lstrip("@")
        status_id = str(
            metadata.get("status_id")
            or paper.social.social_status_id(
                str(metadata.get("twitter") or "")
            )
        )
        status_ns = paper.social.snowflake_timestamp_ns(status_id)
        if not handle or status_ns <= 0:
            counters["missing_social_identity_or_timestamp"] += 1
            continue

        age = (row["create_ns"] - status_ns) / 1_000_000_000
        if age < 0 or age > 10:
            counters["social_age_outside_window"] += 1
            continue

        early_flow = _early_flow_features(
            traces.get(row["mint"]),
            decision_ns=row["decision_ns"],
        )
        score, components = new_creator_score(
            creator_seed_sol=row["seed"],
            tweet_age_seconds=age,
            handle=handle,
            handle_stats=handle_stats,
            seed_reference=seed_reference,
            metadata_quality=1.0,
            early_flow=early_flow,
        )
        tier = (
            "SHADOW_ELIGIBLE"
            if score >= 0.72
            else "WATCH"
            if score >= 0.60
            else "REJECT"
        )

        candidate = paper.Candidate(
            run_id=run.run_id,
            mint=row["mint"],
            creator=row["creator"],
            handle=handle,
            status_id=status_id,
            tweet_age_seconds=age,
            create_ns=row["create_ns"],
            decision_ns=row["decision_ns"],
            decision_sequence=row["decision_sequence"],
            creator_seed_sol=row["seed"],
        )
        scored.append(
            (
                candidate,
                {
                    "version": NEW_CREATOR_VERSION,
                    "run_id": run.run_id,
                    "mint": row["mint"],
                    "creator": row["creator"],
                    "handle": handle,
                    "decision_ns": row["decision_ns"],
                    "creator_seed_sol": row["seed"],
                    "tweet_age_seconds": age,
                    "score": score,
                    "components": components,
                    "early_flow": early_flow,
                    "tier": tier,
                    "shadow_only": True,
                },
            )
        )

    cash = finite(model["position_sizing"]["starting_bankroll_sol"])
    fraction = finite(model["position_sizing"]["fraction_of_available_cash"])
    max_positions = integer(
        model["position_sizing"]["maximum_concurrent_positions"]
    )
    active: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []

    for candidate, decision in sorted(
        scored, key=lambda pair: (pair[0].decision_ns, pair[0].mint)
    ):
        active, cash = paper.settle(active, cash, candidate.decision_ns)
        outcome: dict[str, Any]
        if decision["tier"] == "REJECT":
            outcome = {
                "status": "NOT_SIMULATED",
                "pnl_sol": None,
                "win": False,
                "rejection_reason": "SHADOW_SCORE_REJECTED",
            }
        elif len(active) >= max_positions:
            counters["shadow_concurrency_rejected"] += 1
            outcome = {
                "status": "CONCURRENCY_REJECTED",
                "pnl_sol": None,
                "win": False,
                "rejection_reason": "MAX_CONCURRENT_POSITIONS",
            }
        else:
            trace = traces.get(candidate.mint)
            if trace is None:
                counters["shadow_missing_trace"] += 1
                outcome = {
                    "status": "MISSING_TRACE",
                    "pnl_sol": None,
                    "win": False,
                    "rejection_reason": "MISSING_TRACE",
                }
            else:
                budget = cash * fraction
                trade, rejection = paper.simulate_trade(
                    run,
                    trace,
                    candidate,
                    budget,
                    model,
                    costs,
                    policy,
                )
                if rejection is not None:
                    fee = min(cash, finite(rejection.get("fee_sol")))
                    cash -= fee
                    counters["shadow_execution_rejected"] += 1
                    outcome = {
                        "status": "REJECTED_BY_EXECUTION",
                        "pnl_sol": None,
                        "win": False,
                        "rejection_reason": rejection.get("reason"),
                        "fee_sol": fee,
                    }
                else:
                    assert trade is not None
                    cash -= finite(trade.get("entry_cost_sol"))
                    active.append(trade)
                    outcome = {
                        "status": "FILLED",
                        "pnl_sol": finite(trade.get("pnl_sol")),
                        "win": finite(trade.get("pnl_sol")) > 0,
                        "rejection_reason": None,
                        "entry_cost_sol": finite(trade.get("entry_cost_sol")),
                        "exit_ns": integer(trade.get("exit_ns")),
                    }
        decision["outcome"] = outcome
        decisions.append(decision)

    active, cash = paper.settle(active, cash, 2**63 - 1)
    if active:
        raise ValueError("new-creator shadow positions remained open after tail")

    counters["scored"] = len(decisions)
    counters["shadow_eligible"] = sum(
        row["tier"] == "SHADOW_ELIGIBLE" for row in decisions
    )
    counters["watch"] = sum(
        row["tier"] == "WATCH" for row in decisions
    )
    return {
        "counts": dict(counters),
        "decisions": decisions,
        "shadow_bankroll": {
            "starting_sol": finite(
                model["position_sizing"]["starting_bankroll_sol"]
            ),
            "ending_sol": cash,
            "net_pnl_sol": cash
            - finite(model["position_sizing"]["starting_bankroll_sol"]),
        },
    }


def _pre_entry_microstructure(
    trace: base.Trace | None,
    *,
    fill_ns: int,
) -> dict[str, float]:
    if trace is None:
        return {
            "buyer_concentration": 0.0,
            "same_slot_buy_share": 0.0,
            "sell_pressure": 0.0,
            "unique_buyers": 0.0,
        }
    points = [point for point in trace.points if point.timestamp_ns <= fill_ns]
    buys = [
        point
        for point in points
        if point.kind.upper() in base.choice_sets.BUY_KINDS
    ]
    sells = [
        point
        for point in points
        if point.kind.upper() in base.choice_sets.SELL_KINDS
    ]
    buy_sol = sum(max(0.0, point.sol_amount) for point in buys)
    sell_sol = sum(max(0.0, point.sol_amount) for point in sells)
    by_buyer: Counter[str] = Counter()
    by_slot: Counter[int] = Counter()
    for point in buys:
        if point.trader:
            by_buyer[point.trader] += max(0.0, point.sol_amount)
        by_slot[point.slot] += 1
    buyer_concentration = (
        max(by_buyer.values()) / max(buy_sol, 1e-12)
        if by_buyer and buy_sol > 0
        else 0.0
    )
    same_slot_buy_share = (
        max(by_slot.values()) / max(len(buys), 1)
        if by_slot
        else 0.0
    )
    sell_pressure = sell_sol / max(buy_sol + sell_sol, 1e-12)
    return {
        "buyer_concentration": clamp(buyer_concentration),
        "same_slot_buy_share": clamp(same_slot_buy_share),
        "sell_pressure": clamp(sell_pressure),
        "unique_buyers": float(
            len({point.trader for point in buys if point.trader})
        ),
    }


def hard_negative_score(
    trade: Mapping[str, Any],
    regime: Mapping[str, Any],
    microstructure: Mapping[str, Any] | None = None,
) -> tuple[float, dict[str, float]]:
    output_ratio = finite(trade.get("output_ratio"), 1.0)
    chase = finite(trade.get("create_to_fill_price_multiple"), 1.0)
    age = finite(trade.get("tweet_age_seconds"))
    seed = finite(trade.get("creator_seed_sol"))
    pressure = finite(regime.get("relative_launch_pressure"), 1.0)
    micro = dict(microstructure or {})
    crowding = max(
        clamp(finite(micro.get("buyer_concentration"))),
        clamp(finite(micro.get("same_slot_buy_share"))),
    )
    sell_pressure = clamp(finite(micro.get("sell_pressure")))
    components = {
        "output_deterioration": clamp((0.82 - output_ratio) / 0.17),
        "chase_pressure": clamp((chase - 1.20) / 0.30),
        "late_social": clamp((age - 5.0) / 5.0),
        "low_creator_seed": clamp((3.0 - seed) / 1.0),
        "market_heat": clamp((pressure - 1.0) / 1.0),
        "pre_entry_crowding": crowding,
        "pre_entry_sell_pressure": sell_pressure,
    }
    legacy_score = (
        0.32 * components["output_deterioration"]
        + 0.30 * components["chase_pressure"]
        + 0.14 * components["late_social"]
        + 0.14 * components["low_creator_seed"]
        + 0.10 * components["market_heat"]
    )
    microstructure_score = (
        0.24 * components["output_deterioration"]
        + 0.22 * components["chase_pressure"]
        + 0.10 * components["late_social"]
        + 0.10 * components["low_creator_seed"]
        + 0.08 * components["market_heat"]
        + 0.16 * components["pre_entry_crowding"]
        + 0.10 * components["pre_entry_sell_pressure"]
    )
    # Preserve every veto the prior hard-negative contract would have made.
    # New causal microstructure is additive evidence, never a way to dilute
    # an already-dangerous execution profile below its historical threshold.
    score = max(legacy_score, microstructure_score)
    return clamp(score), components


def hard_negative_shadow(
    ledger: Sequence[Mapping[str, Any]],
    traces: Mapping[str, base.Trace],
    run_id: str,
    regime: Mapping[str, Any],
) -> list[dict[str, Any]]:
    output = []
    for trade in ledger:
        if str(trade.get("run_id")) != run_id:
            continue
        mint = str(trade.get("mint"))
        micro = _pre_entry_microstructure(
            traces.get(mint),
            fill_ns=integer(trade.get("fill_ns")),
        )
        score, components = hard_negative_score(
            trade, regime, micro
        )
        veto = score >= 0.70
        pnl = finite(trade.get("pnl_sol"))
        output.append(
            {
                "version": HARD_NEGATIVE_VERSION,
                "run_id": run_id,
                "mint": mint,
                "score": score,
                "components": components,
                "pre_entry_microstructure": micro,
                "shadow_veto": veto,
                "actual_pnl_sol": pnl,
                "classification": (
                    "TRUE_VETO_SAVED_LOSS"
                    if veto and pnl <= 0
                    else "FALSE_VETO_KILLED_WINNER"
                    if veto and pnl > 0
                    else "PASS_WINNER"
                    if pnl > 0
                    else "PASS_LOSER"
                ),
                "shadow_only": True,
            }
        )
    return output


def _fill_price(trace: base.Trace, fill_ns: int) -> float:
    prior = [
        point.price_sol
        for point in trace.points
        if point.timestamp_ns <= fill_ns and point.price_sol > 0
    ]
    if prior:
        return prior[-1]
    future = [
        point.price_sol
        for point in trace.points
        if point.timestamp_ns >= fill_ns and point.price_sol > 0
    ]
    return future[0] if future else 0.0


def _absorption_snapshot(
    trace: base.Trace,
    *,
    fill_ns: int,
    horizon_ms: int,
    fill_price: float,
) -> dict[str, Any]:
    cutoff = fill_ns + horizon_ms * 1_000_000
    points = [
        point
        for point in trace.points
        if fill_ns <= point.timestamp_ns <= cutoff
    ]
    buys = [
        point
        for point in points
        if point.kind.upper() in base.choice_sets.BUY_KINDS
    ]
    sells = [
        point
        for point in points
        if point.kind.upper() in base.choice_sets.SELL_KINDS
    ]
    buy_sol = sum(point.sol_amount for point in buys)
    sell_sol = sum(point.sol_amount for point in sells)
    unique_buyers = len(
        {point.trader for point in buys if point.trader}
    )
    unique_sellers = len(
        {point.trader for point in sells if point.trader}
    )
    prices = [
        point.price_sol for point in points if point.price_sol > 0
    ]
    current_price = prices[-1] if prices else fill_price
    trough = min(prices, default=fill_price)
    resilience = current_price / max(fill_price, 1e-18)
    recovery = (
        (current_price - trough)
        / max(fill_price - trough, 1e-18)
        if trough < fill_price
        else 1.0
    )
    flow_ratio = buy_sol / max(sell_sol, 1e-9)
    breadth = unique_buyers / max(
        unique_buyers + unique_sellers, 1
    )
    score = clamp(
        0.35 * clamp(flow_ratio / 2.0)
        + 0.30 * clamp((resilience - 0.70) / 0.50)
        + 0.20 * clamp(recovery)
        + 0.15 * breadth
    )
    action = (
        "HOLD_SUPPORT"
        if score >= 0.68
        else "EXIT_PRESSURE"
        if score <= 0.32
        else "NEUTRAL"
    )
    return {
        "horizon_ms": horizon_ms,
        "buy_sol": buy_sol,
        "sell_sol": sell_sol,
        "buy_sell_ratio": flow_ratio,
        "unique_buyers": unique_buyers,
        "unique_sellers": unique_sellers,
        "price_resilience": resilience,
        "trough_recovery": recovery,
        "score": score,
        "shadow_action": action,
    }


def sell_absorption_shadow(
    ledger: Sequence[Mapping[str, Any]],
    traces: Mapping[str, base.Trace],
    run_id: str,
) -> list[dict[str, Any]]:
    output = []
    for trade in ledger:
        if str(trade.get("run_id")) != run_id:
            continue
        trace = traces.get(str(trade.get("mint")))
        if trace is None:
            continue
        fill_ns = integer(trade.get("fill_ns"))
        fill_price = _fill_price(trace, fill_ns)
        snapshots = [
            _absorption_snapshot(
                trace,
                fill_ns=fill_ns,
                horizon_ms=horizon,
                fill_price=fill_price,
            )
            for horizon in ABSORPTION_HORIZONS_MS
        ]
        output.append(
            {
                "version": SELL_ABSORPTION_VERSION,
                "run_id": run_id,
                "mint": str(trade.get("mint")),
                "actual_pnl_sol": finite(trade.get("pnl_sol")),
                "actual_exit_reason": trade.get("exit_reason"),
                "snapshots": snapshots,
                "shadow_only": True,
            }
        )
    return output


def _row_failed_attempt(row: Mapping[str, Any]) -> bool:
    if failed_registry.looks_like_attempt(row):
        return True
    raw = (
        row.get("raw")
        if isinstance(row.get("raw"), Mapping)
        else {}
    )
    if failed_registry.looks_like_attempt(raw):
        return True
    if row.get("success") is False or raw.get("success") is False:
        return True
    return bool(
        row.get("err")
        or raw.get("err")
        or row.get("error")
        or raw.get("error")
    )


def _failed_attempt_features(
    rows: Sequence[Mapping[str, Any]],
    *,
    cutoff_ns: int,
) -> dict[str, Any]:
    attempts = [
        row
        for row in rows
        if integer(row.get("received_ns")) <= cutoff_ns
        and _row_failed_attempt(row)
    ]
    actors: set[str] = set()
    signatures: set[str] = set()
    notional_sol = 0.0
    for row in attempts:
        raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
        actor = str(
            row.get("trader")
            or row.get("user")
            or raw.get("trader")
            or raw.get("user")
            or raw.get("from")
            or ""
        )
        if actor:
            actors.add(actor)
        signature = str(
            row.get("signature")
            or raw.get("signature")
            or row.get("tx_signature")
            or ""
        )
        if signature:
            signatures.add(signature)
        amount = finite(
            row.get("sol_amount")
            or row.get("amount_sol")
            or row.get("quote_amount")
            or raw.get("sol_amount")
            or raw.get("amount_sol")
            or raw.get("quote_amount")
            or raw.get("input_sol")
        )
        notional_sol += max(0.0, amount)
    return {
        "count": len(attempts),
        "unique_actors": len(actors),
        "unique_signatures": len(signatures),
        "notional_sol": notional_sol,
    }


def failed_aware_shadow(
    run: replay.RunData,
    ledger: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    output = []
    for trade in ledger:
        if str(trade.get("run_id")) != run.run_id:
            continue
        rows = run.events_by_mint.get(str(trade.get("mint")), [])
        decision_ns = integer(trade.get("decision_ns"))
        fill_ns = integer(trade.get("fill_ns"))
        before_decision = _failed_attempt_features(
            rows, cutoff_ns=decision_ns
        )
        before_fill = _failed_attempt_features(
            rows, cutoff_ns=fill_ns
        )
        count_score = min(1.0, before_fill["count"] / 5.0)
        breadth_score = min(1.0, before_fill["unique_actors"] / 3.0)
        notional_score = min(1.0, finite(before_fill["notional_sol"]) / 1.0)
        score = clamp(
            0.40 * count_score
            + 0.25 * breadth_score
            + 0.35 * notional_score
        )
        output.append(
            {
                "version": FAILED_AWARE_VERSION,
                "run_id": run.run_id,
                "mint": str(trade.get("mint")),
                "failed_attempts_before_decision": before_decision["count"],
                "failed_attempts_before_fill": before_fill["count"],
                "failed_unique_actors_before_decision": before_decision[
                    "unique_actors"
                ],
                "failed_unique_actors_before_fill": before_fill[
                    "unique_actors"
                ],
                "failed_notional_sol_before_decision": before_decision[
                    "notional_sol"
                ],
                "failed_notional_sol_before_fill": before_fill[
                    "notional_sol"
                ],
                "failed_intent_score": score,
                "coverage": (
                    "OBSERVED"
                    if before_fill["count"]
                    else "NO_FAILED_INTENT_EVIDENCE"
                ),
                "actual_pnl_sol": finite(trade.get("pnl_sol")),
                "shadow_only": True,
            }
        )
    return output


def validate_integrity(
    paper_state: Mapping[str, Any],
    model: Mapping[str, Any],
    run_id: str,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    expected_hash = paper.stable_hash(model)

    if paper_state.get("model_sha256") != expected_hash:
        errors.append("PAPER_STATE_MODEL_HASH_MISMATCH")
    if paper_state.get("production_authorised") is not False:
        errors.append("PRODUCTION_AUTHORISATION_NOT_FALSE")
    if paper_state.get("real_money_execution") is not False:
        errors.append("REAL_MONEY_EXECUTION_NOT_FALSE")
    if integer(paper_state.get("production_paths_changed")) != 0:
        errors.append("PRODUCTION_PATHS_CHANGED_NONZERO")

    required = integer(model["acceptance_gate"]["closed_trades"])
    if required != 100:
        warnings.append(f"CONFIRMATION_TARGET_IS_{required}_NOT_100")

    ledger = list(paper_state.get("ledger", []))
    rejections = list(paper_state.get("rejections", []))
    processed_runs = [str(value) for value in paper_state.get("processed_runs", [])]
    if len(ledger) > required:
        errors.append("LEDGER_EXCEEDS_REQUIRED_SAMPLE")
    if len(processed_runs) != len(set(processed_runs)):
        errors.append("DUPLICATE_PROCESSED_RUN_IDS")

    seen: set[tuple[str, str]] = set()
    duplicate_keys = 0
    pnls: list[float] = []
    wins = 0
    for row in ledger:
        key = (str(row.get("run_id")), str(row.get("mint")))
        if key in seen:
            duplicate_keys += 1
        seen.add(key)
        decision_ns = integer(row.get("decision_ns"))
        fill_ns = integer(row.get("fill_ns"))
        exit_ns = integer(row.get("exit_ns"))
        if not decision_ns <= fill_ns <= exit_ns:
            errors.append(
                f"NON_MONOTONIC_TRADE_TIMESTAMPS:{key[0]}:{key[1]}"
            )
        parsed: dict[str, float] = {}
        for field in ("entry_cost_sol", "pnl_sol", "proceeds_sol"):
            try:
                value = float(row.get(field))
            except (TypeError, ValueError):
                errors.append(
                    f"NON_NUMERIC_TRADE_VALUE:{field}:{key[0]}:{key[1]}"
                )
                continue
            if not math.isfinite(value):
                errors.append(
                    f"NONFINITE_TRADE_VALUE:{field}:{key[0]}:{key[1]}"
                )
            parsed[field] = value
        if {"entry_cost_sol", "pnl_sol", "proceeds_sol"} <= parsed.keys():
            implied = parsed["proceeds_sol"] - parsed["entry_cost_sol"]
            tolerance = max(1e-10, abs(parsed["pnl_sol"]) * 1e-9)
            if abs(implied - parsed["pnl_sol"]) > tolerance:
                errors.append(
                    f"TRADE_PNL_CASHFLOW_MISMATCH:{key[0]}:{key[1]}"
                )
            pnls.append(parsed["pnl_sol"])
            actual_win = parsed["pnl_sol"] > 0
            wins += int(actual_win)
            if "win" in row and bool(row.get("win")) != actual_win:
                errors.append(
                    f"TRADE_WIN_FLAG_MISMATCH:{key[0]}:{key[1]}"
                )

    if duplicate_keys:
        errors.append(f"DUPLICATE_RUN_MINT_KEYS:{duplicate_keys}")

    rejection_seen: set[tuple[str, str]] = set()
    rejection_fees = 0.0
    for row in rejections:
        key = (str(row.get("run_id")), str(row.get("mint")))
        if key in rejection_seen:
            errors.append(f"DUPLICATE_REJECTION_KEY:{key[0]}:{key[1]}")
        rejection_seen.add(key)
        fee = finite(row.get("fee_sol"), -1.0)
        if fee < 0:
            errors.append(f"INVALID_REJECTION_FEE:{key[0]}:{key[1]}")
        else:
            rejection_fees += fee

    starting = finite(paper_state.get("starting_bankroll_sol"))
    actual_ending = finite(paper_state.get("ending_bankroll_sol"))
    expected_ending = starting + sum(pnls) - rejection_fees
    cash_tolerance = max(1e-9, abs(expected_ending) * 1e-9)
    if abs(actual_ending - expected_ending) > cash_tolerance:
        errors.append("ENDING_BANKROLL_RECONCILIATION_FAILED")

    metrics = paper_state.get("metrics") or {}
    if metrics:
        if integer(metrics.get("closed_trades")) != len(ledger):
            errors.append("METRIC_CLOSED_TRADES_MISMATCH")
        if integer(metrics.get("wins")) != wins:
            errors.append("METRIC_WINS_MISMATCH")
        if integer(metrics.get("losses")) != len(ledger) - wins:
            errors.append("METRIC_LOSSES_MISMATCH")
        if abs(finite(metrics.get("net_pnl_sol")) - sum(pnls)) > 1e-9:
            errors.append("METRIC_NET_PNL_MISMATCH")

    completion = paper_state.get("completion") or {}
    expected_remaining = max(0, required - len(ledger))
    if integer(completion.get("remaining"), expected_remaining) != expected_remaining:
        errors.append("COMPLETION_REMAINING_MISMATCH")
    expected_reached = len(ledger) >= required
    if bool(completion.get("reached")) != expected_reached:
        errors.append("COMPLETION_REACHED_MISMATCH")

    if run_id not in set(processed_runs):
        warnings.append("CURRENT_RUN_NOT_YET_PERSISTED_IN_PAPER_STATE")

    return {
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "warnings": warnings,
        "ledger_rows": len(ledger),
        "rejection_rows": len(rejections),
        "required_closed_trades": required,
        "model_hash": expected_hash,
        "cash_reconciliation": {
            "starting_bankroll_sol": starting,
            "trade_pnl_sol": sum(pnls),
            "rejected_entry_fees_sol": rejection_fees,
            "expected_ending_bankroll_sol": expected_ending,
            "actual_ending_bankroll_sol": actual_ending,
        },
    }


def _dedupe_extend(
    existing: list[dict[str, Any]],
    incoming: Sequence[Mapping[str, Any]],
    keys: Sequence[str],
) -> list[dict[str, Any]]:
    index = {
        tuple(str(row.get(key)) for key in keys): row
        for row in existing
    }
    for row in incoming:
        index[tuple(str(row.get(key)) for key in keys)] = dict(row)
    return list(index.values())


def render_report(state: Mapping[str, Any]) -> str:
    concentration = state.get("creator_concentration", {})
    drift = state.get("drift", {})
    risk = state.get("risk_of_ruin", {})
    stress = state.get("execution_stress_summary", {})
    new_creator = state.get("new_creator_summary", {})
    reliability = state.get("reliability", {})

    lines = [
        "# V12 Pre-Armed shadow intelligence",
        "",
        f"Status: **{reliability.get('status', 'UNKNOWN')}**",
        "",
        (
            "This sidecar never changes V12 Pre-Armed selection, sizing, "
            "execution guards or exits."
        ),
        "",
        "## Creator concentration",
        "",
        f"- Creators observed: {concentration.get('creator_count', 0)}",
        (
            "- Effective creator count: "
            f"{finite(concentration.get('effective_creator_count')):.2f}"
        ),
        (
            "- Top creator trade share: "
            f"{finite(concentration.get('top_creator_trade_share')):.2%}"
        ),
        (
            "- Top 3 trade share: "
            f"{finite(concentration.get('top3_trade_share')):.2%}"
        ),
        "",
        "## Drift",
        "",
        f"- Drift status: {drift.get('status', 'UNKNOWN')}",
        f"- Flags: {', '.join(drift.get('flags', [])) or 'none'}",
        "",
        "## Bankroll risk simulation",
        "",
        f"- Source trade samples: {risk.get('source_trade_samples', 0)}",
        (
            f"- 20% DD probability over {risk.get('horizon_trades', 0)} "
            f"trades: {finite(risk.get('probability_drawdown_ge_20pct')):.2%}"
        ),
        (
            "- Bankroll <= 1 SOL probability: "
            f"{finite(risk.get('probability_bankroll_le_1_sol')):.2%}"
        ),
        "",
        "## New creator shadow",
        "",
        f"- Decisions scored: {new_creator.get('decisions', 0)}",
        f"- Shadow eligible: {new_creator.get('shadow_eligible', 0)}",
        "",
        "## Execution stress",
        "",
    ]

    for key, value in sorted(stress.items()):
        lines.append(
            f"- {key}: {value.get('filled', 0)}/"
            f"{value.get('attempted', 0)} filled, "
            f"PnL {finite(value.get('net_pnl_sol')):+.4f} SOL, "
            f"PF {finite(value.get('profit_factor')):.2f}"
        )

    lines += [
        "",
        "## Integrity",
        "",
        f"- Reliability status: {reliability.get('status', 'UNKNOWN')}",
        f"- Guardrail state: {state.get('guardrails', {}).get('state', 'UNKNOWN')}",
        f"- Guardrail halts: {', '.join(state.get('guardrails', {}).get('hard_halts', [])) or 'none'}",
        f"- Guardrail warnings: {', '.join(state.get('guardrails', {}).get('warnings', [])) or 'none'}",
        f"- Errors: {', '.join(reliability.get('errors', [])) or 'none'}",
        (
            "- Warnings: "
            f"{', '.join(reliability.get('warnings', [])) or 'none'}"
        ),
        f"- Processed shadow windows: {len(state.get('processed_runs', []))}",
        "",
    ]
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    model = read_json(args.model, {})
    paper_state = read_json(args.paper_state, {})
    baseline_state = read_json(args.baseline, {})
    baseline_ledger = list(baseline_state.get("ledger", []))

    run = replay.load_run(args.run_id, args.batch, args.events)
    traces = paper.trace_map(args.run_id, args.events)
    cache = paper.read_cache(args.metadata_cache)

    analytics = read_json(args.analytics_state, None)
    if analytics is None:
        analytics = {
            "version": SCHEMA_VERSION,
            "model_sha256": paper.stable_hash(model),
            "baseline_50_sha256": sha256_path(args.baseline),
            "processed_runs": [],
            "windows": [],
            "rejected_counterfactuals": [],
            "execution_stress": [],
            "new_creator_shadow": [],
            "hard_negative_shadow": [],
            "sell_absorption_shadow": [],
            "failed_aware_shadow": [],
        }

    if analytics.get("model_sha256") != paper.stable_hash(model):
        raise ValueError("shadow analytics model fingerprint changed")
    if analytics.get("baseline_50_sha256") != sha256_path(
        args.baseline
    ):
        raise ValueError("locked 50-trade baseline fingerprint changed")

    candidates, candidate_counts = await paper.candidates_for_run(
        run, model, cache
    )
    regime = regime_snapshot(
        run, traces, candidate_counts, analytics.get("windows", [])
    )
    reliability = validate_integrity(
        paper_state, model, args.run_id
    )
    if reliability["status"] != "PASS":
        raise ValueError(
            "shadow integrity failed: "
            + ",".join(reliability["errors"])
        )

    already_processed = args.run_id in {
        str(value) for value in analytics.get("processed_runs", [])
    }

    if not already_processed:
        counterfactuals = rejected_counterfactuals(
            run, traces, candidates, paper_state, model
        )
        stress = execution_stress_records(
            run, traces, candidates, paper_state, model
        )
        new_creator = await new_creator_shadow(
            run,
            traces,
            model,
            cache,
            baseline_ledger,
        )
        hard_negative = hard_negative_shadow(
            paper_state.get("ledger", []),
            traces,
            args.run_id,
            regime,
        )
        absorption = sell_absorption_shadow(
            paper_state.get("ledger", []),
            traces,
            args.run_id,
        )
        failed_aware = failed_aware_shadow(
            run, paper_state.get("ledger", [])
        )

        analytics["processed_runs"].append(args.run_id)
        analytics["windows"] = _dedupe_extend(
            list(analytics.get("windows", [])),
            [regime],
            ("run_id",),
        )
        analytics["rejected_counterfactuals"] = _dedupe_extend(
            list(
                analytics.get("rejected_counterfactuals", [])
            ),
            counterfactuals,
            ("run_id", "mint"),
        )
        analytics["execution_stress"] = _dedupe_extend(
            list(analytics.get("execution_stress", [])),
            stress,
            ("run_id", "mint", "latency_ms", "fee_multiplier"),
        )
        analytics["new_creator_shadow"] = _dedupe_extend(
            list(analytics.get("new_creator_shadow", [])),
            new_creator["decisions"],
            ("run_id", "mint"),
        )
        analytics["hard_negative_shadow"] = _dedupe_extend(
            list(analytics.get("hard_negative_shadow", [])),
            hard_negative,
            ("run_id", "mint"),
        )
        analytics["sell_absorption_shadow"] = _dedupe_extend(
            list(analytics.get("sell_absorption_shadow", [])),
            absorption,
            ("run_id", "mint"),
        )
        analytics["failed_aware_shadow"] = _dedupe_extend(
            list(analytics.get("failed_aware_shadow", [])),
            failed_aware,
            ("run_id", "mint"),
        )
        analytics["last_new_creator_counts"] = new_creator[
            "counts"
        ]

    ledger = list(paper_state.get("ledger", []))
    analytics["creator_concentration"] = creator_concentration(
        ledger
    )
    analytics["drift"] = drift_telemetry(
        ledger, baseline_ledger
    )
    analytics["risk_of_ruin"] = risk_of_ruin(
        ledger,
        starting_bankroll=finite(
            model["position_sizing"]["starting_bankroll_sol"]
        ),
        position_fraction=finite(
            model["position_sizing"]["fraction_of_available_cash"]
        ),
        model_hash=paper.stable_hash(model),
    )
    analytics["execution_stress_summary"] = (
        aggregate_execution_stress(
            analytics.get("execution_stress", [])
        )
    )

    new_rows = analytics.get("new_creator_shadow", [])
    analytics["new_creator_summary"] = {
        "decisions": len(new_rows),
        "shadow_eligible": sum(
            row.get("tier") == "SHADOW_ELIGIBLE"
            for row in new_rows
        ),
        "watch": sum(
            row.get("tier") == "WATCH" for row in new_rows
        ),
        "filled_shadow_eligible": sum(
            row.get("tier") == "SHADOW_ELIGIBLE"
            and row.get("outcome", {}).get("status") == "FILLED"
            for row in new_rows
        ),
        "profitable_shadow_eligible": sum(
            row.get("tier") == "SHADOW_ELIGIBLE"
            and row.get("outcome", {}).get("win") is True
            for row in new_rows
        ),
    }

    analytics["reliability"] = {
        **reliability,
        "input_sha256": {
            "model": sha256_path(args.model),
            "paper_state": sha256_path(args.paper_state),
            "baseline": sha256_path(args.baseline),
            "batch": sha256_path(args.batch),
            "events": sha256_path(args.events),
        },
        "shadow_only": True,
        "frozen_v12_modified": False,
    }
    dependency_summary = read_json(args.dependency_summary, None)
    analytics["guardrails"] = guardrail_policy.evaluate_guardrails(
        paper_state=paper_state,
        shadow_state=analytics,
        dependency_summary=dependency_summary,
    )
    analytics["latest_run_id"] = args.run_id
    analytics["paper_progress"] = dict(
        paper_state.get("completion", {})
    )

    paper.atomic_json(args.analytics_state, analytics)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        render_report(analytics), encoding="utf-8"
    )
    for path in args.metadata_cache:
        paper.atomic_json(path, cache)
        break

    return analytics


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--run-id", required=True)
    value.add_argument("--batch", type=Path, required=True)
    value.add_argument("--events", type=Path, required=True)
    value.add_argument("--paper-state", type=Path, required=True)
    value.add_argument("--model", type=Path, required=True)
    value.add_argument("--baseline", type=Path, required=True)
    value.add_argument(
        "--analytics-state", type=Path, required=True
    )
    value.add_argument("--report", type=Path, required=True)
    value.add_argument(
        "--dependency-summary",
        type=Path,
        default=Path("research/e4-builder-security-status.json"),
    )
    value.add_argument(
        "--metadata-cache",
        action="append",
        type=Path,
        default=[],
    )
    return value


def main() -> int:
    args = parser().parse_args()
    result = asyncio.run(run(args))
    print(
        json.dumps(
            {
                "version": result["version"],
                "latest_run_id": result["latest_run_id"],
                "paper_progress": result["paper_progress"],
                "drift": result["drift"],
                "new_creator_summary": result[
                    "new_creator_summary"
                ],
                "guardrails": result["guardrails"],
                "reliability": result["reliability"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Replay the frozen development selections against exact traces and latencies."""

from __future__ import annotations

import json
import pickle
from collections import Counter
from pathlib import Path
from typing import Any

import e4_v12_adaptive_exit_search as adaptive
import e4_v12_profit_survival_search as base

ResearchRow = base.ResearchRow
Outcome = base.Outcome
Point = base.Point
Trace = base.Trace
Policy = base.Policy

DEVELOPMENT = Path("artifacts/e4-v12-online-conformal-development.json")
OUTPUT = Path("artifacts/e4-v12-online-conformal-latency.json")
ACTOR_TRACES = Path(".tmp-regime-veto-holdout/actor-scorecard-traces.pkl")
CURRENT_CACHE = Path(".tmp-relative-price-capped-actor-holdout/combined-rows.pkl")
TRACE_CACHE = Path(".tmp-relative-price-capped-actor-holdout/conformal-selected-traces.pkl")
POLICY_INDEX = 12
LATENCIES_MS = (0, 1, 2, 5, 10)


def compact(metrics: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metrics.items() if key != "ledger"}


def replay(
    selected: list[dict[str, Any]],
    traces: dict[tuple[str, str], base.Trace],
    latency_ms: int,
) -> dict[str, Any]:
    candidates = sorted(
        selected,
        key=lambda row: (int(row["run_id"]), int(row["decision_ns"]), str(row["mint"])),
    )
    bankroll = base.STARTING_BANKROLL_SOL
    active: list[int] = []
    ledger = []
    rejected_concurrency = 0
    rejected_quote = 0
    for candidate in candidates:
        decision_ns = int(candidate["decision_ns"])
        active = [exit_ns for exit_ns in active if exit_ns > decision_ns]
        if len(active) >= base.MAX_CONCURRENT_POSITIONS:
            rejected_concurrency += 1
            continue
        trace = traces[(str(candidate["run_id"]), str(candidate["mint"]))]
        outcome = adaptive.adaptive_outcome(
            trace,
            250,
            adaptive.POLICIES[POLICY_INDEX],
            latency_ms=latency_ms,
        )
        if outcome is None:
            rejected_quote += 1
            continue
        position = bankroll * base.POSITION_FRACTION
        pnl_sol = base.net_pnl(outcome, 0.001, position)
        bankroll += pnl_sol
        active.append(decision_ns + int(outcome.exit_offset_ms * 1_000_000))
        ledger.append(
            {
                "run_id": str(candidate["run_id"]),
                "mint": str(candidate["mint"]),
                "decision_ns": decision_ns,
                "latency_ms": latency_ms,
                "position_sol": position,
                "gross_multiple": outcome.gross_multiple,
                "pnl_sol": pnl_sol,
                "win": pnl_sol > 0,
                "exit_offset_ms": outcome.exit_offset_ms,
                "exit_reason": outcome.exit_reason,
            }
        )
    profits = [row["pnl_sol"] for row in ledger if row["pnl_sol"] > 0]
    losses = [row["pnl_sol"] for row in ledger if row["pnl_sol"] <= 0]
    equity = base.STARTING_BANKROLL_SOL
    peak = equity
    drawdown = 0.0
    by_window: dict[str, dict[str, Any]] = {}
    for row in ledger:
        equity += row["pnl_sol"]
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        item = by_window.setdefault(row["run_id"], {"trades": 0, "wins": 0, "pnl_sol": 0.0})
        item["trades"] += 1
        item["wins"] += int(row["win"])
        item["pnl_sol"] += row["pnl_sol"]
    return {
        "latency_ms": latency_ms,
        "attempted_quotes": len(candidates),
        "trades": len(ledger),
        "wins": len(profits),
        "win_rate": len(profits) / max(len(ledger), 1),
        "wilson_95_lower_bound": base.wilson_lower_bound(len(profits), len(ledger)),
        "net_pnl_sol": bankroll - base.STARTING_BANKROLL_SOL,
        "profit_factor": sum(profits) / max(abs(sum(losses)), 1e-12),
        "maximum_drawdown_fraction": drawdown / base.STARTING_BANKROLL_SOL,
        "capture_windows": len(by_window),
        "profitable_capture_windows": sum(row["pnl_sol"] > 0 for row in by_window.values()),
        "largest_winner_contribution": max(profits, default=0.0) / max(sum(profits), 1e-12),
        "quote_coverage": len(ledger) / max(len(candidates), 1),
        "rejected_concurrency": rejected_concurrency,
        "rejected_quote": rejected_quote,
        "by_capture_window": by_window,
        "exit_reasons": dict(Counter(row["exit_reason"] for row in ledger)),
        "ledger_hash": base.stable_hash(ledger),
        "ledger": ledger,
    }


def load_selected_traces(keys: set[tuple[str, str]]) -> tuple[dict[tuple[str, str], base.Trace], dict[str, Any]]:
    if TRACE_CACHE.exists():
        with TRACE_CACHE.open("rb") as handle:
            cached = pickle.load(handle)
        if set(cached) == keys:
            return cached, {"cache_hit": True, "selected_traces": len(cached)}
    with ACTOR_TRACES.open("rb") as handle:
        actor_payload = pickle.load(handle)
    with CURRENT_CACHE.open("rb") as handle:
        current_payload = pickle.load(handle)
    traces = {
        key: trace
        for source in (actor_payload["traces"], current_payload["traces"])
        for key, trace in source.items()
        if key in keys
    }
    specifications = {
        spec.run_id: spec
        for spec in adaptive.capture_specs(Path.cwd(), include_holdout=True)
    }
    creator_counts: Counter[str] = Counter()
    missing = keys - set(traces)
    parsed_runs = []
    for run_id in sorted({key[0] for key in missing}, key=int):
        spec = specifications[run_id]
        print(f"parse selected trace gaps {run_id}", flush=True)
        if base.sha256_path(spec.events_path) != spec.expected_sha256:
            raise ValueError(f"capture hash changed: {run_id}")
        candidates, parse_errors = base.load_capture(spec, creator_counts)
        if parse_errors:
            raise ValueError(f"capture contains parse errors: {run_id}")
        wanted = {key for key in missing if key[0] == run_id}
        traces.update(
            {
                (trace.run_id, trace.mint): trace
                for trace in candidates
                if (trace.run_id, trace.mint) in wanted
            }
        )
        parsed_runs.append(run_id)
    still_missing = keys - set(traces)
    if still_missing:
        raise ValueError(f"selected traces remain missing: {sorted(still_missing)[:10]}")
    traces = {key: traces[key] for key in sorted(keys, key=lambda key: (int(key[0]), key[1]))}
    with TRACE_CACHE.open("wb") as handle:
        pickle.dump(traces, handle)
    return traces, {
        "cache_hit": False,
        "selected_traces": len(traces),
        "actor_trace_hits": len(keys & set(actor_payload["traces"])),
        "recent_trace_hits": len(keys & set(current_payload["traces"])),
        "parsed_gap_runs": parsed_runs,
    }


def main() -> dict[str, Any]:
    development = json.loads(DEVELOPMENT.read_text(encoding="utf-8"))
    folds = [list(fold["ledger"]) for fold in development["folds"]]
    keys = {
        (str(row["run_id"]), str(row["mint"]))
        for selected in folds
        for row in selected
    }
    traces, trace_audit = load_selected_traces(keys)
    latency_results = {}
    for latency_ms in LATENCIES_MS:
        fold_results = [replay(selected, traces, latency_ms) for selected in folds]
        closed = [row for fold in fold_results for row in fold["ledger"]]
        profits = [row["pnl_sol"] for row in closed if row["pnl_sol"] > 0]
        losses = [row["pnl_sol"] for row in closed if row["pnl_sol"] <= 0]
        trades = len(closed)
        wins = len(profits)
        windows = {
            key: value
            for fold in fold_results
            for key, value in fold["by_capture_window"].items()
        }
        latency_results[str(latency_ms)] = {
            "latency_ms": latency_ms,
            "trades": trades,
            "wins": wins,
            "win_rate": wins / max(trades, 1),
            "wilson_95_lower_bound": base.wilson_lower_bound(wins, trades),
            "net_pnl_sol": sum(fold["net_pnl_sol"] for fold in fold_results),
            "profit_factor": sum(profits) / max(abs(sum(losses)), 1e-12),
            "maximum_fold_drawdown_fraction": max(fold["maximum_drawdown_fraction"] for fold in fold_results),
            "minimum_fold_win_rate": min(fold["win_rate"] for fold in fold_results),
            "minimum_fold_profit_factor": min(fold["profit_factor"] for fold in fold_results),
            "positive_folds": sum(fold["net_pnl_sol"] > 0 for fold in fold_results),
            "capture_windows": len(windows),
            "profitable_capture_windows": sum(row["pnl_sol"] > 0 for row in windows.values()),
            "largest_winner_contribution": max(profits, default=0.0) / max(sum(profits), 1e-12),
            "quote_coverage": trades / max(sum(fold["attempted_quotes"] for fold in fold_results), 1),
            "rejected_concurrency": sum(fold["rejected_concurrency"] for fold in fold_results),
            "rejected_quote": sum(fold["rejected_quote"] for fold in fold_results),
            "ledger_hash": base.stable_hash(closed),
            "folds": [compact(fold) for fold in fold_results],
        }
    zero = latency_results["0"]
    requirements = {
        "minimum_closed_trades": zero["trades"] >= 300,
        "minimum_win_rate": zero["win_rate"] >= 0.65,
        "minimum_wilson_bound": zero["wilson_95_lower_bound"] >= 0.60,
        "positive_net_pnl": zero["net_pnl_sol"] > 0,
        "minimum_profit_factor": zero["profit_factor"] >= 1.25,
        "every_fold_minimum_trades": all(fold["trades"] >= 50 for fold in zero["folds"]),
        "every_fold_minimum_win_rate": zero["minimum_fold_win_rate"] >= 0.55,
        "every_fold_positive": zero["positive_folds"] == len(folds),
        "every_fold_minimum_profit_factor": zero["minimum_fold_profit_factor"] >= 1.10,
        "maximum_drawdown": zero["maximum_fold_drawdown_fraction"] <= 0.15,
        "largest_winner_limit": zero["largest_winner_contribution"] <= 0.15,
        "every_latency_positive": all(row["net_pnl_sol"] > 0 for row in latency_results.values()),
        "every_latency_profit_factor": all(row["profit_factor"] >= 1.25 for row in latency_results.values()),
        "every_latency_full_quote_coverage": all(row["quote_coverage"] == 1.0 for row in latency_results.values()),
    }
    output = {
        "version": "e4-v12-online-conformal-latency-v1",
        "thesis_family": "window-conformal-stable-precision-v12",
        "experiment_id": development["experiment_id"],
        "policy_index": POLICY_INDEX,
        "policy": adaptive.POLICIES[POLICY_INDEX].key,
        "trace_audit": trace_audit,
        "latencies": latency_results,
        "requirements": requirements,
        "exact_trace_gate_passed": all(requirements.values()),
        "production_paths_changed": 0,
        "untouched_holdout_passed": False,
        "production_promotion_authorised": False,
        "production_deployment_authorised": False,
    }
    base.write_json(OUTPUT, output)
    print(json.dumps(output, indent=2, sort_keys=True))
    return output


if __name__ == "__main__":
    main()

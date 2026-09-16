#!/usr/bin/env python3
"""Locate the frozen candidate's exact latency-gate break point.

This supplementary diagnostic replays the unchanged frozen selections and
exact source traces.  It does not alter the registered latency contract,
candidate, untouched-live evidence, or any production path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import e4_v12_online_conformal_latency as latency
import e4_v12_profit_survival_search as base

OUTPUT = Path("artifacts/e4-v12-candidate-latency-breakpoint.json")
SCHEMA_VERSION = "e4-v12-candidate-latency-breakpoint-v1"
LATENCIES_MS = (0, 1, 2, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50)
SOURCE_FILES = (
    "scripts/e4_v12_candidate_latency_breakpoint.py",
    "scripts/e4_v12_online_conformal_latency.py",
    "scripts/e4_v12_online_conformal_precision.py",
    "scripts/e4_v12_adaptive_exit_search.py",
    "scripts/e4_v12_profit_survival_search.py",
)


def sha256_lf(path: Path) -> str:
    return __import__("hashlib").sha256(
        path.read_bytes().replace(b"\r\n", b"\n")
    ).hexdigest()


def source_code_fingerprint(root: Path | None = None) -> str:
    root = Path.cwd() if root is None else root
    return base.stable_hash(
        {relative: sha256_lf(root / relative) for relative in SOURCE_FILES}
    )


def summarize(folds: list[dict[str, Any]], delay_ms: int) -> dict[str, Any]:
    ledger = [trade for fold in folds for trade in fold["ledger"]]
    profits = [row["pnl_sol"] for row in ledger if row["pnl_sol"] > 0]
    losses = [row["pnl_sol"] for row in ledger if row["pnl_sol"] <= 0]
    trades = len(ledger)
    wins = len(profits)
    attempted = sum(fold["attempted_quotes"] for fold in folds)
    metrics = {
        "latency_ms": delay_ms,
        "attempted_quotes": attempted,
        "trades": trades,
        "wins": wins,
        "win_rate": wins / max(trades, 1),
        "wilson_95_lower_bound": base.wilson_lower_bound(wins, trades),
        "net_pnl_sol": sum(fold["net_pnl_sol"] for fold in folds),
        "profit_factor": sum(profits) / max(abs(sum(losses)), 1e-12),
        "maximum_fold_drawdown_fraction": max(
            fold["maximum_drawdown_fraction"] for fold in folds
        ),
        "minimum_fold_win_rate": min(fold["win_rate"] for fold in folds),
        "minimum_fold_profit_factor": min(
            fold["profit_factor"] for fold in folds
        ),
        "positive_folds": sum(fold["net_pnl_sol"] > 0 for fold in folds),
        "quote_coverage": trades / max(attempted, 1),
        "ledger_hash": base.stable_hash(ledger),
        "folds": [
            {
                "fold": index,
                "trades": fold["trades"],
                "wins": fold["wins"],
                "win_rate": fold["win_rate"],
                "net_pnl_sol": fold["net_pnl_sol"],
                "profit_factor": fold["profit_factor"],
                "maximum_drawdown_fraction": fold[
                    "maximum_drawdown_fraction"
                ],
                "quote_coverage": fold["quote_coverage"],
            }
            for index, fold in enumerate(folds)
        ],
    }
    strict_precision_requirements = {
        "minimum_closed_trades": trades >= 300,
        "minimum_win_rate": metrics["win_rate"] >= 0.65,
        "minimum_wilson_bound": metrics["wilson_95_lower_bound"] >= 0.60,
        "positive_net_pnl": metrics["net_pnl_sol"] > 0,
        "minimum_profit_factor": metrics["profit_factor"] >= 1.25,
        "every_fold_minimum_trades": all(fold["trades"] >= 50 for fold in folds),
        "every_fold_minimum_win_rate": metrics["minimum_fold_win_rate"] >= 0.55,
        "every_fold_positive": metrics["positive_folds"] == len(folds),
        "every_fold_minimum_profit_factor": (
            metrics["minimum_fold_profit_factor"] >= 1.10
        ),
        "maximum_drawdown": metrics["maximum_fold_drawdown_fraction"] <= 0.15,
        "full_quote_coverage": metrics["quote_coverage"] == 1.0,
    }
    registered_latency_economic_requirements = {
        "positive_net_pnl": metrics["net_pnl_sol"] > 0,
        "minimum_profit_factor": metrics["profit_factor"] >= 1.25,
        "full_quote_coverage": metrics["quote_coverage"] == 1.0,
    }
    metrics["strict_precision_requirements"] = strict_precision_requirements
    metrics["strict_precision_gate_passed"] = all(
        strict_precision_requirements.values()
    )
    metrics["registered_latency_economic_requirements"] = (
        registered_latency_economic_requirements
    )
    metrics["registered_latency_economic_gate_passed"] = all(
        registered_latency_economic_requirements.values()
    )
    return metrics


def main() -> dict[str, Any]:
    development = json.loads(latency.DEVELOPMENT.read_text(encoding="utf-8"))
    selections = [list(fold["ledger"]) for fold in development["folds"]]
    keys = {
        (str(row["run_id"]), str(row["mint"]))
        for fold in selections
        for row in fold
    }
    traces, trace_audit = latency.load_selected_traces(keys)
    frontier = [
        summarize(
            [latency.replay(fold, traces, delay) for fold in selections],
            delay,
        )
        for delay in LATENCIES_MS
    ]
    strict_passing = [
        row["latency_ms"]
        for row in frontier
        if row["strict_precision_gate_passed"]
    ]
    strict_failing = [
        row["latency_ms"]
        for row in frontier
        if not row["strict_precision_gate_passed"]
    ]
    economic_passing = [
        row["latency_ms"]
        for row in frontier
        if row["registered_latency_economic_gate_passed"]
    ]
    identity = {
        "source_experiment_id": development["experiment_id"],
        "source_code_fingerprint": source_code_fingerprint(),
        "dataset_source_manifest_fingerprint": development["identity"][
            "dataset_source_manifest_fingerprint"
        ],
        "latencies_ms": list(LATENCIES_MS),
        "selection_policy": "unchanged frozen development selections",
        "entry_policy": "first exact trace quote at or after 250ms plus latency",
        "exit_policy": development["policy"],
        "supplementary_strict_precision_gate_policy": {
            "minimum_closed_trades": 300,
            "minimum_win_rate": 0.65,
            "minimum_wilson_95_lower_bound": 0.60,
            "minimum_profit_factor": 1.25,
            "minimum_fold_win_rate": 0.55,
            "minimum_fold_profit_factor": 1.10,
            "maximum_fold_drawdown_fraction": 0.15,
            "positive_folds": 4,
            "quote_coverage": 1.0,
        },
        "registered_per_latency_economic_gate_policy": {
            "net_pnl_positive": True,
            "minimum_profit_factor": 1.25,
            "quote_coverage": 1.0,
        },
    }
    output = {
        "version": SCHEMA_VERSION,
        "diagnostic_id": f"e4d-{base.stable_hash(identity)}",
        "identity": identity,
        "source_experiment_id": development["experiment_id"],
        "trace_audit": trace_audit,
        "frontier": frontier,
        "maximum_tested_strict_precision_passing_latency_ms": (
            max(strict_passing) if strict_passing else None
        ),
        "minimum_tested_strict_precision_failing_latency_ms": (
            min(strict_failing) if strict_failing else None
        ),
        "maximum_tested_registered_economic_passing_latency_ms": (
            max(economic_passing) if economic_passing else None
        ),
        "registered_candidate_latency_contract_changed": False,
        "candidate_refit": False,
        "active_untouched_live_data_used": False,
        "untouched_holdout_passed": False,
        "live_confirmation_authorised": False,
        "production_promotion_authorised": False,
        "production_deployment_authorised": False,
        "production_paths_changed": 0,
    }
    base.write_json(OUTPUT, output)
    print(json.dumps(output, indent=2, sort_keys=True))
    return output


if __name__ == "__main__":
    main()

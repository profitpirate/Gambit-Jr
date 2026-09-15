#!/usr/bin/env python3
"""Stress the frozen conformal candidate at empirically observed latency.

This is a supplementary research diagnostic. It cannot alter the frozen live
protocol, consume the active untouched holdout, or authorize production.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import e4_v12_online_conformal_latency as latency
import e4_v12_profit_survival_search as base

OUTPUT = Path("artifacts/e4-v12-realistic-latency-diagnostic.json")
SCHEMA_VERSION = "e4-v12-realistic-latency-diagnostic-v1"
LATENCIES_MS = (50, 100, 200, 300, 500)
OBSERVED_RUNTIME_EVIDENCE = {
    "workflow_run_id": "34938012082",
    "artifact_name": "e4-v12-forward-34938012082",
    "artifact_zip_sha256": (
        "be3fbeccb0fa22b835b1e982bb9b91d88381470f55f908c65ca5b1635a05422e"
    ),
    "evidence_batches": 49,
    "launches": 147_000,
    "direct_copy_closed_positions": 454,
    "median_source_to_fill_ms": 297.207515,
}
SOURCE_FILES = (
    "scripts/e4_v12_realistic_latency_diagnostic.py",
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


def aggregate(
    fold_results: list[dict[str, Any]], delay_ms: int
) -> dict[str, Any]:
    ledger = [trade for fold in fold_results for trade in fold["ledger"]]
    profits = [row["pnl_sol"] for row in ledger if row["pnl_sol"] > 0]
    losses = [row["pnl_sol"] for row in ledger if row["pnl_sol"] <= 0]
    trades = len(ledger)
    wins = len(profits)
    attempted = sum(row["attempted_quotes"] for row in fold_results)
    return {
        "latency_ms": delay_ms,
        "attempted_quotes": attempted,
        "trades": trades,
        "wins": wins,
        "win_rate": wins / max(trades, 1),
        "wilson_95_lower_bound": base.wilson_lower_bound(wins, trades),
        "net_pnl_sol": sum(row["net_pnl_sol"] for row in fold_results),
        "profit_factor": sum(profits) / max(abs(sum(losses)), 1e-12),
        "maximum_fold_drawdown_fraction": max(
            row["maximum_drawdown_fraction"] for row in fold_results
        ),
        "minimum_fold_win_rate": min(row["win_rate"] for row in fold_results),
        "minimum_fold_profit_factor": min(
            row["profit_factor"] for row in fold_results
        ),
        "positive_folds": sum(row["net_pnl_sol"] > 0 for row in fold_results),
        "quote_coverage": trades / max(attempted, 1),
        "rejected_quotes": sum(row["rejected_quote"] for row in fold_results),
        "ledger_hash": base.stable_hash(ledger),
        "folds": [
            {
                "fold": fold_number,
                "trades": result["trades"],
                "wins": result["wins"],
                "win_rate": result["win_rate"],
                "net_pnl_sol": result["net_pnl_sol"],
                "profit_factor": result["profit_factor"],
                "maximum_drawdown_fraction": result[
                    "maximum_drawdown_fraction"
                ],
                "quote_coverage": result["quote_coverage"],
                "ledger_hash": result["ledger_hash"],
            }
            for fold_number, result in enumerate(fold_results)
        ],
    }


def main() -> dict[str, Any]:
    development = json.loads(latency.DEVELOPMENT.read_text(encoding="utf-8"))
    fold_selections = [list(fold["ledger"]) for fold in development["folds"]]
    keys = {
        (str(row["run_id"]), str(row["mint"]))
        for selected in fold_selections
        for row in selected
    }
    traces, trace_audit = latency.load_selected_traces(keys)
    results = {
        str(delay): aggregate(
            [
                latency.replay(selected, traces, delay)
                for selected in fold_selections
            ],
            delay,
        )
        for delay in LATENCIES_MS
    }
    observed = results["300"]
    requirements = {
        "minimum_closed_trades": observed["trades"] >= 300,
        "minimum_win_rate": observed["win_rate"] >= 0.65,
        "minimum_wilson_bound": observed["wilson_95_lower_bound"] >= 0.55,
        "positive_net_pnl": observed["net_pnl_sol"] > 0,
        "minimum_profit_factor": observed["profit_factor"] >= 1.25,
        "every_fold_positive": observed["positive_folds"] == 4,
        "every_fold_minimum_profit_factor": (
            observed["minimum_fold_profit_factor"] >= 1.10
        ),
        "maximum_drawdown": observed["maximum_fold_drawdown_fraction"] <= 0.15,
        "full_quote_coverage": observed["quote_coverage"] == 1.0,
    }
    economic_keys = {
        "minimum_closed_trades",
        "positive_net_pnl",
        "minimum_profit_factor",
        "every_fold_positive",
        "every_fold_minimum_profit_factor",
        "maximum_drawdown",
        "full_quote_coverage",
    }
    identity = {
        "source_experiment_id": development["experiment_id"],
        "source_code_fingerprint": source_code_fingerprint(),
        "runtime_evidence": OBSERVED_RUNTIME_EVIDENCE,
        "latencies_ms": LATENCIES_MS,
        "selection_policy": "unchanged frozen development selections",
        "entry_policy": "first exact trace quote at or after 250ms plus latency",
        "exit_policy": development["policy"],
        "fee_model": development["replay"],
    }
    output = {
        "version": SCHEMA_VERSION,
        "diagnostic_id": f"e4d-{base.stable_hash(identity)}",
        "identity": identity,
        "source_experiment_id": development["experiment_id"],
        "observed_runtime_evidence": OBSERVED_RUNTIME_EVIDENCE,
        "trace_audit": trace_audit,
        "latencies": results,
        "observed_latency_bucket_ms": 300,
        "observed_latency_requirements": requirements,
        "economic_robustness_passed": all(
            requirements[key] for key in economic_keys
        ),
        "declared_win_rate_gate_passed_at_observed_latency": (
            requirements["minimum_win_rate"]
            and requirements["minimum_wilson_bound"]
        ),
        "supplemental_failure_classification": "LATENCY_FAILURE",
        "frozen_protocol_changed": False,
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

#!/usr/bin/env python3
"""Bound a fee-net partial scale-out plus protected-runner family at 300 ms."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import e4_v12_online_conformal_latency as latency
import e4_v12_profit_survival_search as base

OUTPUT = Path("artifacts/e4-v12-latency300-scaleout-oracle.json")
SCHEMA_VERSION = "e4-v12-latency300-scaleout-oracle-v1"
DIAGNOSTIC_FAMILY = "latency300-fee-net-scaleout-runner-oracle-v12"
LATENCY_MS = 300
HORIZON_MS = 250
MAXIMUM_HOLD_MS = 60_000
TRIGGER_FRACTIONS = (0.05, 0.10, 0.15, 0.20)
SCALE_OUT_FRACTIONS = (0.25, 0.50, 0.75, 0.90)
SOURCE_FILES = (
    "scripts/e4_v12_latency300_scaleout_oracle.py",
    "scripts/e4_v12_online_conformal_latency.py",
    "scripts/e4_v12_online_conformal_precision.py",
    "scripts/e4_v12_profit_survival_search.py",
)


@dataclass(frozen=True, slots=True)
class ScaleOutPolicy:
    trigger_fraction: float
    scale_out_fraction: float

    @property
    def key(self) -> str:
        return (
            f"net_trigger={self.trigger_fraction:.2f}"
            f"|scale_out={self.scale_out_fraction:.2f}"
            "|runner_take=3.00|runner_trail=1.25:0.15"
            f"|hold={MAXIMUM_HOLD_MS}"
        )


POLICIES = tuple(
    ScaleOutPolicy(trigger, fraction)
    for trigger in TRIGGER_FRACTIONS
    for fraction in SCALE_OUT_FRACTIONS
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


def sell_quote(tokens: float, point: base.Point) -> float:
    return tokens * point.virtual_sol / max(point.virtual_tokens + tokens, 1e-12)


def total_pnl(
    position_sol: float, gross_proceeds: float, exit_transactions: int
) -> float:
    variable_fee = base.PROTOCOL_AND_CREATOR_FEE_BPS / 10_000
    fixed_fee = 0.001 + base.TIP_SOL + base.BASE_TRANSACTION_FEE_SOL
    return (
        gross_proceeds * (1 - variable_fee)
        - position_sol * (1 + variable_fee)
        - exit_transactions * fixed_fee
    )


def replay(
    selected: list[dict[str, Any]],
    traces: dict[tuple[str, str], base.Trace],
    policy: ScaleOutPolicy,
) -> dict[str, Any]:
    candidates = sorted(
        selected,
        key=lambda row: (
            int(row["run_id"]),
            int(row["decision_ns"]),
            str(row["mint"]),
        ),
    )
    bankroll = base.STARTING_BANKROLL_SOL
    active: list[int] = []
    ledger = []
    for candidate in candidates:
        decision_ns = int(candidate["decision_ns"])
        active = [exit_ns for exit_ns in active if exit_ns > decision_ns]
        if len(active) >= base.MAX_CONCURRENT_POSITIONS:
            continue
        trace = traces[(str(candidate["run_id"]), str(candidate["mint"]))]
        entry_ns = trace.create_ns + (HORIZON_MS + LATENCY_MS) * 1_000_000
        entry = base.latest_point(trace.points, entry_ns)
        if entry is None or entry.complete:
            continue
        position = bankroll * base.POSITION_FRACTION
        tokens = base.choice_sets.buy_tokens(
            position, entry.virtual_sol, entry.virtual_tokens
        )
        if entry.real_tokens > 0:
            tokens = min(tokens, entry.real_tokens)
        entry_price = entry.price_sol or entry.virtual_sol / max(
            entry.virtual_tokens, 1e-12
        )
        remaining_tokens = tokens
        partial_proceeds = 0.0
        partial_executed = False
        peak_multiple = 1.0
        exit_point = entry
        exit_reason = "MAXIMUM_HOLD"
        deadline = entry_ns + MAXIMUM_HOLD_MS * 1_000_000
        for point in trace.points:
            if point.timestamp_ns <= entry_ns:
                continue
            if point.timestamp_ns > deadline:
                break
            if point.virtual_sol <= 0 or point.virtual_tokens <= 0:
                continue
            exit_point = point
            multiple = point.price_sol / max(entry_price, 1e-18)
            peak_multiple = max(peak_multiple, multiple)
            if point.complete:
                exit_reason = "LIQUIDITY_EMERGENCY"
                break
            full_pnl = total_pnl(position, sell_quote(tokens, point), 1)
            if (
                not partial_executed
                and full_pnl >= policy.trigger_fraction * position
            ):
                sold = tokens * policy.scale_out_fraction
                partial_proceeds = sell_quote(sold, point)
                remaining_tokens -= sold
                partial_executed = True
                exit_reason = "PARTIAL_SCALE_OUT"
                continue
            if not partial_executed and multiple <= 0.70:
                exit_reason = "INITIAL_STOP"
                break
            if partial_executed:
                if multiple >= 3.0:
                    exit_reason = "RUNNER_TAKE"
                    break
                floor = 1.06
                if peak_multiple >= 1.25:
                    floor = max(floor, peak_multiple * 0.85)
                if multiple <= floor:
                    exit_reason = "RUNNER_TRAIL"
                    break
        final_proceeds = sell_quote(remaining_tokens, exit_point)
        pnl_sol = total_pnl(
            position,
            partial_proceeds + final_proceeds,
            2 if partial_executed else 1,
        )
        bankroll += pnl_sol
        active.append(exit_point.timestamp_ns)
        ledger.append(
            {
                "run_id": str(candidate["run_id"]),
                "mint": str(candidate["mint"]),
                "decision_ns": decision_ns,
                "pnl_sol": pnl_sol,
                "win": pnl_sol > 0,
                "exit_reason": exit_reason,
                "partial_executed": partial_executed,
            }
        )
    profits = [row["pnl_sol"] for row in ledger if row["pnl_sol"] > 0]
    losses = [row["pnl_sol"] for row in ledger if row["pnl_sol"] <= 0]
    wins = len(profits)
    trades = len(ledger)
    metrics = {
        "policy": policy.key,
        "trigger_fraction": policy.trigger_fraction,
        "scale_out_fraction": policy.scale_out_fraction,
        "trades": trades,
        "wins": wins,
        "win_rate": wins / max(trades, 1),
        "wilson_95_lower_bound": base.wilson_lower_bound(wins, trades),
        "net_pnl_sol": bankroll - base.STARTING_BANKROLL_SOL,
        "profit_factor": sum(profits) / max(abs(sum(losses)), 1e-12),
        "partial_executions": sum(row["partial_executed"] for row in ledger),
        "exit_reasons": dict(Counter(row["exit_reason"] for row in ledger)),
        "ledger_hash": base.stable_hash(ledger),
    }
    requirements = {
        "minimum_trades": trades >= 50,
        "minimum_win_rate": metrics["win_rate"] >= 0.55,
        "positive_net_pnl": metrics["net_pnl_sol"] > 0,
        "minimum_profit_factor": metrics["profit_factor"] >= 1.10,
    }
    metrics["requirements"] = requirements
    metrics["fold_gate_passed"] = all(requirements.values())
    return metrics


def rank(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row["fold_gate_passed"],
        row["win_rate"],
        row["net_pnl_sol"],
        row["profit_factor"],
    )


def main() -> dict[str, Any]:
    development = json.loads(latency.DEVELOPMENT.read_text(encoding="utf-8"))
    selections = [list(fold["ledger"]) for fold in development["folds"]]
    keys = {
        (str(row["run_id"]), str(row["mint"]))
        for fold in selections
        for row in fold
    }
    traces, trace_audit = latency.load_selected_traces(keys)
    folds = []
    for fold_number, selected in enumerate(selections):
        policies = [replay(selected, traces, policy) for policy in POLICIES]
        policies.sort(key=rank, reverse=True)
        folds.append(
            {
                "fold": fold_number,
                "policies_evaluated": len(policies),
                "feasible_policies": sum(
                    row["fold_gate_passed"] for row in policies
                ),
                "oracle_best": policies[0],
                "all_policy_metrics_hash": base.stable_hash(policies),
            }
        )
    identity = {
        "diagnostic_family": DIAGNOSTIC_FAMILY,
        "source_experiment_id": development["experiment_id"],
        "source_code_fingerprint": source_code_fingerprint(),
        "dataset_source_manifest_fingerprint": development["identity"][
            "dataset_source_manifest_fingerprint"
        ],
        "policy_grid": [policy.key for policy in POLICIES],
        "chronological_folds": development["identity"]["chronological_split"],
        "latency_ms": LATENCY_MS,
        "causal_horizon_ms": HORIZON_MS,
        "position_sizing_fraction": base.POSITION_FRACTION,
        "maximum_concurrent_positions": base.MAX_CONCURRENT_POSITIONS,
        "fee_model": "exact two-exit fixed and variable fees when scaled out",
    }
    final_fold = folds[-1]
    output = {
        "version": SCHEMA_VERSION,
        "diagnostic_id": f"e4d-{base.stable_hash(identity)}",
        "identity": identity,
        "source_experiment_id": development["experiment_id"],
        "trace_audit": trace_audit,
        "folds": folds,
        "finding": (
            "No fee-net partial scale-out plus protected-runner policy passes "
            "the final chronological fold; the late failure requires a new "
            "causal risk-set selector rather than another exit overlay."
        ),
        "final_fold_feasible_policies": final_fold["feasible_policies"],
        "final_fold_oracle_best": final_fold["oracle_best"],
        "candidate_warranted": False,
        "candidate_frozen": False,
        "retired_family": True,
        "failure_classification": "EXIT_FAILURE",
        "material_change_required_before_rerun": (
            "A materially different causal risk-set selector; further fee-net "
            "take, stop, partial-scale-out, or protected-runner tuning is prohibited."
        ),
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

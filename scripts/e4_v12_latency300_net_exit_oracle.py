#!/usr/bin/env python3
"""Bound the exact-300 ms fee-net take/stop exit family by fold.

The frozen 317 selections and exact source traces are unchanged.  Each policy
exits on net-of-all-fees return thresholds.  Evaluating the full bounded family
inside every chronological fold provides an oracle upper bound: if no member
passes a fold, tuning or online selection within this family cannot repair it.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import e4_v12_online_conformal_latency as latency
import e4_v12_profit_survival_search as base

OUTPUT = Path("artifacts/e4-v12-latency300-net-exit-oracle.json")
SCHEMA_VERSION = "e4-v12-latency300-net-exit-oracle-v1"
DIAGNOSTIC_FAMILY = "latency300-fee-net-take-stop-oracle-v12"
LATENCY_MS = 300
HORIZON_MS = 250
MAXIMUM_HOLD_MS = 60_000
TAKE_FRACTIONS = (0.05, 0.10, 0.15, 0.20)
STOP_FRACTIONS = (0.10, 0.20, 0.30, 0.40)
SOURCE_FILES = (
    "scripts/e4_v12_latency300_net_exit_oracle.py",
    "scripts/e4_v12_online_conformal_latency.py",
    "scripts/e4_v12_online_conformal_precision.py",
    "scripts/e4_v12_profit_survival_search.py",
)


@dataclass(frozen=True, slots=True)
class NetExitPolicy:
    take_fraction: float
    stop_fraction: float

    @property
    def key(self) -> str:
        return (
            f"net_take={self.take_fraction:.2f}"
            f"|net_stop={self.stop_fraction:.2f}"
            f"|hold={MAXIMUM_HOLD_MS}"
        )


POLICIES = tuple(
    NetExitPolicy(take, stop)
    for take in TAKE_FRACTIONS
    for stop in STOP_FRACTIONS
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


def exact_pnl(
    position_sol: float,
    tokens: float,
    point: base.Point,
) -> float:
    gross = tokens * point.virtual_sol / max(point.virtual_tokens + tokens, 1e-12)
    return base.net_pnl(
        base.Outcome(gross / position_sol, 0.0, "FEE_NET_AUDIT"),
        0.001,
        position_sol,
    )


def replay(
    selected: list[dict[str, Any]],
    traces: dict[tuple[str, str], base.Trace],
    policy: NetExitPolicy,
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
    rejected_concurrency = 0
    for candidate in candidates:
        decision_ns = int(candidate["decision_ns"])
        active = [exit_ns for exit_ns in active if exit_ns > decision_ns]
        if len(active) >= base.MAX_CONCURRENT_POSITIONS:
            rejected_concurrency += 1
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
        deadline = entry_ns + MAXIMUM_HOLD_MS * 1_000_000
        exit_point = entry
        exit_reason = "MAXIMUM_HOLD"
        pnl_sol: float | None = None
        for point in trace.points:
            if point.timestamp_ns <= entry_ns:
                continue
            if point.timestamp_ns > deadline:
                break
            if point.virtual_sol <= 0 or point.virtual_tokens <= 0:
                continue
            exit_point = point
            candidate_pnl = exact_pnl(position, tokens, point)
            if point.complete:
                exit_reason = "LIQUIDITY_EMERGENCY"
                pnl_sol = candidate_pnl
                break
            if candidate_pnl >= policy.take_fraction * position:
                exit_reason = "NET_TAKE"
                pnl_sol = candidate_pnl
                break
            if candidate_pnl <= -policy.stop_fraction * position:
                exit_reason = "NET_STOP"
                pnl_sol = candidate_pnl
                break
        if pnl_sol is None:
            pnl_sol = exact_pnl(position, tokens, exit_point)
        bankroll += pnl_sol
        active.append(exit_point.timestamp_ns)
        ledger.append(
            {
                "run_id": str(candidate["run_id"]),
                "mint": str(candidate["mint"]),
                "decision_ns": decision_ns,
                "position_sol": position,
                "pnl_sol": pnl_sol,
                "win": pnl_sol > 0,
                "exit_reason": exit_reason,
            }
        )

    profits = [row["pnl_sol"] for row in ledger if row["pnl_sol"] > 0]
    losses = [row["pnl_sol"] for row in ledger if row["pnl_sol"] <= 0]
    equity = base.STARTING_BANKROLL_SOL
    peak = equity
    maximum_drawdown = 0.0
    for row in ledger:
        equity += row["pnl_sol"]
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, peak - equity)
    wins = len(profits)
    trades = len(ledger)
    metrics = {
        "policy": policy.key,
        "take_fraction": policy.take_fraction,
        "stop_fraction": policy.stop_fraction,
        "trades": trades,
        "wins": wins,
        "win_rate": wins / max(trades, 1),
        "wilson_95_lower_bound": base.wilson_lower_bound(wins, trades),
        "net_pnl_sol": bankroll - base.STARTING_BANKROLL_SOL,
        "profit_factor": sum(profits) / max(abs(sum(losses)), 1e-12),
        "maximum_drawdown_fraction": maximum_drawdown
        / base.STARTING_BANKROLL_SOL,
        "largest_winner_contribution": max(profits, default=0.0)
        / max(sum(profits), 1e-12),
        "rejected_concurrency": rejected_concurrency,
        "exit_reasons": dict(Counter(row["exit_reason"] for row in ledger)),
        "ledger_hash": base.stable_hash(ledger),
    }
    requirements = {
        "minimum_trades": trades >= 50,
        "minimum_win_rate": metrics["win_rate"] >= 0.55,
        "positive_net_pnl": metrics["net_pnl_sol"] > 0,
        "minimum_profit_factor": metrics["profit_factor"] >= 1.10,
        "maximum_drawdown": metrics["maximum_drawdown_fraction"] <= 0.15,
        "largest_winner_limit": metrics["largest_winner_contribution"] <= 0.25,
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
                "policies": policies,
            }
        )
    final_fold = folds[-1]
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
        "fee_model": {
            "protocol_and_creator_fee_bps": base.PROTOCOL_AND_CREATOR_FEE_BPS,
            "priority_fee_sol": 0.001,
            "tip_sol": base.TIP_SOL,
            "base_transaction_fee_sol": base.BASE_TRANSACTION_FEE_SOL,
        },
    }
    final_best = final_fold["oracle_best"]
    output = {
        "version": SCHEMA_VERSION,
        "diagnostic_id": f"e4d-{base.stable_hash(identity)}",
        "identity": identity,
        "source_experiment_id": development["experiment_id"],
        "trace_audit": trace_audit,
        "folds": folds,
        "finding": (
            "No member of the bounded fee-net take/stop family passes the final "
            "chronological fold, so parameter tuning or causal selection within "
            "this family cannot repair the 300 ms thesis."
        ),
        "final_fold_feasible_policies": final_fold["feasible_policies"],
        "final_fold_oracle_best": final_best,
        "candidate_warranted": False,
        "candidate_frozen": False,
        "retired_family": True,
        "failure_classification": "EXIT_FAILURE",
        "material_change_required_before_rerun": (
            "A new exit mechanic beyond single full-position fee-net take/stop, "
            "or a materially different causal risk-set selector."
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

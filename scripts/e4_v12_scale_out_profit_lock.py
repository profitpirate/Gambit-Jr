#!/usr/bin/env python3
"""Develop a fee-aware partial-profit and protected-runner exit policy.

The selector, causal horizon, score threshold, and selected risk sets are frozen
from adaptive-exit V3.  Exit policies are selected only on V3's five-window
development ledger, then replayed on V3's later five-window historical holdout.
That second replay is retrospective confirmation for this new exit thesis, not
an untouched golden holdout; strictly later capture evidence is still required.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import e4_v12_adaptive_exit_search as adaptive
import e4_v12_profit_survival_search as base

SCHEMA_VERSION = "e4-v12-scale-out-profit-lock-v4"
THESIS_FAMILY = "fee-aware-scale-out-protected-runner-v4"
HORIZON_MS = 250
INITIAL_STOP = 0.70
HOLD_MS = 60_000
PRIORITY_FEE_SOL = 0.001


@dataclass(frozen=True, slots=True)
class ScaleOutPolicy:
    partial_trigger: float
    partial_fraction: float
    runner_take: float
    runner_trail_activate: float
    runner_trail_retrace: float
    runner_floor: float = 1.06

    @property
    def key(self) -> str:
        return (
            f"partial={self.partial_trigger:.2f}:{self.partial_fraction:.2f}"
            f"|runner_take={self.runner_take:.2f}"
            f"|runner_trail={self.runner_trail_activate:.2f}:{self.runner_trail_retrace:.2f}"
            f"|runner_floor={self.runner_floor:.2f}"
        )


POLICIES = tuple(
    ScaleOutPolicy(trigger, fraction, take, trail_activate, retrace)
    for trigger in (1.15, 1.20, 1.30)
    for fraction in (0.35, 0.45, 0.50, 0.65, 1.00)
    for take in (2.0, 3.0)
    for trail_activate in (1.25, 1.50)
    for retrace in (0.10, 0.15, 0.25)
)


def sell_quote(tokens: float, virtual_sol: float, virtual_tokens: float) -> float:
    return tokens * virtual_sol / max(virtual_tokens + tokens, 1e-12)


def scale_out_outcome(
    trace: base.Trace,
    policy: ScaleOutPolicy,
    *,
    latency_ms: int = 0,
) -> base.Outcome | None:
    decision_ns = trace.create_ns + HORIZON_MS * 1_000_000
    entry_ns = decision_ns + latency_ms * 1_000_000
    entry = base.latest_point(trace.points, entry_ns)
    if entry is None or entry.complete:
        return None
    entry_price = entry.price_sol or entry.virtual_sol / max(entry.virtual_tokens, 1e-12)
    position = base.STARTING_BANKROLL_SOL * base.POSITION_FRACTION
    tokens = base.choice_sets.buy_tokens(position, entry.virtual_sol, entry.virtual_tokens)
    if entry.real_tokens > 0:
        tokens = min(tokens, entry.real_tokens)
    remaining_tokens = tokens
    partial_proceeds = 0.0
    partial_executed = False
    peak_multiple = 1.0
    exit_point = entry
    exit_reason = "MAXIMUM_HOLD"
    deadline = entry_ns + HOLD_MS * 1_000_000
    for point in trace.points:
        if point.timestamp_ns <= entry_ns:
            continue
        if point.timestamp_ns > deadline:
            break
        if point.price_sol <= 0 or point.virtual_sol <= 0 or point.virtual_tokens <= 0:
            continue
        exit_point = point
        multiple = point.price_sol / max(entry_price, 1e-18)
        peak_multiple = max(peak_multiple, multiple)
        if point.complete:
            exit_reason = "LIQUIDITY_EMERGENCY"
            break
        if not partial_executed and multiple >= policy.partial_trigger:
            sold_tokens = tokens * policy.partial_fraction
            partial_proceeds = sell_quote(
                sold_tokens, point.virtual_sol, point.virtual_tokens
            )
            remaining_tokens -= sold_tokens
            partial_executed = True
            if remaining_tokens <= 1e-12:
                exit_reason = "FULL_PROFIT_LOCK"
                break
            continue
        if not partial_executed and multiple <= INITIAL_STOP:
            exit_reason = "INITIAL_STOP"
            break
        if partial_executed:
            if multiple >= policy.runner_take:
                exit_reason = "RUNNER_TAKE_PROFIT"
                break
            floor = policy.runner_floor
            if peak_multiple >= policy.runner_trail_activate:
                floor = max(
                    floor, peak_multiple * (1 - policy.runner_trail_retrace)
                )
            if multiple <= floor:
                exit_reason = "RUNNER_TRAILING_STOP"
                break
    final_proceeds = sell_quote(
        remaining_tokens, exit_point.virtual_sol, exit_point.virtual_tokens
    )
    total_proceeds = partial_proceeds + final_proceeds
    if partial_executed and remaining_tokens > 1e-12:
        # Two exit transactions instead of the single exit already in base.net_pnl.
        additional_fixed_fee = PRIORITY_FEE_SOL + base.TIP_SOL + base.BASE_TRANSACTION_FEE_SOL
        proceeds_fee_multiplier = 1 - base.PROTOCOL_AND_CREATOR_FEE_BPS / 10_000
        total_proceeds -= additional_fixed_fee / proceeds_fee_multiplier
    return base.Outcome(
        gross_multiple=total_proceeds / position,
        exit_offset_ms=max(0.0, (exit_point.timestamp_ns - decision_ns) / 1_000_000),
        exit_reason=exit_reason,
    )


def selected_ledgers(root: Path) -> dict[str, list[Mapping[str, Any]]]:
    development = json.loads(
        (root / "artifacts/e4-v12-adaptive-exit-development-search.json").read_text(
            encoding="utf-8"
        )
    )["winner"]["validation"]["ledger"]
    historical = json.loads(
        (root / "artifacts/e4-v12-adaptive-exit-historical-economics.json").read_text(
            encoding="utf-8"
        )
    )["latencies"]["0"]["ledger"]
    return {"development": development, "secondary_confirmation": historical}


def load_selected_traces(
    root: Path, ledgers: Mapping[str, Sequence[Mapping[str, Any]]]
) -> tuple[dict[tuple[str, str], base.Trace], list[dict[str, Any]]]:
    selected = {
        (str(row["run_id"]), str(row["mint"]))
        for ledger in ledgers.values()
        for row in ledger
    }
    specs = {
        spec.run_id: spec
        for spec in adaptive.capture_specs(root, include_holdout=True)
        if any(run_id == spec.run_id for run_id, _ in selected)
    }
    traces = {}
    audit = []
    for run_id in sorted(specs, key=int):
        spec = specs[run_id]
        actual_sha = base.sha256_path(spec.events_path)
        if actual_sha != spec.expected_sha256:
            raise ValueError(f"capture hash changed: {run_id}")
        capture_traces, parse_errors = base.load_capture(spec, Counter())
        chosen = [trace for trace in capture_traces if (run_id, trace.mint) in selected]
        traces.update({(run_id, trace.mint): trace for trace in chosen})
        audit.append(
            {
                "run_id": run_id,
                "sha256": actual_sha,
                "hash_match": True,
                "parse_errors": parse_errors,
                "selected_traces": len(chosen),
            }
        )
    missing = sorted(selected - set(traces))
    if missing:
        raise ValueError(f"selected traces missing: {missing[:3]}")
    return traces, audit


def research_rows(
    ledger: Sequence[Mapping[str, Any]],
    traces: Mapping[tuple[str, str], base.Trace],
    split: str,
) -> list[base.ResearchRow]:
    rows = []
    for selected in ledger:
        run_id = str(selected["run_id"])
        mint = str(selected["mint"])
        trace = traces[(run_id, mint)]
        outcomes = tuple(scale_out_outcome(trace, policy) for policy in POLICIES)
        if any(outcome is None for outcome in outcomes):
            raise ValueError(f"invalid scale-out outcome: {run_id}/{mint}")
        rows.append(
            base.ResearchRow(
                run_id=run_id,
                split=split,
                mint=mint,
                decision_ns=base.integer(selected["decision_ns"]),
                horizon_ms=HORIZON_MS,
                features=(),
                outcomes=outcomes,  # type: ignore[arg-type]
            )
        )
    return rows


def rank(metrics: Mapping[str, Any]) -> tuple[Any, ...]:
    windows = list(metrics["by_capture_window"].values())
    requirements = (
        metrics["trades"] >= 50,
        metrics["capture_windows"] == 5,
        metrics["win_rate"] >= 0.65,
        metrics["net_pnl_sol"] > 0,
        metrics["profit_factor"] >= 1.25,
        metrics["maximum_drawdown_fraction"] <= 0.15,
        metrics["largest_winner_contribution"] <= 0.25,
        sum(window["pnl_sol"] > 0 for window in windows) >= 4,
    )
    return (
        sum(requirements),
        *requirements,
        metrics["win_rate"],
        metrics["net_pnl_sol"],
        metrics["profit_factor"],
    )


def evaluate(rows: Sequence[base.ResearchRow], policy_index: int) -> dict[str, Any]:
    return base.simulate_predictions(
        rows,
        scores=base.np.ones(len(rows)),
        threshold=0.5,
        policy_index=policy_index,
        priority_fee_sol=PRIORITY_FEE_SOL,
    )


def run(root: Path) -> dict[str, Any]:
    ledgers = selected_ledgers(root)
    traces, source_audit = load_selected_traces(root, ledgers)
    development_rows = research_rows(ledgers["development"], traces, "development")
    confirmation_rows = research_rows(
        ledgers["secondary_confirmation"], traces, "secondary_confirmation"
    )
    candidates = []
    for index, policy in enumerate(POLICIES):
        candidates.append(
            {
                "policy_index": index,
                "policy": policy.key,
                "parameters": asdict(policy),
                "development": evaluate(development_rows, index),
            }
        )
    candidates.sort(key=lambda row: rank(row["development"]), reverse=True)
    winner = candidates[0]
    confirmation = evaluate(confirmation_rows, base.integer(winner["policy_index"]))
    identity = {
        "thesis_family_identifier": THESIS_FAMILY,
        "source_code_fingerprint": base.sha256_path(Path(__file__).resolve()),
        "parent_experiment_id": json.loads(
            (root / "artifacts/e4-v12-adaptive-exit-frozen-candidate.json").read_text(
                encoding="utf-8"
            )
        )["experiment_id"],
        "selector_policy": "adaptive-exit V3 selector, horizon, threshold and risk set frozen",
        "source_manifest_fingerprint": base.sha256_path(
            root / "artifacts/e4-v12-adaptive-exit-source-manifest.json"
        ),
        "full_parameters": winner["parameters"],
        "causal_horizon_ms": HORIZON_MS,
        "chronological_split": {
            "development": sorted({row.run_id for row in development_rows}, key=int),
            "secondary_confirmation": sorted(
                {row.run_id for row in confirmation_rows}, key=int
            ),
            "untouched_holdout": "strictly later evidence still collecting",
        },
        "bankroll": base.STARTING_BANKROLL_SOL,
        "position_sizing_fraction": base.POSITION_FRACTION,
        "fee_model": {
            "priority_fee_sol_per_transaction": PRIORITY_FEE_SOL,
            "tip_sol_per_transaction": base.TIP_SOL,
            "base_fee_sol_per_transaction": base.BASE_TRANSACTION_FEE_SOL,
            "protocol_and_creator_fee_bps": base.PROTOCOL_AND_CREATOR_FEE_BPS,
            "additional_scale_out_transaction_charged": True,
        },
        "latency_assumptions_ms": list(base.LATENCIES_MS),
        "execution_policy": "paper replay; exact reserve quote; maximum two exits",
        "exit_policy": winner["parameters"],
    }
    development_passed = rank(winner["development"])[0] == 8
    confirmation_passed = rank(confirmation)[0] == 8
    return {
        "version": SCHEMA_VERSION,
        "experiment_id": f"e4x-{base.stable_hash(identity)}",
        "identity": identity,
        "source_audit": source_audit,
        "policies_evaluated": len(POLICIES),
        "selected_policy": winner,
        "secondary_confirmation": confirmation,
        "top_candidates": candidates[:20],
        "development_gate_passed": development_passed,
        "secondary_confirmation_gate_passed": confirmation_passed,
        "ready_for_strictly_later_evidence": development_passed and confirmation_passed,
        "untouched_holdout_passed": False,
        "live_confirmation_authorised": False,
        "production_promotion_authorised": False,
        "production_paths_changed": 0,
    }


def main() -> None:
    root = Path.cwd().resolve()
    report = run(root)
    base.write_json(root / "artifacts/e4-v12-scale-out-profit-lock-development.json", report)
    summary = {
        "experiment_id": report["experiment_id"],
        "policy": report["selected_policy"]["policy"],
        "development": {
            key: report["selected_policy"]["development"][key]
            for key in ("trades", "wins", "win_rate", "net_pnl_sol", "profit_factor")
        },
        "secondary_confirmation": {
            key: report["secondary_confirmation"][key]
            for key in ("trades", "wins", "win_rate", "net_pnl_sol", "profit_factor")
        },
        "ready": report["ready_for_strictly_later_evidence"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

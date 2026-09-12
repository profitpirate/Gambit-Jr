#!/usr/bin/env python3
"""Test fee-aware profit locking inside the frozen high-expectancy actor cohort."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

import e4_v12_actor_reputation_rules as reputation
import e4_v12_adaptive_exit_search as adaptive
import e4_v12_causal_actor_memory as actors
import e4_v12_direct_hazard_consensus as hazard
import e4_v12_profit_survival_search as base
import e4_v12_scale_out_profit_lock as scale_out
import numpy as np

SCHEMA_VERSION = "e4-v12-actor-profit-lock-v8"
THESIS_FAMILY = "high-expectancy-actor-cohort-profit-lock-v8"
POLICIES = (
    *scale_out.POLICIES,
    *(
        scale_out.ScaleOutPolicy(1.20, fraction, take, trail_activate, retrace)
        for fraction in (0.52, 0.55, 0.58, 0.60)
        for take in (2.0, 3.0)
        for trail_activate in (1.25, 1.50)
        for retrace in (0.10, 0.15, 0.25)
    ),
)


def selected_actor_rows(root: Path) -> tuple[list[base.ResearchRow], reputation.BuyerRule]:
    rows, _ = actors.load_rows(root, root / ".tmp-actor-memory-development.pkl")
    windows = hazard.ordered_windows(rows)
    validation_ids = set(windows[28:])
    report = json.loads(
        (root / "artifacts/e4-v12-actor-reputation-rules-development.json").read_text(
            encoding="utf-8"
        )
    )
    rule = reputation.BuyerRule(**report["winner"]["parameters"])
    return [
        row
        for row in rows
        if row.run_id in validation_ids and reputation.buyer_matches(row, rule)
    ], rule


def load_traces(
    root: Path, selected: Sequence[base.ResearchRow]
) -> tuple[dict[tuple[str, str], base.Trace], list[dict[str, Any]]]:
    keys = {(row.run_id, row.mint) for row in selected}
    specs = {
        spec.run_id: spec
        for spec in adaptive.capture_specs(root, include_holdout=True)
        if any(run_id == spec.run_id for run_id, _ in keys)
    }
    traces = {}
    audits = []
    for run_id in sorted(specs, key=int):
        spec = specs[run_id]
        actual_sha = base.sha256_path(spec.events_path)
        if actual_sha != spec.expected_sha256:
            raise ValueError(f"capture hash changed: {run_id}")
        capture, parse_errors = base.load_capture(spec, Counter())
        chosen = [trace for trace in capture if (run_id, trace.mint) in keys]
        traces.update({(run_id, trace.mint): trace for trace in chosen})
        audits.append(
            {
                "run_id": run_id,
                "sha256": actual_sha,
                "hash_match": True,
                "parse_errors": parse_errors,
                "selected_traces": len(chosen),
            }
        )
    missing = keys - set(traces)
    if missing:
        raise ValueError(f"missing selected actor traces: {sorted(missing)[:3]}")
    return traces, audits


def rows_with_scale_outs(
    selected: Sequence[base.ResearchRow],
    traces: Mapping[tuple[str, str], base.Trace],
) -> list[base.ResearchRow]:
    output = []
    for row in selected:
        trace = traces[(row.run_id, row.mint)]
        outcomes = tuple(
            scale_out.scale_out_outcome(trace, policy) for policy in POLICIES
        )
        if any(outcome is None for outcome in outcomes):
            raise ValueError(f"invalid scale-out outcome: {row.run_id}/{row.mint}")
        output.append(
            base.ResearchRow(
                run_id=row.run_id,
                split="development",
                mint=row.mint,
                decision_ns=row.decision_ns,
                horizon_ms=row.horizon_ms,
                features=row.features,
                outcomes=outcomes,  # type: ignore[arg-type]
            )
        )
    return output


def evaluate_policy(
    groups: Sequence[Sequence[base.ResearchRow]], policy_index: int
) -> dict[str, Any]:
    key = f"policy={policy_index}"
    folds = []
    for rows in groups:
        folds.append(
            base.simulate_predictions(
                rows,
                np.ones(len(rows)),
                0.5,
                policy_index=policy_index,
                priority_fee_sol=scale_out.PRIORITY_FEE_SOL,
            )
        )
    return hazard.aggregate_candidate(
        [{"candidates": {key: metrics}} for metrics in folds], key
    )


def compact(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in candidate.items() if key != "folds"
    } | {
        "folds": [
            {
                key: fold[key]
                for key in (
                    "trades",
                    "wins",
                    "win_rate",
                    "wilson_95_lower_bound",
                    "net_pnl_sol",
                    "profit_factor",
                    "maximum_drawdown_fraction",
                    "capture_windows",
                    "largest_winner_contribution",
                    "by_capture_window",
                    "exit_reasons",
                    "ledger_hash",
                )
            }
            for fold in candidate["folds"]
        ]
    }


def run(root: Path) -> dict[str, Any]:
    selected, rule = selected_actor_rows(root)
    traces, audit = load_traces(root, selected)
    rows = rows_with_scale_outs(selected, traces)
    all_rows, _ = actors.load_rows(root, root / ".tmp-actor-memory-development.pkl")
    windows = hazard.ordered_windows(all_rows)[-20:]
    groups = [
        [row for row in rows if row.run_id in set(windows[start : start + 5])]
        for start in (0, 5, 10, 15)
    ]
    candidates = []
    for index, policy in enumerate(POLICIES):
        candidate = evaluate_policy(groups, index)
        candidate["policy_index"] = index
        candidate["policy"] = policy.key
        candidate["parameters"] = asdict(policy)
        candidates.append(candidate)
    candidates.sort(key=hazard.candidate_rank, reverse=True)
    winner = candidates[0]
    requirements = hazard.candidate_rank(winner)[0]
    identity = {
        "thesis_family_identifier": THESIS_FAMILY,
        "source_code_fingerprint": base.sha256_path(Path(__file__).resolve()),
        "dataset_source_manifest_fingerprint": base.sha256_path(
            root / "artifacts/e4-v12-adaptive-exit-source-manifest.json"
        ),
        "evidence_epoch": windows,
        "feature_set_fingerprint": base.stable_hash(actors.FEATURE_NAMES),
        "model_family": "frozen point-in-time buyer reputation rule",
        "full_parameters": {
            "buyer_rule": asdict(rule),
            "scale_out": winner["parameters"],
        },
        "causal_horizon_ms": actors.HORIZON_MS,
        "candidate_risk_set_policy": rule.key,
        "chronological_split": {
            "walk_forward_development": [
                windows[start : start + 5] for start in (0, 5, 10, 15)
            ],
            "future_holdout": "strictly later evidence still collecting",
        },
        "bankroll": base.STARTING_BANKROLL_SOL,
        "position_sizing_fraction": base.POSITION_FRACTION,
        "fee_model": {
            "priority_fee_sol_per_transaction": scale_out.PRIORITY_FEE_SOL,
            "tip_sol_per_transaction": base.TIP_SOL,
            "base_fee_sol_per_transaction": base.BASE_TRANSACTION_FEE_SOL,
            "protocol_and_creator_fee_bps": base.PROTOCOL_AND_CREATOR_FEE_BPS,
            "additional_scale_out_transaction_charged": True,
        },
        "output_guard": "actor results delayed 60 seconds; reserve-valid quotes",
        "latency_assumptions_ms": list(base.LATENCIES_MS),
        "execution_policy": "paper replay; maximum two exits",
        "exit_policy": winner["parameters"],
    }
    ready = requirements == 11
    return {
        "version": SCHEMA_VERSION,
        "experiment_id": f"e4x-{base.stable_hash(identity)}",
        "identity": identity,
        "source_audit": audit,
        "actor_candidates": len(selected),
        "policies_evaluated": len(candidates),
        "winner": compact(winner),
        "top_candidates": [compact(candidate) for candidate in candidates[:20]],
        "walk_forward_requirements_passed": requirements,
        "walk_forward_gate_passed": ready,
        "ready_for_strictly_later_evidence": ready,
        "untouched_holdout_passed": False,
        "live_confirmation_authorised": False,
        "production_promotion_authorised": False,
        "production_paths_changed": 0,
    }


def main() -> None:
    root = Path.cwd().resolve()
    report = run(root)
    base.write_json(root / "artifacts/e4-v12-actor-profit-lock-development.json", report)
    winner = report["winner"]
    print(
        json.dumps(
            {
                "experiment_id": report["experiment_id"],
                "policy": winner["policy"],
                "trades": winner["trades"],
                "win_rate": winner["win_rate"],
                "pnl_sol": winner["net_pnl_sol"],
                "profit_factor": winner["profit_factor"],
                "positive_folds": winner["positive_folds"],
                "positive_windows": winner["positive_capture_windows"],
                "requirements_passed": report["walk_forward_requirements_passed"],
                "ready": report["ready_for_strictly_later_evidence"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

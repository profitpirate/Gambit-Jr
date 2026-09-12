#!/usr/bin/env python3
"""Choose a safe profit lock or protected runner causally per launch."""

from __future__ import annotations

import json
import pickle
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import e4_v12_actor_reputation_rules as reputation
import e4_v12_adaptive_exit_search as adaptive
import e4_v12_causal_actor_memory as actors
import e4_v12_direct_hazard_consensus as hazard
import e4_v12_profit_survival_search as base
import e4_v12_scale_out_profit_lock as scale_out
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

SCHEMA_VERSION = "e4-v12-causal-dynamic-exit-v9"
THESIS_FAMILY = "causal-safe-lock-versus-protected-runner-v9"
RANDOM_SEED = 12_095
RECENT_WINDOWS = 8
SAFE_POLICY = scale_out.ScaleOutPolicy(1.20, 1.00, 2.0, 1.25, 0.10)
RUNNER_POLICIES = (
    scale_out.ScaleOutPolicy(1.30, 0.35, 3.0, 1.50, 0.25),
    scale_out.ScaleOutPolicy(1.20, 0.50, 3.0, 1.25, 0.25),
    scale_out.ScaleOutPolicy(1.20, 0.55, 3.0, 1.25, 0.25),
)
THRESHOLDS = (0.30, 0.40, 0.50, 0.60, 0.70)


def actor_rule(root: Path) -> reputation.BuyerRule:
    report = json.loads(
        (root / "artifacts/e4-v12-actor-reputation-rules-development.json").read_text(
            encoding="utf-8"
        )
    )
    return reputation.BuyerRule(**report["winner"]["parameters"])


def extract_actor_cohort(
    root: Path, rule: reputation.BuyerRule
) -> tuple[list[base.ResearchRow], list[dict[str, Any]]]:
    actor_rows, _ = actors.load_rows(root, root / ".tmp-actor-memory-development.pkl")
    selected = [row for row in actor_rows if reputation.buyer_matches(row, rule)]
    keys = {(row.run_id, row.mint) for row in selected}
    specs = {
        spec.run_id: spec
        for spec in adaptive.capture_specs(root, include_holdout=True)
        if any(run_id == spec.run_id for run_id, _ in keys)
    }
    traces = {}
    audit = []
    for run_id in sorted(specs, key=int):
        spec = specs[run_id]
        actual_sha = base.sha256_path(spec.events_path)
        if actual_sha != spec.expected_sha256:
            raise ValueError(f"capture hash changed: {run_id}")
        capture, parse_errors = base.load_capture(spec, Counter())
        chosen = [trace for trace in capture if (run_id, trace.mint) in keys]
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
    missing = keys - set(traces)
    if missing:
        raise ValueError(f"missing actor traces: {sorted(missing)[:3]}")
    output = []
    policies = (SAFE_POLICY, *RUNNER_POLICIES)
    for row in selected:
        trace = traces[(row.run_id, row.mint)]
        outcomes = tuple(
            scale_out.scale_out_outcome(trace, policy) for policy in policies
        )
        if any(outcome is None for outcome in outcomes):
            raise ValueError(f"invalid dynamic exit outcome: {row.run_id}/{row.mint}")
        output.append(replace(row, outcomes=outcomes))  # type: ignore[arg-type]
    return output, audit


def load_cohort(
    root: Path, cache: Path, rule: reputation.BuyerRule
) -> tuple[list[base.ResearchRow], list[dict[str, Any]]]:
    if cache.exists():
        with cache.open("rb") as handle:
            payload = pickle.load(handle)
        return payload["rows"], payload["audit"]
    rows, audit = extract_actor_cohort(root, rule)
    with cache.open("wb") as handle:
        pickle.dump({"rows": rows, "audit": audit}, handle)
    return rows, audit


def pnl_values(rows: Sequence[base.ResearchRow], policy_index: int) -> np.ndarray:
    position = base.STARTING_BANKROLL_SOL * base.POSITION_FRACTION
    return np.asarray(
        [base.net_pnl(row.outcomes[policy_index], 0.001, position) for row in rows],
        dtype=float,
    )


def classifier() -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(
        learning_rate=0.035,
        max_iter=180,
        max_leaf_nodes=15,
        min_samples_leaf=30,
        l2_regularization=12.0,
        random_state=RANDOM_SEED,
    )


def fit_models(
    train: Sequence[base.ResearchRow], runner_index: int
) -> tuple[HistGradientBoostingClassifier, HistGradientBoostingClassifier]:
    windows = hazard.ordered_windows(train)
    recent_ids = set(windows[-RECENT_WINDOWS:])
    recent = [row for row in train if row.run_id in recent_ids]

    def advantage(rows: Sequence[base.ResearchRow]) -> np.ndarray:
        runner = pnl_values(rows, runner_index)
        safe = pnl_values(rows, 0)
        return (runner >= safe + 0.003).astype(int)

    all_model = classifier().fit(base.feature_matrix(train), advantage(train))
    recent_model = classifier().fit(base.feature_matrix(recent), advantage(recent))
    return all_model, recent_model


def runner_scores(
    models: tuple[HistGradientBoostingClassifier, HistGradientBoostingClassifier],
    rows: Sequence[base.ResearchRow],
) -> np.ndarray:
    matrix = base.feature_matrix(rows)
    return np.minimum(
        models[0].predict_proba(matrix)[:, 1],
        models[1].predict_proba(matrix)[:, 1],
    )


def mixed_rows(
    rows: Sequence[base.ResearchRow],
    scores: Sequence[float],
    threshold: float,
    runner_index: int,
) -> list[base.ResearchRow]:
    return [
        replace(
            row,
            outcomes=(
                row.outcomes[runner_index]
                if score >= threshold
                else row.outcomes[0],
            ),
        )
        for row, score in zip(rows, scores, strict=True)
    ]


def evaluate(rows: Sequence[base.ResearchRow]) -> dict[str, Any]:
    return base.simulate_predictions(
        rows,
        np.ones(len(rows)),
        0.5,
        policy_index=0,
        priority_fee_sol=0.001,
    )


def fold_result(
    rows: Sequence[base.ResearchRow],
    windows: Sequence[str],
    train_end: int,
) -> dict[str, Any]:
    train_ids = set(windows[:train_end])
    validation_ids = set(windows[train_end : train_end + 5])
    train = [row for row in rows if row.run_id in train_ids]
    validation = [row for row in rows if row.run_id in validation_ids]
    candidates = {"safe_only": evaluate(mixed_rows(validation, np.zeros(len(validation)), 1, 1))}
    for runner_index, policy in enumerate(RUNNER_POLICIES, 1):
        models = fit_models(train, runner_index)
        scores = runner_scores(models, validation)
        candidates[f"runner_only={runner_index}"] = evaluate(
            mixed_rows(validation, np.ones(len(validation)), 0, runner_index)
        )
        for threshold in THRESHOLDS:
            key = f"runner={runner_index}|threshold={threshold:.2f}"
            candidates[key] = evaluate(
                mixed_rows(validation, scores, threshold, runner_index)
            )
    return {
        "train_windows": list(windows[:train_end]),
        "validation_windows": list(windows[train_end : train_end + 5]),
        "train_actor_rows": len(train),
        "validation_actor_rows": len(validation),
        "candidates": candidates,
    }


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
    rule = actor_rule(root)
    rows, audit = load_cohort(root, root / ".tmp-dynamic-exit-cohort.pkl", rule)
    all_actor_rows, _ = actors.load_rows(root, root / ".tmp-actor-memory-development.pkl")
    all_windows = hazard.ordered_windows(all_actor_rows)
    folds = []
    for number, train_end in enumerate((28, 33, 38, 43), 1):
        print(f"fit dynamic-exit fold {number}/4", flush=True)
        folds.append(fold_result(rows, all_windows, train_end))
    keys = sorted(folds[0]["candidates"])
    candidates = [hazard.aggregate_candidate(folds, key) for key in keys]
    candidates.sort(key=hazard.candidate_rank, reverse=True)
    winner = candidates[0]
    requirements = hazard.candidate_rank(winner)[0]
    identity = {
        "thesis_family_identifier": THESIS_FAMILY,
        "source_code_fingerprint": base.sha256_path(Path(__file__).resolve()),
        "dataset_source_manifest_fingerprint": base.sha256_path(
            root / "artifacts/e4-v12-adaptive-exit-source-manifest.json"
        ),
        "evidence_epoch": all_windows,
        "feature_set_fingerprint": base.stable_hash(actors.FEATURE_NAMES),
        "model_family": "all/recent HGB runner-advantage consensus",
        "full_parameters": {
            "random_seed": RANDOM_SEED,
            "recent_windows": RECENT_WINDOWS,
            "advantage_margin_sol": 0.003,
            "selected_candidate": winner["candidate"],
            "buyer_rule": asdict(rule),
        },
        "causal_horizon_ms": actors.HORIZON_MS,
        "candidate_risk_set_policy": rule.key,
        "chronological_split": [
            {"train": fold["train_windows"], "validation": fold["validation_windows"]}
            for fold in folds
        ],
        "bankroll": base.STARTING_BANKROLL_SOL,
        "position_sizing_fraction": base.POSITION_FRACTION,
        "fee_model": {
            "priority_fee_sol_per_transaction": 0.001,
            "tip_sol_per_transaction": base.TIP_SOL,
            "base_fee_sol_per_transaction": base.BASE_TRANSACTION_FEE_SOL,
            "protocol_and_creator_fee_bps": base.PROTOCOL_AND_CREATOR_FEE_BPS,
        },
        "output_guard": "resolved actor memory; causal exit choice; reserve-valid quotes",
        "latency_assumptions_ms": list(base.LATENCIES_MS),
        "execution_policy": "paper replay; maximum two exits",
        "exit_policy": {
            "safe": asdict(SAFE_POLICY),
            "runners": [asdict(policy) for policy in RUNNER_POLICIES],
        },
    }
    ready = requirements == 11
    return {
        "version": SCHEMA_VERSION,
        "experiment_id": f"e4x-{base.stable_hash(identity)}",
        "identity": identity,
        "source_audit": audit,
        "actor_cohort_rows": len(rows),
        "folds": folds,
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
    base.write_json(root / "artifacts/e4-v12-causal-dynamic-exit-development.json", report)
    winner = report["winner"]
    print(
        json.dumps(
            {
                "experiment_id": report["experiment_id"],
                "candidate": winner["candidate"],
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

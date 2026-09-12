#!/usr/bin/env python3
"""Test a regime-robust, direct expected-value launch thesis.

This is a new evidence epoch after profit-survival-v1 failed its historical
holdout.  The failed holdout is now development evidence.  Eleven strictly
later 3,000-launch captures supply new training, validation, and a sealed final
holdout.  The final three captures cannot be opened without an explicit
certification flag and a previously frozen candidate.
"""

from __future__ import annotations

import argparse
import json
import pickle
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace
from itertools import pairwise
from pathlib import Path
from typing import Any

import e4_v12_profit_survival_search as base
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor

SCHEMA_VERSION = "e4-v12-regime-robust-survival-v2"
THESIS_FAMILY = "regime-robust-direct-expected-value-v2"
MANIFEST = Path("artifacts/e4-v12-regime-robust-source-manifest.json")
NEW_CAPTURE_ROOT = Path(".tmp-profit-survival-new-evidence/runs")
RECENT_TRAIN_WINDOWS = 8
SHORTLIST_PER_HORIZON = 6
RANDOM_SEED = 12_092
MODEL_FAMILIES = (
    "expected_value_all",
    "expected_value_regime_blend",
    "expected_value_conservative",
    "profit_probability_regime_blend",
)


def all_capture_specs(root: Path, *, include_holdout: bool) -> list[base.CaptureSpec]:
    """Return the immutable 99k-launch chronology with old splits retired."""
    old = [replace(spec, split="train") for spec in base.capture_specs(root, include_holdout=True)]
    manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    newer = []
    for row in manifest["new_captures"]:
        split = str(row["split"])
        if split == "holdout" and not include_holdout:
            continue
        run_id = str(row["run_id"])
        newer.append(
            base.CaptureSpec(
                run_id=run_id,
                split=split,
                start_ns=base.integer(row["capture_start_ns"]),
                end_ns=base.integer(row["capture_end_ns"]),
                events_path=root
                / NEW_CAPTURE_ROOT
                / run_id
                / "artifacts/e4-v12-forward-batch-live-events.jsonl",
                expected_sha256=str(row["events_sha256"]),
            )
        )
    specs = sorted([*old, *newer], key=lambda spec: (spec.start_ns, int(spec.run_id)))
    if any(right.start_ns <= left.start_ns for left, right in pairwise(specs)):
        raise ValueError("capture chronology is not strictly increasing")
    return specs


def extract_specs(
    specs: Sequence[base.CaptureSpec],
    creator_counts: Counter[str],
    *,
    verify_hashes: bool,
    horizons: Sequence[int] = base.HORIZONS_MS,
    certification_policy_index: int | None = None,
) -> tuple[list[base.ResearchRow], dict[str, Any]]:
    rows: list[base.ResearchRow] = []
    audit_runs = []
    for spec in specs:
        print(f"extract {spec.split} capture {spec.run_id}", flush=True)
        actual_sha = base.sha256_path(spec.events_path) if verify_hashes else spec.expected_sha256
        if actual_sha != spec.expected_sha256:
            raise ValueError(f"capture hash changed: {spec.run_id}")
        traces, parse_errors = base.load_capture(spec, creator_counts)
        included: Counter[str] = Counter()
        for trace in traces:
            for horizon_ms in horizons:
                decision_ns = trace.create_ns + horizon_ms * 1_000_000
                if decision_ns + max(base.HOLDS_MS) * 1_000_000 > spec.end_ns:
                    included["insufficient_tail"] += 1
                    continue
                features = base.snapshot(trace, horizon_ms)
                if features is None:
                    included["invalid_snapshot"] += 1
                    continue
                outcomes = tuple(
                    base.trade_outcome(trace, horizon_ms, policy) for policy in base.POLICIES
                )
                if any(outcome is None for outcome in outcomes):
                    included["invalid_outcome"] += 1
                    continue
                latency_outcomes = (
                    tuple(
                        base.trade_outcome(
                            trace,
                            horizon_ms,
                            base.POLICIES[certification_policy_index],
                            latency_ms=latency,
                        )
                        for latency in base.LATENCIES_MS
                    )
                    if certification_policy_index is not None
                    else ()
                )
                if any(outcome is None for outcome in latency_outcomes):
                    included["invalid_latency_outcome"] += 1
                    continue
                rows.append(
                    base.ResearchRow(
                        run_id=trace.run_id,
                        split=trace.split,
                        mint=trace.mint,
                        decision_ns=decision_ns,
                        horizon_ms=horizon_ms,
                        features=features,
                        outcomes=outcomes,  # type: ignore[arg-type]
                        latency_outcomes=latency_outcomes,  # type: ignore[arg-type]
                    )
                )
                included["rows"] += 1
        audit_runs.append(
            {
                "run_id": spec.run_id,
                "split": spec.split,
                "launches": len(traces),
                "parse_errors": parse_errors,
                "sha256": actual_sha,
                "hash_match": actual_sha == spec.expected_sha256,
                **dict(included),
            }
        )
    return rows, {
        "version": SCHEMA_VERSION,
        "runs": audit_runs,
        "rows": len(rows),
        "rows_by_split": dict(Counter(row.split for row in rows)),
        "rows_by_horizon": dict(Counter(str(row.horizon_ms) for row in rows)),
        "feature_names": list(base.FEATURE_NAMES),
        "future_values_in_features": False,
        "holdout_opened": any(spec.split == "holdout" for spec in specs),
        "production_paths_changed": 0,
    }


def development_census(rows: Sequence[base.ResearchRow]) -> list[dict[str, Any]]:
    position = base.STARTING_BANKROLL_SOL * base.POSITION_FRACTION
    output = []
    for horizon in base.HORIZONS_MS:
        for policy_index, policy in enumerate(base.POLICIES):
            record: dict[str, Any] = {
                "horizon_ms": horizon,
                "policy_index": policy_index,
                "policy": policy.key,
                "priority_fee_sol": 0.001,
            }
            for split in ("train", "validation"):
                pnls = [
                    base.net_pnl(row.outcomes[policy_index], 0.001, position)
                    for row in rows
                    if row.split == split and row.horizon_ms == horizon
                ]
                profits = [pnl for pnl in pnls if pnl > 0]
                losses = [pnl for pnl in pnls if pnl <= 0]
                record[split] = {
                    "launches": len(pnls),
                    "profitable": len(profits),
                    "profitable_percent": 100 * len(profits) / max(len(pnls), 1),
                    "oracle_pnl_sol": sum(pnls),
                    "oracle_profit_factor": sum(profits) / max(abs(sum(losses)), 1e-12),
                }
            output.append(record)
    return output


def _targets(rows: Sequence[base.ResearchRow], policy_index: int) -> np.ndarray:
    position = base.STARTING_BANKROLL_SOL * base.POSITION_FRACTION
    values = np.asarray(
        [base.net_pnl(row.outcomes[policy_index], 0.001, position) for row in rows],
        dtype=float,
    )
    # This is fixed economic winsorisation, not data-dependent tail tuning.
    return np.clip(values, -position, position * 2)


def fit_regime_models(
    train: Sequence[base.ResearchRow], policy_index: int
) -> dict[str, Any]:
    windows = sorted({row.run_id for row in train}, key=int)
    recent_ids = set(windows[-RECENT_TRAIN_WINDOWS:])
    recent = [row for row in train if row.run_id in recent_ids]
    matrix_all = base.feature_matrix(train)
    matrix_recent = base.feature_matrix(recent)
    targets_all = _targets(train, policy_index)
    targets_recent = _targets(recent, policy_index)

    def regressor() -> HistGradientBoostingRegressor:
        return HistGradientBoostingRegressor(
            learning_rate=0.04,
            max_iter=160,
            max_leaf_nodes=15,
            min_samples_leaf=80,
            l2_regularization=10.0,
            random_state=RANDOM_SEED,
        )

    def classifier() -> HistGradientBoostingClassifier:
        return HistGradientBoostingClassifier(
            learning_rate=0.04,
            max_iter=160,
            max_leaf_nodes=15,
            min_samples_leaf=80,
            l2_regularization=10.0,
            random_state=RANDOM_SEED,
        )

    reg_all = regressor().fit(matrix_all, targets_all)
    reg_recent = regressor().fit(matrix_recent, targets_recent)
    cls_all = classifier().fit(matrix_all, targets_all > 0)
    cls_recent = classifier().fit(matrix_recent, targets_recent > 0)
    return {
        "reg_all": reg_all,
        "reg_recent": reg_recent,
        "cls_all": cls_all,
        "cls_recent": cls_recent,
        "recent_window_ids": sorted(recent_ids, key=int),
    }


def score_family(models: Mapping[str, Any], rows: Sequence[base.ResearchRow], family: str) -> np.ndarray:
    matrix = base.feature_matrix(rows)
    ev_all = models["reg_all"].predict(matrix)
    if family == "expected_value_all":
        return ev_all
    ev_recent = models["reg_recent"].predict(matrix)
    if family == "expected_value_regime_blend":
        return 0.4 * ev_all + 0.6 * ev_recent
    if family == "expected_value_conservative":
        return np.minimum(ev_all, ev_recent)
    prob_all = models["cls_all"].predict_proba(matrix)[:, 1]
    prob_recent = models["cls_recent"].predict_proba(matrix)[:, 1]
    return 0.4 * prob_all + 0.6 * prob_recent


def robust_rank(metrics: Mapping[str, Any]) -> tuple[Any, ...]:
    windows = list(metrics["by_capture_window"].values())
    positive_windows = sum(window["pnl_sol"] > 0 for window in windows)
    worst_window = min((window["pnl_sol"] for window in windows), default=-999.0)
    requirements = (
        metrics["trades"] >= 30,
        metrics["capture_windows"] == 3,
        metrics["win_rate"] >= 0.65,
        metrics["net_pnl_sol"] > 0,
        metrics["profit_factor"] >= 1.25,
        metrics["maximum_drawdown_fraction"] <= 0.15,
        metrics["largest_winner_contribution"] <= 0.35,
        positive_windows >= 2,
        worst_window >= -0.03,
    )
    return (
        sum(requirements),
        *requirements,
        positive_windows,
        worst_window,
        metrics["net_pnl_sol"],
        metrics["profit_factor"],
    )


def development_search(
    rows: Sequence[base.ResearchRow], census: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    shortlisted = []
    for horizon in base.HORIZONS_MS:
        eligible = [row for row in census if row["horizon_ms"] == horizon]
        eligible.sort(
            key=lambda row: (
                row["train"]["oracle_profit_factor"],
                row["train"]["profitable"],
            ),
            reverse=True,
        )
        shortlisted.extend(eligible[:SHORTLIST_PER_HORIZON])
    results = []
    for index, config in enumerate(shortlisted, 1):
        print(f"fit regime candidate {index}/{len(shortlisted)}", flush=True)
        horizon = base.integer(config["horizon_ms"])
        policy_index = base.integer(config["policy_index"])
        train = [row for row in rows if row.split == "train" and row.horizon_ms == horizon]
        validation = [
            row for row in rows if row.split == "validation" and row.horizon_ms == horizon
        ]
        models = fit_regime_models(train, policy_index)
        for family in MODEL_FAMILIES:
            scores = score_family(models, validation, family)
            ordered = sorted(scores, reverse=True)
            thresholds = {
                float(ordered[min(target - 1, len(ordered) - 1)])
                for target in (30, 50, 80, 120, 180, 240, 360)
            }
            best = None
            for threshold in sorted(thresholds, reverse=True):
                metrics = base.simulate_predictions(
                    validation,
                    scores,
                    threshold,
                    policy_index=policy_index,
                    priority_fee_sol=0.001,
                )
                if best is None or robust_rank(metrics) > robust_rank(best):
                    best = metrics
            results.append(
                {
                    "horizon_ms": horizon,
                    "policy_index": policy_index,
                    "policy": base.POLICIES[policy_index].key,
                    "priority_fee_sol": 0.001,
                    "model_family": family,
                    "recent_train_windows": models["recent_window_ids"],
                    "validation": best,
                }
            )
    results.sort(key=lambda row: robust_rank(row["validation"]), reverse=True)
    winner = results[0] if results else None
    return {
        "version": SCHEMA_VERSION,
        "thesis_family": THESIS_FAMILY,
        "development_only": True,
        "holdout_rows_read": 0,
        "shortlist_policy": "top six policies per horizon by training-only oracle PF",
        "modelled_candidates": len(results),
        "selection_rule": "nine regime-robust validation gates, then window robustness and net economics",
        "winner": winner,
        "top_candidates": results[:20],
        "frozen_candidate_ready": bool(winner and robust_rank(winner["validation"])[0] == 9),
        "production_paths_changed": 0,
    }


def freeze_candidate(
    root: Path,
    audit: Mapping[str, Any],
    search: Mapping[str, Any],
    script_path: Path,
) -> dict[str, Any]:
    winner = search.get("winner")
    if not winner or not search.get("frozen_candidate_ready"):
        raise ValueError("development search did not pass every frozen gate")
    identity = {
        "thesis_family_identifier": THESIS_FAMILY,
        "source_code_fingerprint": base.sha256_path(script_path),
        "dataset_source_manifest_fingerprint": base.sha256_path(root / MANIFEST),
        "evidence_epoch": [
            {"run_id": row["run_id"], "sha256": row["sha256"]} for row in audit["runs"]
        ],
        "feature_set_fingerprint": base.stable_hash(base.FEATURE_NAMES),
        "model_family": winner["model_family"],
        "full_parameters": {
            "seed": RANDOM_SEED,
            "learning_rate": 0.04,
            "max_iter": 160,
            "max_leaf_nodes": 15,
            "min_samples_leaf": 80,
            "l2_regularization": 10.0,
            "recent_train_windows": RECENT_TRAIN_WINDOWS,
            "threshold": winner["validation"]["threshold"],
        },
        "causal_horizon_ms": winner["horizon_ms"],
        "candidate_risk_set_policy": "one independent launch at each frozen causal horizon",
        "chronological_split": {
            "train": [row["run_id"] for row in audit["runs"] if row["split"] == "train"],
            "validation": [
                row["run_id"] for row in audit["runs"] if row["split"] == "validation"
            ],
            "holdout": [
                row["run_id"]
                for row in json.loads((root / MANIFEST).read_text(encoding="utf-8"))["new_captures"]
                if row["split"] == "holdout"
            ],
        },
        "bankroll": base.STARTING_BANKROLL_SOL,
        "position_sizing": {
            "fraction": base.POSITION_FRACTION,
            "maximum_concurrent_positions": base.MAX_CONCURRENT_POSITIONS,
        },
        "fee_model": {
            "priority_fee_sol": 0.001,
            "tip_sol": base.TIP_SOL,
            "base_transaction_fee_sol": base.BASE_TRANSACTION_FEE_SOL,
            "protocol_and_creator_fee_bps": base.PROTOCOL_AND_CREATOR_FEE_BPS,
        },
        "output_guard": "reserve-valid exact constant-product quote",
        "latency_assumptions_ms": list(base.LATENCIES_MS),
        "execution_policy": "paper replay; one entry per launch; no re-entry",
        "exit_policy": asdict(base.POLICIES[base.integer(winner["policy_index"])]),
    }
    return {
        "version": SCHEMA_VERSION,
        "status": "FROZEN_FOR_ONE_TIME_LATER_EPOCH_HOLDOUT",
        "experiment_id": f"e4x-{base.stable_hash(identity)}",
        "identity": identity,
        "candidate": winner,
        "development_ledger_hash": winner["validation"]["ledger_hash"],
        "holdout_rows_read_before_freeze": 0,
        "live_evidence_authorised": False,
        "production_promotion_authorised": False,
        "production_paths_changed": 0,
    }


def historical_gate(
    economics: Mapping[str, Mapping[str, Any]], baseline: Mapping[str, Any]
) -> dict[str, Any]:
    zero = economics["0"]
    windows = list(zero["by_capture_window"].values())
    requirements = {
        "at_least_30_closed_trades": zero["trades"] >= 30,
        "all_three_holdout_windows_represented": zero["capture_windows"] == 3,
        "at_least_two_profitable_windows": sum(row["pnl_sol"] > 0 for row in windows) >= 2,
        "win_rate_at_least_65_percent": zero["win_rate"] >= 0.65,
        "wilson_lower_bound_at_least_50_percent": zero["wilson_95_lower_bound"] >= 0.50,
        "net_pnl_positive": zero["net_pnl_sol"] > 0,
        "profit_factor_at_least_1_25": zero["profit_factor"] >= 1.25,
        "maximum_drawdown_at_most_15_percent": zero["maximum_drawdown_fraction"] <= 0.15,
        "largest_winner_at_most_35_percent": zero["largest_winner_contribution"] <= 0.35,
        "every_latency_pnl_positive": all(row["net_pnl_sol"] > 0 for row in economics.values()),
        "every_latency_profit_factor_at_least_1_25": all(
            row["profit_factor"] >= 1.25 for row in economics.values()
        ),
        "beats_frozen_scalar_baseline_pnl": zero["net_pnl_sol"] > baseline["holdout"]["net_pnl_sol"],
    }
    passed = all(requirements.values())
    if passed:
        failure = None
    elif zero["trades"] < 30:
        failure = "INSUFFICIENT_LABELS"
    elif zero["net_pnl_sol"] <= 0:
        failure = "NEGATIVE_EXPECTANCY"
    elif zero["profit_factor"] < 1.25:
        failure = "INADEQUATE_PROFIT_FACTOR"
    elif zero["win_rate"] < 0.65:
        failure = "FALSE_POSITIVE_OVERLOAD"
    else:
        failure = "HOLDOUT_COLLAPSE"
    return {
        "status": "HISTORICAL_HOLDOUT_CONFIRMED" if passed else "NOT_CONCLUSIVE",
        "historical_qualification_passed": passed,
        "requirements": requirements,
        "failed_requirements": [name for name, passed in requirements.items() if not passed],
        "failure_classification": failure,
        "untouched_live_confirmation_required": True,
        "live_confirmation_authorised": passed,
        "production_promotion_authorised": False,
        "production_paths_changed": 0,
    }


def certify_holdout(root: Path, cache: Path, frozen_path: Path) -> dict[str, Any]:
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    with cache.open("rb") as handle:
        cached = pickle.load(handle)
    rows: list[base.ResearchRow] = cached["rows"]
    candidate = frozen["candidate"]
    horizon = base.integer(candidate["horizon_ms"])
    policy_index = base.integer(candidate["policy_index"])
    train = [row for row in rows if row.split == "train" and row.horizon_ms == horizon]
    validation = [row for row in rows if row.split == "validation" and row.horizon_ms == horizon]
    models = fit_regime_models(train, policy_index)
    validation_scores = score_family(models, validation, str(candidate["model_family"]))
    threshold = base.finite(candidate["validation"]["threshold"])
    validation_repeat = base.simulate_predictions(
        validation,
        validation_scores,
        threshold,
        policy_index=policy_index,
        priority_fee_sol=0.001,
    )
    if validation_repeat["ledger_hash"] != frozen["development_ledger_hash"]:
        raise ValueError("frozen development ledger did not reproduce")
    holdout_specs = [spec for spec in all_capture_specs(root, include_holdout=True) if spec.split == "holdout"]
    holdout, holdout_audit = extract_specs(
        holdout_specs,
        Counter(cached["creator_counts"]),
        verify_hashes=True,
        horizons=(horizon,),
        certification_policy_index=policy_index,
    )
    holdout_scores = score_family(models, holdout, str(candidate["model_family"]))
    economics = {
        str(latency): base.simulate_predictions(
            holdout,
            holdout_scores,
            threshold,
            policy_index=policy_index,
            priority_fee_sol=0.001,
            latency_index=index,
        )
        for index, latency in enumerate(base.LATENCIES_MS)
    }
    baseline = base.scalar_baseline(
        validation,
        holdout,
        target_trades=validation_repeat["trades"],
        policy_index=policy_index,
        priority_fee_sol=0.001,
    )
    repeat_hashes = {
        str(latency): base.simulate_predictions(
            holdout,
            holdout_scores,
            threshold,
            policy_index=policy_index,
            priority_fee_sol=0.001,
            latency_index=index,
        )["ledger_hash"]
        for index, latency in enumerate(base.LATENCIES_MS)
    }
    if any(economics[key]["ledger_hash"] != repeat_hashes[key] for key in economics):
        raise ValueError("holdout replay is not deterministic")
    return {
        "version": SCHEMA_VERSION,
        "experiment_id": frozen["experiment_id"],
        "candidate": candidate,
        "development_reproduction": validation_repeat,
        "holdout_data_audit": holdout_audit,
        "holdout_rows": len(holdout),
        "latencies": economics,
        "strongest_scalar_baseline": baseline,
        "deterministic_replay": True,
        "verdict": historical_gate(economics, baseline),
        "production_paths_changed": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--cache", type=Path, default=Path(".tmp-regime-robust-development.pkl"))
    parser.add_argument("--reuse-cache", action="store_true")
    parser.add_argument("--skip-source-hashes", action="store_true")
    parser.add_argument("--certify-holdout", action="store_true")
    args = parser.parse_args()
    root = args.repo_root.resolve()
    cache = (root / args.cache).resolve()
    output = root / "artifacts"
    frozen_path = output / "e4-v12-regime-robust-frozen-candidate.json"
    if args.certify_holdout:
        if not frozen_path.exists():
            raise ValueError("freeze a development candidate before opening holdout")
        result = certify_holdout(root, cache, frozen_path)
        base.write_json(output / "e4-v12-regime-robust-historical-economics.json", result)
        print(json.dumps(result["verdict"], indent=2, sort_keys=True))
        return
    if args.reuse_cache:
        with cache.open("rb") as handle:
            cached = pickle.load(handle)
        rows = cached["rows"]
        audit = cached["audit"]
    else:
        creators: Counter[str] = Counter()
        development_specs = all_capture_specs(root, include_holdout=False)
        rows, audit = extract_specs(
            development_specs,
            creators,
            verify_hashes=not args.skip_source_hashes,
        )
        with cache.open("wb") as handle:
            pickle.dump({"rows": rows, "audit": audit, "creator_counts": creators}, handle)
    census = development_census(rows)
    search = development_search(rows, census)
    base.write_json(output / "e4-v12-regime-robust-data-audit.json", audit)
    base.write_json(output / "e4-v12-regime-robust-opportunity-census.json", census)
    base.write_json(output / "e4-v12-regime-robust-development-search.json", search)
    if search["frozen_candidate_ready"]:
        frozen = freeze_candidate(root, audit, search, Path(__file__).resolve())
        base.write_json(frozen_path, frozen)
    print(json.dumps({"winner": search["winner"], "ready": search["frozen_candidate_ready"]}, indent=2))


if __name__ == "__main__":
    main()

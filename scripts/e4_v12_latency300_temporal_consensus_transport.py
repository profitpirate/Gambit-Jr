#!/usr/bin/env python3
"""Diagnose temporal-model consensus inside exact-300 ms selections."""

from __future__ import annotations

import hashlib
import json
import pickle
from pathlib import Path
from typing import Any

import e4_v12_causal_actor_memory as actors
import e4_v12_latency300_conformal as baseline
import e4_v12_latency300_labels as labels
import e4_v12_latency300_selected_residual_transport as residual
import e4_v12_online_conformal_precision as conformal
import e4_v12_profit_survival_search as base
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

OUTPUT = Path("artifacts/e4-v12-latency300-temporal-consensus-transport.json")
SCHEMA_VERSION = "e4-v12-latency300-temporal-consensus-transport-v1"
DIAGNOSTIC_FAMILY = "latency300-temporal-model-consensus-v12"
TRAIN_WINDOW_POLICIES = ("all_prior_windows", "recent_16_windows", "recent_8_windows")
RECENT_WINDOW_LENGTHS: tuple[int | None, ...] = (None, 16, 8)
GLOBAL_AUC_FLOOR = 0.52
RESIDUAL_AUC_FLOOR = 0.55
SOURCE_FILES = (
    "scripts/e4_v12_latency300_temporal_consensus_transport.py",
    "scripts/e4_v12_latency300_selected_residual_transport.py",
    "scripts/e4_v12_latency300_conformal.py",
    "scripts/e4_v12_latency300_labels.py",
    "scripts/e4_v12_online_conformal_precision.py",
    "scripts/e4_v12_causal_actor_memory.py",
    "scripts/e4_v12_adaptive_exit_search.py",
    "scripts/e4_v12_profit_survival_search.py",
)


def sha256_lf(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def source_code_fingerprint(root: Path | None = None) -> str:
    root = Path.cwd() if root is None else root
    return base.stable_hash(
        {relative: sha256_lf(root / relative) for relative in SOURCE_FILES}
    )


def empirical_percentile(reference: np.ndarray, values: np.ndarray) -> np.ndarray:
    """Map scores to a fixed calibration CDF without using labels."""
    ordered = np.sort(np.asarray(reference, dtype=np.float64), kind="stable")
    if len(ordered) == 0:
        raise ValueError("percentile reference cannot be empty")
    return np.searchsorted(ordered, values, side="right") / len(ordered)


def auc_or_chance(y: np.ndarray, values: np.ndarray) -> float:
    if len(np.unique(y)) < 2 or len(np.unique(values)) < 2:
        return 0.5
    return float(roc_auc_score(y, values))


def model_for(fold_number: int, member_number: int) -> HistGradientBoostingClassifier:
    parameters = {
        key: value
        for key, value in baseline.MODEL_PARAMETERS.items()
        if key != "random_seed_base"
    }
    return HistGradientBoostingClassifier(
        **parameters,
        early_stopping=False,
        random_state=(
            baseline.MODEL_PARAMETERS["random_seed_base"]
            + member_number * 10_000
            + fold_number * 100
            + conformal.POLICY_INDEX
        ),
    )


def main() -> dict[str, Any]:
    metadata = json.loads(labels.METADATA_PATH.read_text(encoding="utf-8"))
    with conformal.CACHE.open("rb") as handle:
        payload = pickle.load(handle)
    if metadata["dataset_manifest_sha256"] != payload["manifest_sha256"]:
        raise ValueError("300 ms labels target another evidence manifest")
    if metadata["pnl_sha256"] != base.sha256_path(labels.PNL_PATH):
        raise ValueError("300 ms label matrix fingerprint changed")
    if metadata["all_source_hashes_verified"] is not True:
        raise ValueError("300 ms source hashes were not verified")
    if metadata["active_untouched_live_data_used"] is not False:
        raise ValueError("active untouched live evidence was consumed")

    rows = payload["rows"]
    features = np.asarray([row.features for row in rows], dtype=np.float32)
    if features.shape != (len(rows), len(actors.FEATURE_NAMES)):
        raise ValueError("causal feature matrix shape changed")
    pnl = np.load(labels.PNL_PATH, allow_pickle=False)
    if len(pnl) != len(rows) or not bool(np.all(np.isfinite(pnl))):
        raise ValueError("300 ms labels are incomplete")
    decisions = np.asarray([row.decision_ns for row in rows], dtype=np.int64)
    windows = sorted({row.run_id for row in rows}, key=int)
    window_index = {run_id: index for index, run_id in enumerate(windows)}
    row_windows = np.asarray(
        [window_index[row.run_id] for row in rows], dtype=np.int16
    )

    fold_records: list[dict[str, Any]] = []
    residual_aucs: list[float] = []
    global_aucs: list[float] = []
    baseline_trades = 0
    baseline_wins = 0
    for fold_number, (test_start, calibration_size, test_size) in enumerate(
        conformal.FOLDS
    ):
        fit_end = test_start - calibration_size
        calibration = np.flatnonzero(
            (row_windows >= fit_end) & (row_windows < test_start)
        )
        test = np.flatnonzero(
            (row_windows >= test_start) & (row_windows < test_start + test_size)
        )
        calibration = calibration[
            np.argsort(decisions[calibration], kind="stable")
        ]
        test = test[np.argsort(decisions[test], kind="stable")]
        member_calibration_scores: list[np.ndarray] = []
        member_test_scores: list[np.ndarray] = []
        training_audit: list[dict[str, Any]] = []
        for member_number, (policy, recent_windows) in enumerate(
            zip(TRAIN_WINDOW_POLICIES, RECENT_WINDOW_LENGTHS, strict=True)
        ):
            fit_start = 0 if recent_windows is None else max(0, fit_end - recent_windows)
            fit = np.flatnonzero(
                (row_windows >= fit_start) & (row_windows < fit_end)
            )
            model = model_for(fold_number, member_number)
            model.fit(features[fit], pnl[fit] > 0)
            calibration_scores = model.predict_proba(features[calibration])[:, 1]
            test_scores = model.predict_proba(features[test])[:, 1]
            member_calibration_scores.append(calibration_scores)
            member_test_scores.append(test_scores)
            training_audit.append(
                {
                    "policy": policy,
                    "fit_windows": windows[fit_start:fit_end],
                    "fit_rows": len(fit),
                    "fit_profitable_rows": int(np.sum(pnl[fit] > 0)),
                }
            )

        test_percentiles = np.vstack(
            [
                empirical_percentile(calibration_scores, test_scores)
                for calibration_scores, test_scores in zip(
                    member_calibration_scores,
                    member_test_scores,
                    strict=True,
                )
            ]
        )
        test_consensus = np.min(test_percentiles, axis=0)

        baseline_mask, thresholds = conformal.rolling_selection(
            member_calibration_scores[0], member_test_scores[0]
        )
        replay = conformal.replay(
            rows,
            test[baseline_mask],
            pnl,
            member_test_scores[0][baseline_mask],
        )
        executed = residual.ledger_indexes(rows, replay["ledger"])
        test_position = {int(index): position for position, index in enumerate(test)}
        executed_positions = np.asarray(
            [test_position[int(index)] for index in executed], dtype=np.int64
        )
        executed_outcomes = np.asarray(
            [trade["pnl_sol"] > 0 for trade in replay["ledger"]], dtype=np.bool_
        )
        residual_auc = auc_or_chance(
            executed_outcomes, test_consensus[executed_positions]
        )
        global_auc = auc_or_chance(pnl[test] > 0, test_consensus)
        residual_aucs.append(residual_auc)
        global_aucs.append(global_auc)
        baseline_trades += len(executed)
        baseline_wins += int(np.sum(executed_outcomes))
        fold_records.append(
            {
                "fold": fold_number,
                "training_members": training_audit,
                "calibration_windows": windows[fit_end:test_start],
                "test_windows": windows[test_start : test_start + test_size],
                "baseline_attempted_trades": replay["attempted_trades"],
                "baseline_executed_trades": len(executed),
                "baseline_wins": int(np.sum(executed_outcomes)),
                "baseline_losses": int(np.sum(~executed_outcomes)),
                "baseline_threshold_hash": base.stable_hash(thresholds),
                "baseline_ledger_hash": replay["ledger_hash"],
                "minimum_member_percentile_global_auc": global_auc,
                "minimum_member_percentile_residual_auc": residual_auc,
                "consensus_score_minimum": float(np.min(test_consensus)),
                "consensus_score_maximum": float(np.max(test_consensus)),
                "consensus_score_sha256": hashlib.sha256(
                    test_consensus.astype("<f8", copy=False).tobytes()
                ).hexdigest(),
            }
        )

    global_transport = all(value >= GLOBAL_AUC_FLOOR for value in global_aucs)
    residual_transport = all(
        value >= RESIDUAL_AUC_FLOOR for value in residual_aucs
    )
    warranted = global_transport and residual_transport
    baseline_artifact = json.loads(baseline.OUTPUT.read_text(encoding="utf-8"))
    identity = {
        "diagnostic_family_identifier": DIAGNOSTIC_FAMILY,
        "source_code_fingerprint": source_code_fingerprint(),
        "dataset_source_manifest_fingerprint": payload["manifest_sha256"],
        "evidence_epoch": windows,
        "baseline_experiment_id": baseline_artifact["experiment_id"],
        "feature_set_fingerprint": base.stable_hash(actors.FEATURE_NAMES),
        "label_matrix_sha256": metadata["pnl_sha256"],
        "model_family": "three_vintage_histogram_boosting_consensus",
        "training_window_policies": TRAIN_WINDOW_POLICIES,
        "recent_window_lengths": RECENT_WINDOW_LENGTHS,
        "consensus_score": "minimum calibration-CDF percentile across three members",
        "global_auc_floor": GLOBAL_AUC_FLOOR,
        "residual_auc_floor": RESIDUAL_AUC_FLOOR,
        "latency_ms": labels.LATENCY_MS,
    }
    output = {
        "version": SCHEMA_VERSION,
        "diagnostic_id": f"e4d-{base.stable_hash(identity)}",
        "identity": identity,
        "scientific_basis": (
            "No individual causal feature transported inside the baseline's "
            "false-positive tail, so test whether epistemic agreement among "
            "long-, medium-, and short-history models supplies new information."
        ),
        "label_audit": metadata,
        "selection_audit": {
            "folds": fold_records,
            "exact_baseline_replayed": True,
            "executed_trades": baseline_trades,
            "wins": baseline_wins,
            "losses": baseline_trades - baseline_wins,
            "concurrency_rejections_preserved": True,
            "winner_definition": "fee-net replay ledger pnl_sol > 0",
        },
        "consensus_audit": {
            "members": len(TRAIN_WINDOW_POLICIES),
            "training_window_policies": list(TRAIN_WINDOW_POLICIES),
            "calibration_cdf_uses_labels": False,
            "outcomes_used_as_features": False,
            "minimum_member_percentile_global_auc_by_fold": global_aucs,
            "minimum_member_percentile_residual_auc_by_fold": residual_aucs,
            "global_transport_passed": global_transport,
            "residual_transport_passed": residual_transport,
            "active_untouched_live_data_used": False,
            "production_paths_changed": 0,
        },
        "finding": (
            "The preregistered minimum-member consensus score "
            f"{'did' if warranted else 'did not'} transport above both the "
            f"{GLOBAL_AUC_FLOOR:.2f} global and {RESIDUAL_AUC_FLOOR:.2f} "
            "selected-residual AUC floors in every chronological fold."
        ),
        "consensus_candidate_warranted": warranted,
        "candidate_fitted": False,
        "retired_diagnostic_family": not warranted,
        "failure_classification": None if warranted else "VALIDATION_COLLAPSE",
        "material_change_required_before_rerun": (
            "One preregistered temporal-consensus candidate with the frozen score."
            if warranted
            else "A genuinely new causal signal or model family; training-window, "
            "member-count, aggregation, threshold, and parameter variants are prohibited."
        ),
        "development_gate_passed": False,
        "untouched_holdout_passed": False,
        "live_confirmation_authorised": False,
        "production_promotion_authorised": False,
        "production_deployment_authorised": False,
        "active_untouched_live_data_used": False,
        "production_paths_changed": 0,
    }
    base.write_json(OUTPUT, output)
    print(json.dumps(output, indent=2, sort_keys=True))
    return output


if __name__ == "__main__":
    main()

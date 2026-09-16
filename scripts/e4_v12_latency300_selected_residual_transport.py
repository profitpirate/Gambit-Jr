#!/usr/bin/env python3
"""Diagnose residual false-positive features inside exact-300 ms selections."""

from __future__ import annotations

import hashlib
import json
import pickle
from pathlib import Path
from typing import Any

import e4_v12_causal_actor_memory as actors
import e4_v12_latency300_conformal as baseline
import e4_v12_latency300_labels as labels
import e4_v12_latency300_market_flow_transport as market
import e4_v12_online_conformal_precision as conformal
import e4_v12_profit_survival_search as base
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

OUTPUT = Path("artifacts/e4-v12-latency300-selected-residual-transport.json")
SCHEMA_VERSION = "e4-v12-latency300-selected-residual-transport-v1"
DIAGNOSTIC_FAMILY = "latency300-selected-residual-transport-v12"
RESIDUAL_AUC_FLOOR = 0.55
SOURCE_FILES = (
    "scripts/e4_v12_latency300_selected_residual_transport.py",
    "scripts/e4_v12_latency300_market_flow_transport.py",
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


def auc_or_chance(y: np.ndarray, values: np.ndarray) -> float:
    if len(np.unique(y)) < 2 or len(np.unique(values)) < 2:
        return 0.5
    return float(roc_auc_score(y, values))


def ledger_indexes(rows: list[Any], ledger: list[dict[str, Any]]) -> np.ndarray:
    by_key = {
        (str(row.run_id), row.mint, int(row.decision_ns)): index
        for index, row in enumerate(rows)
    }
    indexes = []
    for trade in ledger:
        key = (
            str(trade["run_id"]),
            str(trade["mint"]),
            int(trade["decision_ns"]),
        )
        if key not in by_key:
            raise ValueError(f"replayed trade not found in evidence rows: {key}")
        indexes.append(by_key[key])
    return np.asarray(indexes, dtype=np.int64)


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
    base_features = np.asarray([row.features for row in rows], dtype=np.float64)
    decisions = np.asarray([row.decision_ns for row in rows], dtype=np.int64)
    flow = market.causal_market_flow_matrix(decisions, base_features)
    features = np.concatenate((base_features, flow), axis=1)
    feature_names = (*actors.FEATURE_NAMES, *market.FLOW_FEATURE_NAMES)
    pnl = np.load(labels.PNL_PATH, allow_pickle=False)
    if features.shape != (len(rows), len(feature_names)):
        raise ValueError("residual feature matrix shape changed")
    windows = sorted({row.run_id for row in rows}, key=int)
    window_index = {run_id: index for index, run_id in enumerate(windows)}
    row_windows = np.asarray([window_index[row.run_id] for row in rows], dtype=np.int16)
    selected_by_fold: list[np.ndarray] = []
    outcomes_by_fold: list[np.ndarray] = []
    fold_audit: list[dict[str, Any]] = []
    for fold_number, (test_start, calibration_size, test_size) in enumerate(
        conformal.FOLDS
    ):
        fit_end = test_start - calibration_size
        fit = np.flatnonzero(row_windows < fit_end)
        calibration = np.flatnonzero(
            (row_windows >= fit_end) & (row_windows < test_start)
        )
        test = np.flatnonzero(
            (row_windows >= test_start) & (row_windows < test_start + test_size)
        )
        calibration = calibration[np.argsort(decisions[calibration], kind="stable")]
        test = test[np.argsort(decisions[test], kind="stable")]
        model = HistGradientBoostingClassifier(
            **{
                key: value
                for key, value in baseline.MODEL_PARAMETERS.items()
                if key != "random_seed_base"
            },
            early_stopping=False,
            random_state=(
                baseline.MODEL_PARAMETERS["random_seed_base"]
                + fold_number * 100
                + conformal.POLICY_INDEX
            ),
        )
        model.fit(base_features[fit].astype(np.float32), pnl[fit] > 0)
        calibration_scores = model.predict_proba(
            base_features[calibration].astype(np.float32)
        )[:, 1]
        test_scores = model.predict_proba(base_features[test].astype(np.float32))[:, 1]
        mask, thresholds = conformal.rolling_selection(calibration_scores, test_scores)
        replay = conformal.replay(rows, test[mask], pnl, test_scores[mask])
        selected = ledger_indexes(rows, replay["ledger"])
        outcomes = np.asarray(
            [trade["pnl_sol"] > 0 for trade in replay["ledger"]], dtype=np.bool_
        )
        selected_by_fold.append(selected)
        outcomes_by_fold.append(outcomes)
        fold_audit.append(
            {
                "fold": fold_number,
                "fit_windows": windows[:fit_end],
                "calibration_windows": windows[fit_end:test_start],
                "test_windows": windows[test_start : test_start + test_size],
                "attempted_trades": replay["attempted_trades"],
                "executed_trades": len(selected),
                "wins": int(np.sum(outcomes)),
                "losses": int(np.sum(~outcomes)),
                "threshold_hash": base.stable_hash(thresholds),
                "ledger_hash": replay["ledger_hash"],
            }
        )
    records: list[dict[str, Any]] = []
    for column, name in enumerate(feature_names):
        raw_aucs = [
            auc_or_chance(outcomes, features[index, column])
            for index, outcomes in zip(
                selected_by_fold, outcomes_by_fold, strict=True
            )
        ]
        direction = 1 if raw_aucs[0] >= 0.5 else -1
        oriented = [auc if direction == 1 else 1.0 - auc for auc in raw_aucs]
        records.append(
            {
                "feature": name,
                "feature_family": (
                    "base" if column < len(actors.FEATURE_NAMES) else "market_flow"
                ),
                "fold0_direction": (
                    "higher_is_win" if direction == 1 else "lower_is_win"
                ),
                "raw_auc_by_fold": raw_aucs,
                "oriented_auc_by_fold": oriented,
                "minimum_later_oriented_auc": min(oriented[1:]),
                "mean_later_oriented_auc": float(np.mean(oriented[1:])),
                "direction_consistent_all_folds": all(auc >= 0.5 for auc in oriented),
                "all_folds_above_residual_floor": all(
                    auc >= RESIDUAL_AUC_FLOOR for auc in oriented
                ),
            }
        )
    records.sort(
        key=lambda row: (
            row["all_folds_above_residual_floor"],
            row["minimum_later_oriented_auc"],
            row["mean_later_oriented_auc"],
        ),
        reverse=True,
    )
    stable = [row["feature"] for row in records if row["all_folds_above_residual_floor"]]
    identity = {
        "diagnostic_family_identifier": DIAGNOSTIC_FAMILY,
        "source_code_fingerprint": source_code_fingerprint(),
        "dataset_source_manifest_fingerprint": payload["manifest_sha256"],
        "evidence_epoch": windows,
        "baseline_experiment_id": json.loads(
            baseline.OUTPUT.read_text(encoding="utf-8")
        )["experiment_id"],
        "feature_set_fingerprint": base.stable_hash(feature_names),
        "label_matrix_sha256": metadata["pnl_sha256"],
        "latency_ms": labels.LATENCY_MS,
        "residual_auc_floor": RESIDUAL_AUC_FLOOR,
        "orientation_policy": "orient on fold 0 executed trades and freeze for folds 1-3",
    }
    warranted = len(stable) > 0
    output = {
        "version": SCHEMA_VERSION,
        "diagnostic_id": f"e4d-{base.stable_hash(identity)}",
        "identity": identity,
        "scientific_basis": (
            "A profitable sparse selector can still miss its precision gate if a "
            "causal feature separates wins from false positives only inside its selected tail."
        ),
        "label_audit": metadata,
        "selection_audit": {
            "folds": fold_audit,
            "executed_trades": sum(len(index) for index in selected_by_fold),
            "wins": sum(int(np.sum(outcomes)) for outcomes in outcomes_by_fold),
            "losses": sum(int(np.sum(~outcomes)) for outcomes in outcomes_by_fold),
            "exact_baseline_replayed": True,
            "concurrency_rejections_preserved": True,
            "winner_definition": "fee-net replay ledger pnl_sol > 0",
        },
        "feature_audit": {
            "base_features": len(actors.FEATURE_NAMES),
            "market_flow_features": len(market.FLOW_FEATURE_NAMES),
            "features": len(feature_names),
            "matrix_sha256": hashlib.sha256(
                features.astype("<f8", copy=False).tobytes()
            ).hexdigest(),
            "outcomes_used_as_features": False,
            "active_untouched_live_data_used": False,
            "production_paths_changed": 0,
        },
        "features": records,
        "stable_residual_feature_count": len(stable),
        "stable_residual_features": stable,
        "finding": (
            f"{len(stable)} of {len(feature_names)} causal features separated wins "
            f"from executed false positives with oriented AUC >= {RESIDUAL_AUC_FLOOR:.2f} "
            "in every chronological fold."
        ),
        "veto_candidate_warranted": warranted,
        "candidate_fitted": False,
        "retired_diagnostic_family": not warranted,
        "failure_classification": None if warranted else "VALIDATION_COLLAPSE",
        "material_change_required_before_rerun": (
            "One preregistered causal veto derived from fit/calibration data only."
            if warranted
            else "A new causal feature family; base/market-flow residual threshold retuning is prohibited."
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

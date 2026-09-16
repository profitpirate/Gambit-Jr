#!/usr/bin/env python3
"""Diagnose chronological transport of causal features at exact 300 ms."""

from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Any

import e4_v12_causal_actor_memory as actors
import e4_v12_latency300_labels as labels
import e4_v12_online_conformal_precision as conformal
import e4_v12_profit_survival_search as base
import numpy as np
from scipy.stats import ks_2samp
from sklearn.metrics import roc_auc_score

OUTPUT = Path("artifacts/e4-v12-latency300-feature-transport.json")
SCHEMA_VERSION = "e4-v12-latency300-feature-transport-v1"
DIAGNOSTIC_FAMILY = "latency300-causal-feature-transport-v12"
WINDOWS_PER_EPOCH = 16
DESCRIPTIVE_AUC_FLOOR = 0.52
SOURCE_FILES = (
    "scripts/e4_v12_latency300_feature_transport.py",
    "scripts/e4_v12_latency300_temporal_drift.py",
    "scripts/e4_v12_latency300_labels.py",
    "scripts/e4_v12_online_conformal_precision.py",
    "scripts/e4_v12_causal_actor_memory.py",
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


def auc_or_chance(y: np.ndarray, values: np.ndarray) -> float:
    if len(np.unique(y)) < 2 or len(np.unique(values)) < 2:
        return 0.5
    return float(roc_auc_score(y, values))


def main() -> dict[str, Any]:
    metadata = json.loads(labels.METADATA_PATH.read_text(encoding="utf-8"))
    with conformal.CACHE.open("rb") as handle:
        payload = pickle.load(handle)
    if metadata["dataset_manifest_sha256"] != payload["manifest_sha256"]:
        raise ValueError("300 ms labels target another evidence manifest")
    if metadata["pnl_sha256"] != base.sha256_path(labels.PNL_PATH):
        raise ValueError("300 ms label matrix fingerprint changed")
    if metadata["all_source_hashes_verified"] is not True:
        raise ValueError("source hashes were not verified")
    if metadata["active_untouched_live_data_used"] is not False:
        raise ValueError("active untouched live evidence was consumed")
    rows = payload["rows"]
    features = np.asarray([row.features for row in rows], dtype=np.float64)
    pnl = np.load(labels.PNL_PATH, allow_pickle=False)
    if features.shape != (len(rows), len(actors.FEATURE_NAMES)):
        raise ValueError("feature matrix shape changed")
    if len(pnl) != len(rows) or not bool(np.all(np.isfinite(pnl))):
        raise ValueError("300 ms labels are incomplete")
    windows = sorted({row.run_id for row in rows}, key=int)
    if len(windows) != 4 * WINDOWS_PER_EPOCH:
        raise ValueError("diagnostic requires exactly four 16-window epochs")
    window_index = {run_id: index for index, run_id in enumerate(windows)}
    row_windows = np.asarray(
        [window_index[row.run_id] for row in rows], dtype=np.int16
    )
    labels_positive = pnl > 0
    epoch_masks = [
        (row_windows >= start) & (row_windows < start + WINDOWS_PER_EPOCH)
        for start in range(0, len(windows), WINDOWS_PER_EPOCH)
    ]
    epochs = [
        {
            "epoch": epoch,
            "window_indices": [
                epoch * WINDOWS_PER_EPOCH,
                (epoch + 1) * WINDOWS_PER_EPOCH - 1,
            ],
            "windows": windows[
                epoch * WINDOWS_PER_EPOCH : (epoch + 1) * WINDOWS_PER_EPOCH
            ],
            "rows": int(mask.sum()),
            "profitable_rows": int(labels_positive[mask].sum()),
            "positive_rate": float(np.mean(labels_positive[mask])),
        }
        for epoch, mask in enumerate(epoch_masks)
    ]
    records = []
    for column, name in enumerate(actors.FEATURE_NAMES):
        raw_aucs = [
            auc_or_chance(labels_positive[mask], features[mask, column])
            for mask in epoch_masks
        ]
        direction = 1 if raw_aucs[0] >= 0.5 else -1
        oriented = [auc if direction == 1 else 1.0 - auc for auc in raw_aucs]
        statistic, pvalue = ks_2samp(
            features[epoch_masks[0], column],
            features[epoch_masks[-1], column],
        )
        records.append(
            {
                "feature": name,
                "earliest_epoch_direction": (
                    "higher_is_positive" if direction == 1 else "lower_is_positive"
                ),
                "raw_auc_by_epoch": raw_aucs,
                "earliest_oriented_auc": oriented[0],
                "later_oriented_auc": oriented[1:],
                "minimum_later_oriented_auc": min(oriented[1:]),
                "mean_later_oriented_auc": float(np.mean(oriented[1:])),
                "direction_consistent_all_epochs": all(
                    auc >= 0.5 for auc in oriented
                ),
                "all_epochs_above_descriptive_floor": all(
                    auc >= DESCRIPTIVE_AUC_FLOOR for auc in oriented
                ),
                "first_to_last_oriented_auc_change": oriented[-1] - oriented[0],
                "first_to_last_ks_statistic": float(statistic),
                "first_to_last_ks_pvalue": float(pvalue),
            }
        )
    records.sort(
        key=lambda row: (
            row["all_epochs_above_descriptive_floor"],
            row["minimum_later_oriented_auc"],
            row["mean_later_oriented_auc"],
        ),
        reverse=True,
    )
    stable = [
        row["feature"]
        for row in records
        if row["all_epochs_above_descriptive_floor"]
    ]
    identity = {
        "diagnostic_family_identifier": DIAGNOSTIC_FAMILY,
        "source_code_fingerprint": source_code_fingerprint(),
        "dataset_source_manifest_fingerprint": payload["manifest_sha256"],
        "evidence_epoch": windows,
        "feature_set_fingerprint": base.stable_hash(actors.FEATURE_NAMES),
        "label_matrix_sha256": metadata["pnl_sha256"],
        "latency_ms": labels.LATENCY_MS,
        "chronological_epochs": [row["windows"] for row in epochs],
        "orientation_policy": (
            "orient each feature once from epoch 0, then freeze direction for "
            "epochs 1-3"
        ),
        "descriptive_auc_floor": DESCRIPTIVE_AUC_FLOOR,
    }
    output = {
        "version": SCHEMA_VERSION,
        "diagnostic_id": f"e4d-{base.stable_hash(identity)}",
        "identity": identity,
        "label_audit": metadata,
        "epochs": epochs,
        "features": records,
        "feature_count": len(records),
        "direction_consistent_features": sum(
            row["direction_consistent_all_epochs"] for row in records
        ),
        "all_epoch_auc_52_features": stable,
        "all_epoch_auc_52_feature_count": len(stable),
        "finding": (
            f"{len(stable)} of {len(records)} existing causal features preserved "
            "their epoch-0 direction with oriented AUC >= 0.52 in all four "
            "epochs. This is descriptive transport evidence, not a fitted "
            "candidate or approval."
        ),
        "candidate_fitted": False,
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

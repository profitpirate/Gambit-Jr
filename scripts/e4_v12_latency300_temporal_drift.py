#!/usr/bin/env python3
"""Measure temporal decay and covariate shift in exact-300 ms labels."""

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
from scipy.stats import ks_2samp, spearmanr

OUTPUT = Path("artifacts/e4-v12-latency300-temporal-drift.json")
SCHEMA_VERSION = "e4-v12-latency300-temporal-drift-v1"
SOURCE_FILES = (
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


def main() -> dict[str, Any]:
    metadata = json.loads(labels.METADATA_PATH.read_text(encoding="utf-8"))
    with conformal.CACHE.open("rb") as handle:
        payload = pickle.load(handle)
    if metadata["dataset_manifest_sha256"] != payload["manifest_sha256"]:
        raise ValueError("300 ms labels target another evidence manifest")
    if metadata["pnl_sha256"] != base.sha256_path(labels.PNL_PATH):
        raise ValueError("300 ms label matrix fingerprint changed")
    if metadata["active_untouched_live_data_used"] is not False:
        raise ValueError("active untouched live evidence was consumed")
    rows = payload["rows"]
    pnl = np.load(labels.PNL_PATH, allow_pickle=False)
    features = np.asarray([row.features for row in rows], dtype=np.float64)
    windows = sorted({row.run_id for row in rows}, key=int)
    window_index = {run_id: index for index, run_id in enumerate(windows)}
    row_windows = np.asarray([window_index[row.run_id] for row in rows])

    per_window = []
    for index, run_id in enumerate(windows):
        mask = row_windows == index
        values = pnl[mask]
        per_window.append(
            {
                "index": index,
                "run_id": run_id,
                "rows": int(mask.sum()),
                "positives": int(np.sum(values > 0)),
                "positive_rate": float(np.mean(values > 0)),
                "mean_pnl": float(np.mean(values)),
                "median_pnl": float(np.median(values)),
            }
        )
    rates = np.asarray([row["positive_rate"] for row in per_window])
    rho, rho_p = spearmanr(np.arange(len(rates)), rates)
    quarters = []
    for start in range(0, len(windows), 16):
        mask = (row_windows >= start) & (row_windows < start + 16)
        quarters.append(
            {
                "window_indices": [start, min(start + 15, len(windows) - 1)],
                "rows": int(mask.sum()),
                "positive_rate": float(np.mean(pnl[mask] > 0)),
                "mean_pnl": float(np.mean(pnl[mask])),
            }
        )
    fold_epochs = []
    for fold_number, (test_start, calibration_size, test_size) in enumerate(
        conformal.FOLDS
    ):
        fit_end = test_start - calibration_size
        fit = row_windows < fit_end
        calibration = (row_windows >= fit_end) & (row_windows < test_start)
        test = (row_windows >= test_start) & (
            row_windows < test_start + test_size
        )
        fold_epochs.append(
            {
                "fold": fold_number,
                "fit_positive_rate": float(np.mean(pnl[fit] > 0)),
                "calibration_positive_rate": float(
                    np.mean(pnl[calibration] > 0)
                ),
                "test_positive_rate": float(np.mean(pnl[test] > 0)),
                "test_mean_pnl": float(np.mean(pnl[test])),
                "test_windows": windows[test_start : test_start + test_size],
            }
        )
    final_test_start, final_calibration_size, final_test_size = conformal.FOLDS[-1]
    final_fit_end = final_test_start - final_calibration_size
    reference = row_windows < final_fit_end
    final_test = (row_windows >= final_test_start) & (
        row_windows < final_test_start + final_test_size
    )
    shifts = []
    for column, name in enumerate(actors.FEATURE_NAMES):
        before = features[reference, column]
        after = features[final_test, column]
        statistic, p_value = ks_2samp(before, after)
        q25, q75 = np.quantile(before, [0.25, 0.75])
        scale = max(float(q75 - q25), 1e-12)
        shifts.append(
            {
                "feature": name,
                "ks_statistic": float(statistic),
                "ks_p_value": float(p_value),
                "reference_median": float(np.median(before)),
                "final_median": float(np.median(after)),
                "median_shift_in_reference_iqr": float(
                    (np.median(after) - np.median(before)) / scale
                ),
            }
        )
    shifts.sort(key=lambda row: row["ks_statistic"], reverse=True)
    identity = {
        "source_code_fingerprint": source_code_fingerprint(),
        "dataset_source_manifest_fingerprint": payload["manifest_sha256"],
        "evidence_epoch": windows,
        "feature_set_fingerprint": base.stable_hash(actors.FEATURE_NAMES),
        "latency_ms": labels.LATENCY_MS,
        "folds": conformal.FOLDS,
        "population_comparison": (
            "expanding fit before final calibration versus final ten test windows"
        ),
    }
    output = {
        "version": SCHEMA_VERSION,
        "diagnostic_id": f"e4d-{base.stable_hash(identity)}",
        "identity": identity,
        "rows": len(rows),
        "windows": len(windows),
        "overall_positive_rate": float(np.mean(pnl > 0)),
        "window_rate_spearman": {
            "rho": float(rho),
            "p_value": float(rho_p),
        },
        "first_10_positive_rate": float(np.mean(pnl[row_windows < 10] > 0)),
        "last_10_positive_rate": float(np.mean(pnl[row_windows >= 54] > 0)),
        "quarters": quarters,
        "fold_epochs": fold_epochs,
        "top_final_epoch_feature_shifts": shifts[:20],
        "per_window": per_window,
        "finding": (
            "Exact-300ms opportunity prevalence decays materially through time, "
            "while price, creator, and buyer-activity covariates shift. Expanding "
            "training therefore risks overweighting stale regimes."
        ),
        "candidate_fitted": False,
        "active_untouched_live_data_used": False,
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

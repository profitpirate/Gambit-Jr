#!/usr/bin/env python3
"""Audit causal point-process burst shape at the frozen 250 ms horizon."""

from __future__ import annotations

import hashlib
import json
import math
import pickle
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from itertools import groupby, pairwise
from pathlib import Path
from typing import Any

import e4_v12_adaptive_exit_search as adaptive
import e4_v12_latency300_conformal as baseline
import e4_v12_latency300_feature_transport as transport
import e4_v12_latency300_labels as labels
import e4_v12_latency300_microstructure_transport as micro
import e4_v12_latency300_selected_residual_transport as residual
import e4_v12_online_conformal_precision as conformal
import e4_v12_profit_survival_search as base
import numpy as np
from scipy.stats import ks_2samp
from sklearn.ensemble import HistGradientBoostingClassifier

OUTPUT = Path("artifacts/e4-v12-latency300-point-process-transport.json")
CACHE = Path(".tmp-relative-price-capped-actor-holdout/latency300-point-process.npy")
CACHE_METADATA = Path(
    ".tmp-relative-price-capped-actor-holdout/latency300-point-process.json"
)
SCHEMA_VERSION = "e4-v12-latency300-point-process-transport-v1"
DIAGNOSTIC_FAMILY = "latency300-causal-point-process-transport-v12"
HORIZON_MS = 250
BIN_MS = 25
WINDOWS_PER_EPOCH = 16
GLOBAL_AUC_FLOOR = 0.52
RESIDUAL_AUC_FLOOR = 0.55
PARSE_WORKERS = 2
FEATURE_NAMES = (
    "point_log_arrival_cv_250ms",
    "point_arrival_entropy_25ms",
    "point_arrival_hhi_25ms",
    "point_log_arrival_fano_25ms",
    "point_late_share_50ms",
    "point_log_late_early_ratio",
    "point_log_buy_arrival_cv",
    "point_log_sell_arrival_cv",
    "point_sign_transition_rate",
    "point_max_same_sign_run_share",
    "point_repeat_trader_share",
    "point_trader_hhi",
    "point_first_last_span_share",
    "point_buy_late_share_50ms",
    "point_sell_late_share_50ms",
    "point_log_late_early_bin_ratio",
    "point_arrival_burstiness",
)
SOURCE_FILES = (
    "scripts/e4_v12_latency300_point_process_transport.py",
    "scripts/e4_v12_latency300_microstructure_transport.py",
    "scripts/e4_v12_latency300_selected_residual_transport.py",
    "scripts/e4_v12_latency300_conformal.py",
    "scripts/e4_v12_latency300_labels.py",
    "scripts/e4_v12_online_conformal_precision.py",
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


def log_cv(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    array = np.asarray(values, dtype=np.float64)
    mean = float(np.mean(array))
    if mean <= 0:
        return 0.0
    return math.log1p(float(np.std(array)) / mean)


def point_process_features(trace: base.Trace) -> tuple[float, ...] | None:
    decision_ns = trace.create_ns + HORIZON_MS * 1_000_000
    observed = [point for point in trace.points if point.timestamp_ns <= decision_ns]
    reserve = base.latest_point(observed, decision_ns)
    if reserve is None or reserve.complete:
        return None
    trades = sorted(
        (
            point
            for point in micro.public_trades(trace)
            if point.timestamp_ns >= trace.create_ns
        ),
        key=lambda point: (point.timestamp_ns, point.signature),
    )
    ages = [
        min(HORIZON_MS, max(0.0, (point.timestamp_ns - trace.create_ns) / 1_000_000))
        for point in trades
    ]
    intervals = [right - left for left, right in pairwise(ages)]
    bins = np.zeros(HORIZON_MS // BIN_MS, dtype=np.float64)
    for age in ages:
        bins[min(int(age // BIN_MS), len(bins) - 1)] += 1
    count = len(trades)
    shares = bins / count if count else bins
    nonzero = shares[shares > 0]
    entropy = (
        float(-np.sum(nonzero * np.log(nonzero)) / math.log(len(bins)))
        if len(nonzero)
        else 0.0
    )
    hhi = float(np.sum(shares * shares)) if count else 0.0
    mean_bin = float(np.mean(bins))
    fano = float(np.var(bins)) / mean_bin if mean_bin > 0 else 0.0
    late = sum(age >= 200 for age in ages)
    early = sum(age < 50 for age in ages)
    buy_ages = [
        age
        for point, age in zip(trades, ages, strict=True)
        if point.kind in base.choice_sets.BUY_KINDS
    ]
    sell_ages = [
        age
        for point, age in zip(trades, ages, strict=True)
        if point.kind in base.choice_sets.SELL_KINDS
    ]
    buy_intervals = [right - left for left, right in pairwise(buy_ages)]
    sell_intervals = [right - left for left, right in pairwise(sell_ages)]
    signs = [
        1 if point.kind in base.choice_sets.BUY_KINDS else -1 for point in trades
    ]
    transitions = sum(left != right for left, right in pairwise(signs))
    max_run = max((sum(1 for _ in group) for _, group in groupby(signs)), default=0)
    traders = Counter(point.trader for point in trades)
    trader_hhi = (
        sum((trades_for_actor / count) ** 2 for trades_for_actor in traders.values())
        if count
        else 0.0
    )
    span = (ages[-1] - ages[0]) / HORIZON_MS if len(ages) >= 2 else 0.0
    buy_late = sum(age >= 200 for age in buy_ages)
    sell_late = sum(age >= 200 for age in sell_ages)
    interval_array = np.asarray(intervals, dtype=np.float64)
    interval_mean = float(np.mean(interval_array)) if len(interval_array) else 0.0
    interval_std = float(np.std(interval_array)) if len(interval_array) else 0.0
    burstiness = (
        (interval_std - interval_mean) / (interval_std + interval_mean)
        if interval_std + interval_mean > 0
        else 0.0
    )
    values = (
        log_cv(intervals),
        entropy,
        hhi,
        math.log1p(fano),
        late / max(count, 1),
        math.log((late + 1) / (early + 1)),
        log_cv(buy_intervals),
        log_cv(sell_intervals),
        transitions / max(count - 1, 1),
        max_run / max(count, 1),
        1.0 - len(traders) / max(count, 1),
        trader_hhi,
        span,
        buy_late / max(len(buy_ages), 1),
        sell_late / max(len(sell_ages), 1),
        math.log((float(np.sum(bins[-2:])) + 1) / (float(np.sum(bins[:2])) + 1)),
        burstiness,
    )
    if len(values) != len(FEATURE_NAMES) or not all(math.isfinite(v) for v in values):
        raise ValueError(f"invalid point-process feature vector for {trace.mint}")
    return values


def parse_source_spec(
    spec: base.CaptureSpec, row_lookup: dict[str, int]
) -> dict[str, Any]:
    actual_sha256 = base.sha256_path(spec.events_path)
    if actual_sha256 != spec.expected_sha256:
        raise ValueError(f"capture hash changed: {spec.run_id}")
    traces, parse_errors = base.load_capture(spec, Counter())
    if parse_errors:
        raise ValueError(f"capture {spec.run_id} has {parse_errors} parse errors")
    values = []
    for trace in traces:
        index = row_lookup.get(trace.mint)
        if index is None:
            continue
        feature_values = point_process_features(trace)
        if feature_values is not None:
            values.append((index, feature_values))
    return {
        "run_id": spec.run_id,
        "sha256": actual_sha256,
        "launches": len(traces),
        "rows": len(values),
        "values": values,
    }


def build_matrix(root: Path, payload: dict[str, Any]) -> tuple[np.ndarray, dict[str, Any]]:
    rows = payload["rows"]
    row_lookup: dict[str, dict[str, int]] = {}
    for index, row in enumerate(rows):
        row_lookup.setdefault(str(row.run_id), {})[str(row.mint)] = index
    matrix = np.full((len(rows), len(FEATURE_NAMES)), np.nan, dtype=np.float64)
    visited = np.zeros(len(rows), dtype=bool)
    specs = adaptive.capture_specs(root, include_holdout=True)
    source_runs = {spec.run_id for spec in specs}
    audits = []
    with ProcessPoolExecutor(max_workers=PARSE_WORKERS) as executor:
        futures = {
            executor.submit(parse_source_spec, spec, row_lookup[spec.run_id]): spec.run_id
            for spec in specs
        }
        for number, future in enumerate(as_completed(futures), 1):
            audit = future.result()
            for index, values in audit.pop("values"):
                if visited[index]:
                    raise ValueError(f"duplicate point-process row index {index}")
                matrix[index] = values
                visited[index] = True
            audits.append(audit)
            print(
                f"parsed raw capture {number}/{len(specs)} run={audit['run_id']} "
                f"visited={int(visited.sum())}",
                flush=True,
            )
    cached_traces = payload.get("traces")
    if not isinstance(cached_traces, dict):
        raise TypeError("combined cache does not contain later traces")
    cached_counts: Counter[str] = Counter()
    for trace in cached_traces.values():
        run_id = str(trace.run_id)
        if run_id in source_runs:
            continue
        index = row_lookup.get(run_id, {}).get(str(trace.mint))
        if index is None:
            continue
        values = point_process_features(trace)
        if values is None:
            continue
        if visited[index]:
            raise ValueError(f"duplicate cached point-process row index {index}")
        matrix[index] = values
        visited[index] = True
        cached_counts[run_id] += 1
    if not bool(np.all(visited)):
        missing = np.flatnonzero(~visited)
        examples = [(str(rows[i].run_id), str(rows[i].mint)) for i in missing[:10]]
        raise ValueError(f"{len(missing)} rows lack point-process features: {examples}")
    if not bool(np.all(np.isfinite(matrix))):
        raise ValueError("point-process matrix contains non-finite values")
    micro.atomic_npy(CACHE, matrix)
    audit = {
        "version": "e4-v12-latency300-point-process-cache-v1",
        "dataset_manifest_sha256": payload["manifest_sha256"],
        "rows": len(rows),
        "features": len(FEATURE_NAMES),
        "feature_names": list(FEATURE_NAMES),
        "matrix_sha256": base.sha256_path(CACHE),
        "raw_capture_runs": len(specs),
        "raw_capture_bytes": sum(spec.events_path.stat().st_size for spec in specs),
        "raw_source_hashes_verified": True,
        "raw_run_audits": sorted(audits, key=lambda row: int(row["run_id"])),
        "cached_later_runs": [
            {"run_id": run_id, "rows": cached_counts[run_id]}
            for run_id in sorted(cached_counts, key=int)
        ],
        "cached_later_rows": sum(cached_counts.values()),
        "active_untouched_live_data_used": False,
        "future_values_in_features": False,
        "production_paths_changed": 0,
    }
    base.write_json(CACHE_METADATA, audit)
    return matrix, audit


def load_or_build_matrix(
    root: Path, payload: dict[str, Any]
) -> tuple[np.ndarray, dict[str, Any]]:
    if CACHE.is_file() and CACHE_METADATA.is_file():
        metadata = json.loads(CACHE_METADATA.read_text(encoding="utf-8"))
        matrix = np.load(CACHE, allow_pickle=False)
        if (
            metadata.get("dataset_manifest_sha256") == payload["manifest_sha256"]
            and metadata.get("matrix_sha256") == base.sha256_path(CACHE)
            and matrix.shape == (len(payload["rows"]), len(FEATURE_NAMES))
            and bool(np.all(np.isfinite(matrix)))
            and metadata.get("active_untouched_live_data_used") is False
        ):
            return matrix, metadata
    return build_matrix(root, payload)


def selected_indexes(
    rows: list[Any], base_features: np.ndarray, pnl: np.ndarray, row_windows: np.ndarray
) -> tuple[list[np.ndarray], list[np.ndarray], list[dict[str, Any]]]:
    decisions = np.asarray([row.decision_ns for row in rows], dtype=np.int64)
    windows = sorted({row.run_id for row in rows}, key=int)
    selected_by_fold = []
    outcomes_by_fold = []
    audits = []
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
        model.fit(base_features[fit], pnl[fit] > 0)
        calibration_scores = model.predict_proba(base_features[calibration])[:, 1]
        test_scores = model.predict_proba(base_features[test])[:, 1]
        mask, thresholds = conformal.rolling_selection(calibration_scores, test_scores)
        replay = conformal.replay(rows, test[mask], pnl, test_scores[mask])
        indexes = residual.ledger_indexes(rows, replay["ledger"])
        outcomes = np.asarray(
            [trade["pnl_sol"] > 0 for trade in replay["ledger"]], dtype=np.bool_
        )
        selected_by_fold.append(indexes)
        outcomes_by_fold.append(outcomes)
        audits.append(
            {
                "fold": fold_number,
                "fit_windows": windows[:fit_end],
                "calibration_windows": windows[fit_end:test_start],
                "test_windows": windows[test_start : test_start + test_size],
                "attempted_trades": replay["attempted_trades"],
                "executed_trades": len(indexes),
                "wins": int(np.sum(outcomes)),
                "losses": int(np.sum(~outcomes)),
                "threshold_hash": base.stable_hash(thresholds),
                "ledger_hash": replay["ledger_hash"],
            }
        )
    return selected_by_fold, outcomes_by_fold, audits


def main() -> dict[str, Any]:
    root = Path.cwd()
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
    matrix, cache_audit = load_or_build_matrix(root, payload)
    base_features = np.asarray([row.features for row in rows], dtype=np.float32)
    pnl = np.load(labels.PNL_PATH, allow_pickle=False)
    windows = sorted({row.run_id for row in rows}, key=int)
    if len(windows) != 4 * WINDOWS_PER_EPOCH:
        raise ValueError("diagnostic requires four 16-window epochs")
    window_index = {run_id: index for index, run_id in enumerate(windows)}
    row_windows = np.asarray(
        [window_index[row.run_id] for row in rows], dtype=np.int16
    )
    epoch_masks = [
        (row_windows >= start) & (row_windows < start + WINDOWS_PER_EPOCH)
        for start in range(0, len(windows), WINDOWS_PER_EPOCH)
    ]
    selected_by_fold, outcomes_by_fold, selection_audit = selected_indexes(
        rows, base_features, pnl, row_windows
    )
    records = []
    for column, name in enumerate(FEATURE_NAMES):
        global_raw = [
            transport.auc_or_chance(pnl[mask] > 0, matrix[mask, column])
            for mask in epoch_masks
        ]
        global_direction = 1 if global_raw[0] >= 0.5 else -1
        global_oriented = [
            auc if global_direction == 1 else 1.0 - auc for auc in global_raw
        ]
        residual_raw = [
            residual.auc_or_chance(outcomes, matrix[indexes, column])
            for indexes, outcomes in zip(
                selected_by_fold, outcomes_by_fold, strict=True
            )
        ]
        residual_direction = 1 if residual_raw[0] >= 0.5 else -1
        residual_oriented = [
            auc if residual_direction == 1 else 1.0 - auc for auc in residual_raw
        ]
        statistic, pvalue = ks_2samp(
            matrix[epoch_masks[0], column], matrix[epoch_masks[-1], column]
        )
        records.append(
            {
                "feature": name,
                "global_epoch0_direction": (
                    "higher_is_positive" if global_direction == 1 else "lower_is_positive"
                ),
                "global_raw_auc_by_epoch": global_raw,
                "global_oriented_auc_by_epoch": global_oriented,
                "global_all_epochs_above_floor": all(
                    auc >= GLOBAL_AUC_FLOOR for auc in global_oriented
                ),
                "residual_fold0_direction": (
                    "higher_is_win" if residual_direction == 1 else "lower_is_win"
                ),
                "residual_raw_auc_by_fold": residual_raw,
                "residual_oriented_auc_by_fold": residual_oriented,
                "residual_all_folds_above_floor": all(
                    auc >= RESIDUAL_AUC_FLOOR for auc in residual_oriented
                ),
                "minimum_later_global_auc": min(global_oriented[1:]),
                "minimum_later_residual_auc": min(residual_oriented[1:]),
                "first_to_last_ks_statistic": float(statistic),
                "first_to_last_ks_pvalue": float(pvalue),
            }
        )
    records.sort(
        key=lambda row: (
            row["global_all_epochs_above_floor"]
            and row["residual_all_folds_above_floor"],
            row["minimum_later_residual_auc"],
            row["minimum_later_global_auc"],
        ),
        reverse=True,
    )
    global_stable = [
        row["feature"] for row in records if row["global_all_epochs_above_floor"]
    ]
    residual_stable = [
        row["feature"] for row in records if row["residual_all_folds_above_floor"]
    ]
    jointly_stable = sorted(set(global_stable) & set(residual_stable))
    identity = {
        "diagnostic_family_identifier": DIAGNOSTIC_FAMILY,
        "source_code_fingerprint": source_code_fingerprint(),
        "dataset_source_manifest_fingerprint": payload["manifest_sha256"],
        "evidence_epoch": windows,
        "baseline_experiment_id": json.loads(
            baseline.OUTPUT.read_text(encoding="utf-8")
        )["experiment_id"],
        "feature_set_fingerprint": base.stable_hash(FEATURE_NAMES),
        "point_process_matrix_sha256": cache_audit["matrix_sha256"],
        "label_matrix_sha256": metadata["pnl_sha256"],
        "causal_horizon_ms": HORIZON_MS,
        "bin_ms": BIN_MS,
        "latency_ms": labels.LATENCY_MS,
        "global_auc_floor": GLOBAL_AUC_FLOOR,
        "residual_auc_floor": RESIDUAL_AUC_FLOOR,
        "orientation_policy": "orient once on epoch/fold 0 and freeze",
    }
    warranted = bool(jointly_stable)
    output = {
        "version": SCHEMA_VERSION,
        "diagnostic_id": f"e4d-{base.stable_hash(identity)}",
        "identity": identity,
        "scientific_basis": (
            "Arrival-time dispersion, burst concentration, sign persistence, and "
            "repeat-participant intensity are causal point-process geometry not "
            "represented by the retired raw count/flow microstructure subset."
        ),
        "label_audit": metadata,
        "point_process_cache_audit": cache_audit,
        "selection_audit": {
            "folds": selection_audit,
            "executed_trades": sum(len(indexes) for indexes in selected_by_fold),
            "wins": sum(int(np.sum(outcomes)) for outcomes in outcomes_by_fold),
            "losses": sum(int(np.sum(~outcomes)) for outcomes in outcomes_by_fold),
            "exact_baseline_replayed": True,
            "concurrency_rejections_preserved": True,
            "winner_definition": "fee-net replay ledger pnl_sol > 0",
        },
        "features": records,
        "feature_count": len(records),
        "global_stable_features": global_stable,
        "residual_stable_features": residual_stable,
        "jointly_stable_features": jointly_stable,
        "point_process_candidate_warranted": warranted,
        "candidate_fitted": False,
        "finding": (
            f"{len(jointly_stable)} of {len(records)} point-process features met "
            "both preregistered global and selected-residual transport floors."
        ),
        "retired_diagnostic_family": not warranted,
        "failure_classification": None if warranted else "VALIDATION_COLLAPSE",
        "material_change_required_before_rerun": (
            "One preregistered candidate using only the frozen jointly stable signals."
            if warranted
            else "A genuinely new causal signal or model family; point-process feature, bin-width, threshold, and parameter variants are prohibited."
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

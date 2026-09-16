#!/usr/bin/env python3
"""Build and audit causal sub-250 ms price-path geometry features."""

from __future__ import annotations

import json
import math
import os
import pickle
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import e4_v12_adaptive_exit_search as adaptive
import e4_v12_latency300_feature_transport as transport
import e4_v12_latency300_labels as labels
import e4_v12_latency300_microstructure_transport as micro
import e4_v12_online_conformal_precision as conformal
import e4_v12_profit_survival_search as base
import numpy as np
from scipy.stats import ks_2samp

OUTPUT = Path("artifacts/e4-v12-latency300-price-path-transport.json")
CACHE = Path(".tmp-relative-price-capped-actor-holdout/latency300-price-path.npy")
CACHE_METADATA = Path(
    ".tmp-relative-price-capped-actor-holdout/latency300-price-path.json"
)
SCHEMA_VERSION = "e4-v12-latency300-price-path-transport-v1"
DIAGNOSTIC_FAMILY = "latency300-causal-price-path-geometry-v12"
HORIZON_MS = 250
WINDOWS_PER_EPOCH = 16
DESCRIPTIVE_AUC_FLOOR = 0.52
PRICE_RANGE_TAIL_FRACTION = 0.05
PARSE_WORKERS = 2
PRICE_PATH_FEATURE_NAMES = (
    "path_log_total_variation_250ms",
    "path_directional_efficiency_250ms",
    "path_terminal_peak_drawdown_250ms",
    "path_max_drawdown_250ms",
    "path_close_location_250ms",
    "path_up_step_fraction_250ms",
    "path_new_high_fraction_250ms",
    "path_peak_time_fraction_250ms",
    "path_time_since_peak_ms",
    "path_max_runup_250ms",
    "path_worst_log_step_250ms",
    "path_best_log_step_250ms",
    "path_log_step_skew_250ms",
    "path_late_minus_early_return_250ms",
    "path_linear_trend_r2_250ms",
    "path_log_residual_std_250ms",
)
SOURCE_FILES = (
    "scripts/e4_v12_latency300_price_path_transport.py",
    "scripts/e4_v12_latency300_feature_transport.py",
    "scripts/e4_v12_latency300_microstructure_transport.py",
    "scripts/e4_v12_latency300_labels.py",
    "scripts/e4_v12_online_conformal_precision.py",
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


def atomic_npy(path: Path, values: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            np.save(handle, values, allow_pickle=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def price_path_features(trace: base.Trace) -> tuple[float, ...] | None:
    decision_ns = trace.create_ns + HORIZON_MS * 1_000_000
    observed = sorted(
        (
            point
            for point in trace.points
            if point.timestamp_ns <= decision_ns and point.price_sol > 0
        ),
        key=lambda point: (point.timestamp_ns, point.slot, point.signature),
    )
    reserve = base.latest_point(trace.points, decision_ns)
    if not observed or reserve is None or reserve.complete:
        return None
    log_prices = np.log(
        np.asarray([point.price_sol for point in observed], dtype=np.float64)
    )
    times = np.asarray(
        [
            min(max((point.timestamp_ns - trace.create_ns) / 1_000_000, 0.0), 250.0)
            / 250.0
            for point in observed
        ],
        dtype=np.float64,
    )
    steps = np.diff(log_prices)
    total_variation = float(np.sum(np.abs(steps)))
    net_return = float(log_prices[-1] - log_prices[0])
    directional_efficiency = abs(net_return) / max(total_variation, 1e-12)
    running_peak = np.maximum.accumulate(log_prices)
    running_trough = np.minimum.accumulate(log_prices)
    drawdowns = running_peak - log_prices
    runups = log_prices - running_trough
    peak_index = int(np.argmax(log_prices))
    price_range = float(np.max(log_prices) - np.min(log_prices))
    close_location = (
        float((log_prices[-1] - np.min(log_prices)) / price_range)
        if price_range > 0
        else 0.5
    )
    up_step_fraction = float(np.mean(steps > 0)) if len(steps) else 0.0
    new_highs = 0
    prior_peak = float(log_prices[0])
    for price in log_prices[1:]:
        if price > prior_peak:
            new_highs += 1
            prior_peak = float(price)
    new_high_fraction = new_highs / max(len(steps), 1)
    step_std = float(np.std(steps)) if len(steps) else 0.0
    step_mean = float(np.mean(steps)) if len(steps) else 0.0
    step_skew = (
        float(np.mean(((steps - step_mean) / step_std) ** 3))
        if step_std > 0
        else 0.0
    )
    midpoint = trace.create_ns + 125 * 1_000_000
    prior_midpoint = [
        point for point in observed if point.timestamp_ns <= midpoint
    ]
    midpoint_log_price = math.log(
        prior_midpoint[-1].price_sol if prior_midpoint else observed[0].price_sol
    )
    early_return = midpoint_log_price - float(log_prices[0])
    late_return = float(log_prices[-1]) - midpoint_log_price
    if len(log_prices) > 1 and float(np.ptp(times)) > 0:
        design = np.column_stack((np.ones(len(times)), times))
        coefficients, *_ = np.linalg.lstsq(design, log_prices, rcond=None)
        fitted = design @ coefficients
        residuals = log_prices - fitted
        total_sum_squares = float(np.sum((log_prices - np.mean(log_prices)) ** 2))
        residual_sum_squares = float(np.sum(residuals**2))
        trend_r2 = (
            max(0.0, 1.0 - residual_sum_squares / total_sum_squares)
            if total_sum_squares > 0
            else 0.0
        )
        residual_std = float(np.std(residuals))
    else:
        trend_r2 = 0.0
        residual_std = 0.0
    peak_time_fraction = float(times[peak_index])
    values = (
        math.log1p(total_variation),
        directional_efficiency,
        float(np.max(log_prices) - log_prices[-1]),
        float(np.max(drawdowns)),
        close_location,
        up_step_fraction,
        new_high_fraction,
        peak_time_fraction,
        (1.0 - peak_time_fraction) * HORIZON_MS,
        float(np.max(runups)),
        float(np.min(steps)) if len(steps) else 0.0,
        float(np.max(steps)) if len(steps) else 0.0,
        step_skew,
        late_return - early_return,
        trend_r2,
        residual_std,
    )
    if len(values) != len(PRICE_PATH_FEATURE_NAMES) or not all(
        math.isfinite(value) for value in values
    ):
        raise ValueError(f"invalid price-path feature vector for {trace.mint}")
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
    missing_rows = 0
    invalid_rows = 0
    for trace in traces:
        index = row_lookup.get(trace.mint)
        if index is None:
            missing_rows += 1
            continue
        feature_values = price_path_features(trace)
        if feature_values is None:
            invalid_rows += 1
            continue
        values.append((index, feature_values))
    return {
        "run_id": spec.run_id,
        "sha256": actual_sha256,
        "launches": len(traces),
        "rows": len(values),
        "missing_rows": missing_rows,
        "invalid_rows": invalid_rows,
        "values": values,
    }


def build_matrix(root: Path, payload: dict[str, Any]) -> tuple[np.ndarray, dict[str, Any]]:
    rows = payload["rows"]
    row_lookup: dict[str, dict[str, int]] = {}
    for index, row in enumerate(rows):
        row_lookup.setdefault(str(row.run_id), {})[str(row.mint)] = index
    matrix = np.full(
        (len(rows), len(PRICE_PATH_FEATURE_NAMES)), np.nan, dtype=np.float64
    )
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
                    raise ValueError(f"duplicate price-path row index {index}")
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
        feature_values = price_path_features(trace)
        if feature_values is None:
            continue
        if visited[index]:
            raise ValueError(f"duplicate cached price-path row index {index}")
        matrix[index] = feature_values
        visited[index] = True
        cached_counts[run_id] += 1
    if not bool(np.all(visited)):
        missing = np.flatnonzero(~visited)
        examples = [
            (str(rows[index].run_id), str(rows[index].mint)) for index in missing[:10]
        ]
        raise ValueError(f"{len(missing)} rows lack price-path features: {examples}")
    if not bool(np.all(np.isfinite(matrix))):
        raise ValueError("price-path matrix contains non-finite values")
    atomic_npy(CACHE, matrix)
    cache_audit = {
        "version": "e4-v12-latency300-price-path-cache-v1",
        "dataset_manifest_sha256": payload["manifest_sha256"],
        "rows": len(rows),
        "features": len(PRICE_PATH_FEATURE_NAMES),
        "feature_names": list(PRICE_PATH_FEATURE_NAMES),
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
    base.write_json(CACHE_METADATA, cache_audit)
    return matrix, cache_audit


def load_or_build_matrix(
    root: Path, payload: dict[str, Any]
) -> tuple[np.ndarray, dict[str, Any]]:
    if CACHE.is_file() and CACHE_METADATA.is_file():
        metadata = json.loads(CACHE_METADATA.read_text(encoding="utf-8"))
        matrix = np.load(CACHE, allow_pickle=False)
        if (
            metadata.get("dataset_manifest_sha256") == payload["manifest_sha256"]
            and metadata.get("matrix_sha256") == base.sha256_path(CACHE)
            and matrix.shape == (len(payload["rows"]), len(PRICE_PATH_FEATURE_NAMES))
            and bool(np.all(np.isfinite(matrix)))
            and metadata.get("active_untouched_live_data_used") is False
        ):
            return matrix, metadata
    return build_matrix(root, payload)


def main() -> dict[str, Any]:
    root = Path.cwd()
    label_metadata = json.loads(labels.METADATA_PATH.read_text(encoding="utf-8"))
    with conformal.CACHE.open("rb") as handle:
        payload = pickle.load(handle)
    if label_metadata["dataset_manifest_sha256"] != payload["manifest_sha256"]:
        raise ValueError("300 ms labels target another evidence manifest")
    if label_metadata["pnl_sha256"] != base.sha256_path(labels.PNL_PATH):
        raise ValueError("300 ms label matrix fingerprint changed")
    if label_metadata["all_source_hashes_verified"] is not True:
        raise ValueError("label source hashes were not verified")
    if label_metadata["active_untouched_live_data_used"] is not False:
        raise ValueError("active untouched live evidence was consumed")
    rows = payload["rows"]
    matrix, cache_audit = load_or_build_matrix(root, payload)
    micro_matrix, micro_audit = micro.load_or_build_matrix(root, payload)
    if micro_audit["active_untouched_live_data_used"] is not False:
        raise ValueError("microstructure matrix consumed active live evidence")
    pnl = np.load(labels.PNL_PATH, allow_pickle=False)
    profitable = pnl > 0
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
    price_range = micro_matrix[
        :, micro.MICRO_FEATURE_NAMES.index("micro_log_price_range_250ms")
    ]
    tail_masks = []
    tail_audit = []
    for epoch, mask in enumerate(epoch_masks):
        threshold = float(
            np.quantile(price_range[mask], 1.0 - PRICE_RANGE_TAIL_FRACTION)
        )
        tail = mask & (price_range >= threshold)
        tail_masks.append(tail)
        tail_audit.append(
            {
                "epoch": epoch,
                "price_range_threshold": threshold,
                "rows": int(np.sum(tail)),
                "profitable_rows": int(np.sum(profitable[tail])),
                "positive_rate": float(np.mean(profitable[tail])),
            }
        )
    records = []
    for column, name in enumerate(PRICE_PATH_FEATURE_NAMES):
        raw_aucs = [
            transport.auc_or_chance(profitable[mask], matrix[mask, column])
            for mask in epoch_masks
        ]
        direction = 1 if raw_aucs[0] >= 0.5 else -1
        oriented = [auc if direction == 1 else 1.0 - auc for auc in raw_aucs]
        tail_raw_aucs = [
            transport.auc_or_chance(profitable[mask], matrix[mask, column])
            for mask in tail_masks
        ]
        tail_direction = 1 if tail_raw_aucs[0] >= 0.5 else -1
        tail_oriented = [
            auc if tail_direction == 1 else 1.0 - auc for auc in tail_raw_aucs
        ]
        statistic, pvalue = ks_2samp(
            matrix[epoch_masks[0], column], matrix[epoch_masks[-1], column]
        )
        records.append(
            {
                "feature": name,
                "earliest_epoch_direction": (
                    "higher_is_positive" if direction == 1 else "lower_is_positive"
                ),
                "raw_auc_by_epoch": raw_aucs,
                "oriented_auc_by_epoch": oriented,
                "minimum_later_oriented_auc": min(oriented[1:]),
                "mean_later_oriented_auc": float(np.mean(oriented[1:])),
                "direction_consistent_all_epochs": all(auc >= 0.5 for auc in oriented),
                "all_epochs_above_descriptive_floor": all(
                    auc >= DESCRIPTIVE_AUC_FLOOR for auc in oriented
                ),
                "first_to_last_oriented_auc_change": oriented[-1] - oriented[0],
                "first_to_last_ks_statistic": float(statistic),
                "first_to_last_ks_pvalue": float(pvalue),
                "price_range_tail_earliest_direction": (
                    "higher_is_positive"
                    if tail_direction == 1
                    else "lower_is_positive"
                ),
                "price_range_tail_raw_auc_by_epoch": tail_raw_aucs,
                "price_range_tail_oriented_auc_by_epoch": tail_oriented,
                "price_range_tail_minimum_later_oriented_auc": min(
                    tail_oriented[1:]
                ),
                "price_range_tail_direction_consistent_all_epochs": all(
                    auc >= 0.5 for auc in tail_oriented
                ),
                "price_range_tail_all_epochs_above_descriptive_floor": all(
                    auc >= DESCRIPTIVE_AUC_FLOOR for auc in tail_oriented
                ),
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
        row["feature"] for row in records if row["all_epochs_above_descriptive_floor"]
    ]
    stable_tail = [
        row["feature"]
        for row in records
        if row["price_range_tail_all_epochs_above_descriptive_floor"]
    ]
    identity = {
        "diagnostic_family_identifier": DIAGNOSTIC_FAMILY,
        "source_code_fingerprint": source_code_fingerprint(),
        "dataset_source_manifest_fingerprint": payload["manifest_sha256"],
        "evidence_epoch": windows,
        "feature_set_fingerprint": base.stable_hash(PRICE_PATH_FEATURE_NAMES),
        "price_path_matrix_sha256": cache_audit["matrix_sha256"],
        "microstructure_matrix_sha256": micro_audit["matrix_sha256"],
        "label_matrix_sha256": label_metadata["pnl_sha256"],
        "causal_horizon_ms": HORIZON_MS,
        "latency_ms": labels.LATENCY_MS,
        "orientation_policy": (
            "orient each new feature once from epoch 0, then freeze direction "
            "for epochs 1-3"
        ),
        "descriptive_auc_floor": DESCRIPTIVE_AUC_FLOOR,
        "price_range_tail_fraction": PRICE_RANGE_TAIL_FRACTION,
        "price_range_tail_policy": (
            "within each chronological diagnostic epoch, audit the fixed top-five-"
            "percent price-range stratum; descriptive incrementality only"
        ),
    }
    output = {
        "version": SCHEMA_VERSION,
        "diagnostic_id": f"e4d-{base.stable_hash(identity)}",
        "identity": identity,
        "label_audit": label_metadata,
        "price_path_cache_audit": cache_audit,
        "features": records,
        "feature_count": len(records),
        "direction_consistent_features": sum(
            row["direction_consistent_all_epochs"] for row in records
        ),
        "all_epoch_auc_52_features": stable,
        "all_epoch_auc_52_feature_count": len(stable),
        "price_range_tail_audit": tail_audit,
        "price_range_tail_all_epoch_auc_52_features": stable_tail,
        "price_range_tail_all_epoch_auc_52_feature_count": len(stable_tail),
        "finding": (
            f"{len(stable)} of {len(records)} new causal sub-250ms price-path "
            "features preserved epoch-0 direction with oriented AUC >=0.52 in "
            "all four epochs. This is a transport diagnostic, not a fitted "
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

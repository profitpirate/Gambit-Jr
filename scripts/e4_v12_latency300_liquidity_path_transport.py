#!/usr/bin/env python3
"""Build and audit causal sub-250 ms bonding-curve liquidity-path features."""

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

OUTPUT = Path("artifacts/e4-v12-latency300-liquidity-path-transport.json")
CACHE = Path(".tmp-relative-price-capped-actor-holdout/latency300-liquidity-path.npy")
CACHE_METADATA = Path(
    ".tmp-relative-price-capped-actor-holdout/latency300-liquidity-path.json"
)
SCHEMA_VERSION = "e4-v12-latency300-liquidity-path-transport-v1"
DIAGNOSTIC_FAMILY = "latency300-causal-liquidity-absorption-path-v12"
HORIZON_MS = 250
WINDOWS_PER_EPOCH = 16
DESCRIPTIVE_AUC_FLOOR = 0.52
PRICE_RANGE_TAIL_FRACTION = 0.05
PARSE_WORKERS = 2
LIQUIDITY_FEATURE_NAMES = (
    "liquidity_log_virtual_sol_growth_250ms",
    "liquidity_log_virtual_sol_variation_250ms",
    "liquidity_virtual_sol_efficiency_250ms",
    "liquidity_terminal_reserve_drawdown_250ms",
    "liquidity_max_reserve_drawdown_250ms",
    "liquidity_late_minus_early_sol_growth_250ms",
    "liquidity_last50_sol_growth_250ms",
    "liquidity_log_virtual_token_depletion_250ms",
    "liquidity_real_token_depletion_fraction_250ms",
    "liquidity_real_token_remaining_fraction_250ms",
    "liquidity_price_reserve_elasticity_250ms",
    "liquidity_reserve_delta_per_public_sol_250ms",
    "liquidity_public_sol_to_final_reserve_250ms",
    "liquidity_log_virtual_to_real_tokens_250ms",
    "liquidity_time_to_half_reserve_growth_ms",
    "liquidity_reserve_step_cv_250ms",
)
INCREMENTAL_FEATURE_NAMES = (
    "liquidity_price_reserve_elasticity_250ms",
    "liquidity_public_sol_to_final_reserve_250ms",
    "liquidity_reserve_step_cv_250ms",
    "liquidity_reserve_delta_per_public_sol_250ms",
    "liquidity_time_to_half_reserve_growth_ms",
)
SOURCE_FILES = (
    "scripts/e4_v12_latency300_liquidity_path_transport.py",
    "scripts/e4_v12_latency300_microstructure_transport.py",
    "scripts/e4_v12_latency300_feature_transport.py",
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


def liquidity_path_features(trace: base.Trace) -> tuple[float, ...] | None:
    decision_ns = trace.create_ns + HORIZON_MS * 1_000_000
    points = sorted(
        (
            point
            for point in trace.points
            if point.timestamp_ns <= decision_ns
            and point.price_sol > 0
            and point.virtual_sol > 0
            and point.virtual_tokens > 0
        ),
        key=lambda point: (point.timestamp_ns, point.slot, point.signature),
    )
    reserve = base.latest_point(trace.points, decision_ns)
    if not points or reserve is None or reserve.complete:
        return None
    virtual_sol = np.asarray([point.virtual_sol for point in points], dtype=np.float64)
    virtual_tokens = np.asarray(
        [point.virtual_tokens for point in points], dtype=np.float64
    )
    real_tokens = np.asarray(
        [max(point.real_tokens, 0.0) for point in points], dtype=np.float64
    )
    log_prices = np.log(
        np.asarray([point.price_sol for point in points], dtype=np.float64)
    )
    sol_steps = np.diff(virtual_sol)
    total_variation = float(np.sum(np.abs(sol_steps)))
    net_sol = float(virtual_sol[-1] - virtual_sol[0])
    efficiency = abs(net_sol) / max(total_variation, 1e-12)
    running_peak = np.maximum.accumulate(virtual_sol)
    drawdowns = running_peak - virtual_sol
    midpoint_ns = trace.create_ns + 125 * 1_000_000
    last50_ns = trace.create_ns + 200 * 1_000_000
    midpoint_points = [point for point in points if point.timestamp_ns <= midpoint_ns]
    last50_anchor = [point for point in points if point.timestamp_ns <= last50_ns]
    midpoint_sol = (
        midpoint_points[-1].virtual_sol if midpoint_points else virtual_sol[0]
    )
    last50_sol = last50_anchor[-1].virtual_sol if last50_anchor else virtual_sol[0]
    early_growth = midpoint_sol - virtual_sol[0]
    late_growth = virtual_sol[-1] - midpoint_sol
    initial_real = float(real_tokens[0])
    public_sol = sum(abs(point.sol_amount) for point in micro.public_trades(trace))
    log_virtual_growth = math.log(virtual_sol[-1] / virtual_sol[0])
    log_price_growth = float(log_prices[-1] - log_prices[0])
    price_reserve_elasticity = math.atan2(log_price_growth, log_virtual_growth)
    half_growth_time = float(HORIZON_MS)
    if net_sol > 0:
        target = virtual_sol[0] + 0.5 * net_sol
        for point in points:
            if point.virtual_sol >= target:
                half_growth_time = min(
                    max((point.timestamp_ns - trace.create_ns) / 1_000_000, 0.0),
                    250.0,
                )
                break
    step_mean = float(np.mean(np.abs(sol_steps))) if len(sol_steps) else 0.0
    step_cv = (
        float(np.std(np.abs(sol_steps))) / step_mean if step_mean > 0 else 0.0
    )
    values = (
        log_virtual_growth,
        math.log1p(total_variation),
        efficiency,
        float(np.max(virtual_sol) - virtual_sol[-1]),
        float(np.max(drawdowns)),
        late_growth - early_growth,
        float(virtual_sol[-1] - last50_sol),
        math.log(virtual_tokens[0] / virtual_tokens[-1]),
        (initial_real - float(real_tokens[-1])) / max(initial_real, 1e-9),
        float(real_tokens[-1]) / max(initial_real, 1e-9),
        price_reserve_elasticity,
        net_sol / max(public_sol, 1e-9),
        public_sol / max(float(virtual_sol[-1]), 1e-9),
        math.log1p(float(virtual_tokens[-1]) / max(float(real_tokens[-1]), 1e-9)),
        half_growth_time,
        step_cv,
    )
    if len(values) != len(LIQUIDITY_FEATURE_NAMES) or not all(
        math.isfinite(value) for value in values
    ):
        raise ValueError(f"invalid liquidity-path feature vector for {trace.mint}")
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
        feature_values = liquidity_path_features(trace)
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
        (len(rows), len(LIQUIDITY_FEATURE_NAMES)), np.nan, dtype=np.float64
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
                    raise ValueError(f"duplicate liquidity-path row index {index}")
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
        feature_values = liquidity_path_features(trace)
        if feature_values is None:
            continue
        if visited[index]:
            raise ValueError(f"duplicate cached liquidity-path row index {index}")
        matrix[index] = feature_values
        visited[index] = True
        cached_counts[run_id] += 1
    if not bool(np.all(visited)):
        missing = np.flatnonzero(~visited)
        examples = [
            (str(rows[index].run_id), str(rows[index].mint)) for index in missing[:10]
        ]
        raise ValueError(f"{len(missing)} rows lack liquidity features: {examples}")
    if not bool(np.all(np.isfinite(matrix))):
        raise ValueError("liquidity-path matrix contains non-finite values")
    atomic_npy(CACHE, matrix)
    cache_audit = {
        "version": "e4-v12-latency300-liquidity-path-cache-v1",
        "dataset_manifest_sha256": payload["manifest_sha256"],
        "rows": len(rows),
        "features": len(LIQUIDITY_FEATURE_NAMES),
        "feature_names": list(LIQUIDITY_FEATURE_NAMES),
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
            and matrix.shape == (len(payload["rows"]), len(LIQUIDITY_FEATURE_NAMES))
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
    for column, name in enumerate(LIQUIDITY_FEATURE_NAMES):
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
    stable_incremental_tail = [
        name for name in INCREMENTAL_FEATURE_NAMES if name in stable_tail
    ]
    identity = {
        "diagnostic_family_identifier": DIAGNOSTIC_FAMILY,
        "source_code_fingerprint": source_code_fingerprint(),
        "dataset_source_manifest_fingerprint": payload["manifest_sha256"],
        "evidence_epoch": windows,
        "feature_set_fingerprint": base.stable_hash(LIQUIDITY_FEATURE_NAMES),
        "liquidity_path_matrix_sha256": cache_audit["matrix_sha256"],
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
        "liquidity_path_cache_audit": cache_audit,
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
        "incremental_feature_names": list(INCREMENTAL_FEATURE_NAMES),
        "stable_incremental_tail_features": stable_incremental_tail,
        "stable_incremental_tail_feature_count": len(stable_incremental_tail),
        "candidate_warranted": len(stable_incremental_tail) >= 3,
        "finding": (
            f"{len(stable)} of {len(records)} new causal sub-250ms liquidity-path "
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

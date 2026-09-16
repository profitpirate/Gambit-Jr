#!/usr/bin/env python3
"""Build and diagnose a causal creation-time corpus with exact 300 ms fills."""

from __future__ import annotations

import hashlib
import heapq
import json
import pickle
import statistics
from collections import Counter, deque
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Any

import e4_v12_adaptive_exit_search as adaptive
import e4_v12_causal_actor_memory as actors
import e4_v12_latency300_market_flow_transport as market
import e4_v12_online_conformal_precision as conformal
import e4_v12_profit_survival_search as base
import numpy as np
from scipy.stats import ks_2samp
from sklearn.metrics import roc_auc_score

OUTPUT = Path("artifacts/e4-v12-horizon0-latency300-transport.json")
CACHE = Path(".tmp-relative-price-capped-actor-holdout/horizon0-latency300.pkl")
SCHEMA_VERSION = "e4-v12-horizon0-latency300-transport-v1"
CACHE_VERSION = "e4-v12-horizon0-latency300-corpus-strict-create-v1"
DIAGNOSTIC_FAMILY = "horizon0-latency300-causal-transport-v12"
HORIZON_MS = 0
LATENCY_MS = 300
RESOLUTION_DELAY_MS = 60_300
WINDOWS_PER_EPOCH = 16
DESCRIPTIVE_AUC_FLOOR = 0.52
PARSE_WORKERS = 2
SOURCE_FILES = (
    "scripts/e4_v12_horizon0_latency300_transport.py",
    "scripts/e4_v12_latency300_early_horizon_frontier.py",
    "scripts/e4_v12_latency300_market_flow_transport.py",
    "scripts/e4_v12_online_conformal_precision.py",
    "scripts/e4_v12_adaptive_exit_search.py",
    "scripts/e4_v12_causal_actor_memory.py",
    "scripts/e4_v12_profit_survival_search.py",
)

_ROW_LOOKUP: set[tuple[str, str]] = set()


def sha256_lf(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def source_code_fingerprint(root: Path | None = None) -> str:
    root = Path.cwd() if root is None else root
    return base.stable_hash(
        {relative: sha256_lf(root / relative) for relative in SOURCE_FILES}
    )


def _initialize_worker(row_lookup: set[tuple[str, str]]) -> None:
    global _ROW_LOOKUP
    _ROW_LOOKUP = row_lookup


def strict_creation_snapshot(trace: base.Trace) -> tuple[float, ...] | None:
    """Use only the CREATE event, excluding later equal-timestamp events."""
    create = next(
        (
            point
            for point in trace.points
            if point.kind == "CREATE" and point.timestamp_ns == trace.create_ns
        ),
        None,
    )
    if create is None:
        return None
    return base.snapshot(replace(trace, points=[create]), HORIZON_MS)


def compact_trace(trace: base.Trace) -> tuple[Any, ...]:
    eligible = (str(trace.run_id), str(trace.mint)) in _ROW_LOOKUP
    snapshot: tuple[float, ...] | None = None
    pnl_sol: float | None = None
    if eligible:
        snapshot = strict_creation_snapshot(trace)
        outcome = adaptive.adaptive_outcome(
            trace,
            HORIZON_MS,
            adaptive.POLICIES[conformal.POLICY_INDEX],
            latency_ms=LATENCY_MS,
        )
        if snapshot is None or outcome is None:
            raise ValueError(f"missing horizon-0 evidence for {trace.run_id}/{trace.mint}")
        pnl_sol = base.net_pnl(
            outcome,
            0.001,
            base.STARTING_BANKROLL_SOL * base.POSITION_FRACTION,
        )
    return (
        str(trace.run_id),
        str(trace.mint),
        int(trace.create_ns),
        str(trace.creator),
        snapshot,
        pnl_sol,
    )


def parse_spec(spec: base.CaptureSpec) -> dict[str, Any]:
    actual_sha256 = base.sha256_path(spec.events_path)
    if actual_sha256 != spec.expected_sha256:
        raise ValueError(f"capture hash changed: {spec.run_id}")
    traces, parse_errors = base.load_capture(spec, Counter())
    if parse_errors:
        raise ValueError(f"capture {spec.run_id} has {parse_errors} parse errors")
    records = [compact_trace(trace) for trace in traces]
    return {
        "run_id": spec.run_id,
        "sha256": actual_sha256,
        "launches": len(traces),
        "eligible": sum(record[4] is not None for record in records),
        "records": records,
    }


def creator_memory(state: actors.ActorState) -> tuple[float, ...]:
    return (
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        float(state.resolved),
        state.bayesian_win_rate,
        state.average_pnl,
        state.severe_loss_rate,
        float(state.resolved > 0 and state.average_pnl > 0),
    )


def add_horizon0_market_context(
    rows: list[base.ResearchRow],
) -> list[base.ResearchRow]:
    """Append the existing 12 market-context fields to one horizon-0 stream."""
    enhanced: list[base.ResearchRow | None] = [None] * len(rows)
    order = sorted(
        range(len(rows)),
        key=lambda index: (
            rows[index].decision_ns,
            int(rows[index].run_id),
            rows[index].mint,
        ),
    )
    prior: deque[base.ResearchRow] = deque()
    cursor = 0
    while cursor < len(order):
        decision_ns = rows[order[cursor]].decision_ns
        end = cursor + 1
        while end < len(order) and rows[order[end]].decision_ns == decision_ns:
            end += 1
        lower_300s = decision_ns - 300_000_000_000
        while prior and prior[0].decision_ns < lower_300s:
            prior.popleft()
        history = list(prior)[-100:]
        recent_60s = sum(
            item.decision_ns >= decision_ns - 60_000_000_000 for item in history
        )
        feature_indices = (2, 6, 14, 22, 16)
        medians = tuple(
            statistics.median(item.features[position] for item in history)
            if history
            else 0.0
            for position in feature_indices
        )
        for position in range(cursor, end):
            index = int(order[position])
            row = rows[index]
            relatives = tuple(
                row.features[feature_index] / max(abs(median), 1e-9)
                for feature_index, median in zip(
                    feature_indices, medians, strict=True
                )
            )
            context = (float(recent_60s), float(len(history)), *medians, *relatives)
            base_part = row.features[: len(base.FEATURE_NAMES)]
            actor_part = row.features[len(base.FEATURE_NAMES) :]
            enhanced[index] = replace(
                row, features=(*base_part, *context, *actor_part)
            )
        for position in range(cursor, end):
            prior.append(rows[int(order[position])])
        cursor = end
    if any(row is None for row in enhanced):
        raise ValueError("horizon-0 market context did not cover every row")
    return [row for row in enhanced if row is not None]


def build_corpus(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    rows_250 = payload["rows"]
    row_lookup = {(str(row.run_id), str(row.mint)) for row in rows_250}
    specs = adaptive.capture_specs(root, include_holdout=True)
    source_runs = {spec.run_id for spec in specs}
    audits: list[dict[str, Any]] = []
    records: list[tuple[Any, ...]] = []
    with ProcessPoolExecutor(
        max_workers=PARSE_WORKERS,
        initializer=_initialize_worker,
        initargs=(row_lookup,),
    ) as executor:
        futures = {executor.submit(parse_spec, spec): spec.run_id for spec in specs}
        for number, future in enumerate(as_completed(futures), 1):
            result = future.result()
            records.extend(result.pop("records"))
            audits.append(result)
            print(
                f"parsed raw capture {number}/{len(specs)} run={result['run_id']} "
                f"eligible={result['eligible']}",
                flush=True,
            )
    cached_traces = payload.get("traces")
    if not isinstance(cached_traces, dict):
        raise TypeError("combined cache does not contain later traces")
    global _ROW_LOOKUP
    _ROW_LOOKUP = row_lookup
    later_by_run: dict[str, list[base.Trace]] = {}
    for trace in cached_traces.values():
        if str(trace.run_id) not in source_runs:
            later_by_run.setdefault(str(trace.run_id), []).append(trace)
    for run_id, traces in later_by_run.items():
        compact = [compact_trace(trace) for trace in traces]
        records.extend(compact)
        audits.append(
            {
                "run_id": run_id,
                "sha256": "bound-by-combined-cache-manifest",
                "launches": len(traces),
                "eligible": sum(record[4] is not None for record in compact),
            }
        )
    records.sort(key=lambda row: (row[2], int(row[0]), row[1]))
    creator_counts: Counter[str] = Counter()
    creator_states: dict[str, actors.ActorState] = {}
    pending: list[tuple[int, int, str, float]] = []
    sequence = 0
    rows: list[base.ResearchRow] = []
    pnls: list[float] = []
    cursor = 0
    while cursor < len(records):
        decision_ns = records[cursor][2]
        end = cursor + 1
        while end < len(records) and records[end][2] == decision_ns:
            end += 1
        while pending and pending[0][0] <= decision_ns:
            _, _, resolved_creator, resolved_pnl = heapq.heappop(pending)
            creator_states.setdefault(resolved_creator, actors.ActorState()).update(
                resolved_pnl
            )
        for run_id, mint, _, creator, snapshot, pnl_sol in records[cursor:end]:
            if snapshot is not None and pnl_sol is not None:
                base_snapshot = list(snapshot)
                base_snapshot[26] = float(creator_counts[creator])
                memory = creator_memory(
                    creator_states.get(creator, actors.ActorState())
                )
                rows.append(
                    base.ResearchRow(
                        run_id=run_id,
                        split="development",
                        mint=mint,
                        decision_ns=decision_ns,
                        horizon_ms=HORIZON_MS,
                        features=(*base_snapshot, *memory),
                        outcomes=(),
                    )
                )
                pnls.append(float(pnl_sol))
                sequence += 1
                heapq.heappush(
                    pending,
                    (
                        decision_ns + RESOLUTION_DELAY_MS * 1_000_000,
                        sequence,
                        creator,
                        float(pnl_sol),
                    ),
                )
        for _, _, _, creator, _, _ in records[cursor:end]:
            creator_counts[creator] += 1
        cursor = end
    rows = add_horizon0_market_context(rows)
    if len(rows) != len(rows_250) or len(pnls) != len(rows_250):
        raise ValueError(f"horizon-0 corpus covered {len(rows)}/{len(rows_250)} rows")
    audits.sort(key=lambda row: int(row["run_id"]))
    result = {
        "version": CACHE_VERSION,
        "builder_fingerprint": source_code_fingerprint(root),
        "manifest_sha256": payload["manifest_sha256"],
        "rows": rows,
        "pnl": np.asarray(pnls, dtype=np.float64),
        "audit": {
            "capture_windows": len(audits),
            "raw_capture_runs": len(specs),
            "cached_later_runs": len(later_by_run),
            "raw_capture_bytes": sum(spec.events_path.stat().st_size for spec in specs),
            "raw_source_hashes_verified": True,
            "launches": sum(row["launches"] for row in audits),
            "rows": len(rows),
            "creator_updates_delayed_ms": RESOLUTION_DELAY_MS,
            "global_creator_counts_use_all_launches": True,
            "same_timestamp_creator_counts_batched": True,
            "same_timestamp_market_context_batched": True,
            "future_values_in_features": False,
            "active_untouched_live_data_used": False,
            "production_paths_changed": 0,
            "runs": audits,
        },
    }
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with CACHE.open("wb") as handle:
        pickle.dump(result, handle, protocol=pickle.HIGHEST_PROTOCOL)
    return result


def auc_or_chance(y: np.ndarray, values: np.ndarray) -> float:
    if len(np.unique(y)) < 2 or len(np.unique(values)) < 2:
        return 0.5
    return float(roc_auc_score(y, values))


def main() -> dict[str, Any]:
    root = Path.cwd()
    with conformal.CACHE.open("rb") as handle:
        payload = pickle.load(handle)
    if CACHE.exists():
        with CACHE.open("rb") as handle:
            corpus = pickle.load(handle)
        if (
            corpus.get("version") != CACHE_VERSION
            or corpus.get("builder_fingerprint") != source_code_fingerprint(root)
        ):
            corpus = build_corpus(root, payload)
        elif corpus["manifest_sha256"] != payload["manifest_sha256"]:
            raise ValueError("horizon-0 cache targets another evidence manifest")
    else:
        corpus = build_corpus(root, payload)
    rows = corpus["rows"]
    pnl = np.asarray(corpus["pnl"], dtype=np.float64)
    base_features = np.asarray([row.features for row in rows], dtype=np.float64)
    if base_features.shape != (len(rows), len(actors.FEATURE_NAMES)):
        raise ValueError("horizon-0 causal feature matrix shape changed")
    decisions = np.asarray([row.decision_ns for row in rows], dtype=np.int64)
    flow = market.causal_market_flow_matrix(decisions, base_features)
    features = np.concatenate((base_features, flow), axis=1)
    feature_names = (*actors.FEATURE_NAMES, *market.FLOW_FEATURE_NAMES)
    windows = sorted({row.run_id for row in rows}, key=int)
    if len(windows) != 4 * WINDOWS_PER_EPOCH:
        raise ValueError("diagnostic requires exactly four 16-window epochs")
    window_index = {run_id: index for index, run_id in enumerate(windows)}
    row_windows = np.asarray([window_index[row.run_id] for row in rows], dtype=np.int16)
    positive = pnl > 0
    epoch_masks = [
        (row_windows >= start) & (row_windows < start + WINDOWS_PER_EPOCH)
        for start in range(0, len(windows), WINDOWS_PER_EPOCH)
    ]
    records = []
    for column, name in enumerate(feature_names):
        raw_aucs = [auc_or_chance(positive[mask], features[mask, column]) for mask in epoch_masks]
        direction = 1 if raw_aucs[0] >= 0.5 else -1
        oriented = [auc if direction == 1 else 1.0 - auc for auc in raw_aucs]
        statistic, pvalue = ks_2samp(features[epoch_masks[0], column], features[epoch_masks[-1], column])
        records.append(
            {
                "feature": name,
                "feature_family": "base_actor" if column < len(actors.FEATURE_NAMES) else "market_flow",
                "epoch0_direction": "higher_is_positive" if direction == 1 else "lower_is_positive",
                "raw_auc_by_epoch": raw_aucs,
                "oriented_auc_by_epoch": oriented,
                "minimum_later_oriented_auc": min(oriented[1:]),
                "mean_later_oriented_auc": float(np.mean(oriented[1:])),
                "direction_consistent_all_epochs": all(auc >= 0.5 for auc in oriented),
                "all_epochs_above_descriptive_floor": all(
                    auc >= DESCRIPTIVE_AUC_FLOOR for auc in oriented
                ),
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
    stable = [row["feature"] for row in records if row["all_epochs_above_descriptive_floor"]]
    identity = {
        "diagnostic_family_identifier": DIAGNOSTIC_FAMILY,
        "source_code_fingerprint": source_code_fingerprint(root),
        "dataset_source_manifest_fingerprint": payload["manifest_sha256"],
        "evidence_epoch": windows,
        "feature_set_fingerprint": base.stable_hash(feature_names),
        "corpus_fingerprint": base.stable_hash(
            {
                "rows": [(row.run_id, row.mint, row.decision_ns) for row in rows],
                "pnl_sha256": hashlib.sha256(pnl.astype("<f8", copy=False).tobytes()).hexdigest(),
            }
        ),
        "decision_horizon_ms": HORIZON_MS,
        "latency_ms": LATENCY_MS,
        "resolution_delay_ms": RESOLUTION_DELAY_MS,
        "orientation_policy": "orient on epoch 0 once and freeze for epochs 1-3",
        "descriptive_auc_floor": DESCRIPTIVE_AUC_FLOOR,
    }
    warranted = len(stable) > 0
    output = {
        "version": SCHEMA_VERSION,
        "diagnostic_id": f"e4d-{base.stable_hash(identity)}",
        "identity": identity,
        "scientific_basis": (
            "The frozen early-horizon frontier measured the highest raw exact-300ms "
            "positive rate at a creation-time decision, which requires a separately "
            "built causal feature corpus before any candidate fit."
        ),
        "corpus_audit": corpus["audit"],
        "label_audit": {
            "rows": len(rows),
            "profitable_rows": int(np.sum(positive)),
            "positive_rate": float(np.mean(positive)),
            "pnl_sha256": hashlib.sha256(pnl.astype("<f8", copy=False).tobytes()).hexdigest(),
            "all_values_finite": bool(np.all(np.isfinite(pnl))),
            "active_untouched_live_data_used": False,
            "production_paths_changed": 0,
        },
        "feature_audit": {
            "rows": len(rows),
            "features": len(feature_names),
            "feature_names": list(feature_names),
            "matrix_sha256": hashlib.sha256(features.astype("<f8", copy=False).tobytes()).hexdigest(),
            "future_values_in_features": False,
            "active_untouched_live_data_used": False,
            "production_paths_changed": 0,
        },
        "epochs": [
            {
                "epoch": epoch,
                "windows": windows[epoch * WINDOWS_PER_EPOCH : (epoch + 1) * WINDOWS_PER_EPOCH],
                "rows": int(mask.sum()),
                "profitable_rows": int(positive[mask].sum()),
                "positive_rate": float(np.mean(positive[mask])),
            }
            for epoch, mask in enumerate(epoch_masks)
        ],
        "features": records,
        "stable_feature_count": len(stable),
        "stable_features": stable,
        "finding": (
            f"{len(stable)} of {len(feature_names)} creation-time causal features "
            "preserved epoch-0 direction with oriented AUC >= 0.52 in all four epochs."
        ),
        "candidate_warranted": warranted,
        "candidate_fitted": False,
        "retired_family": not warranted,
        "failure_classification": None if warranted else "VALIDATION_COLLAPSE",
        "material_change_required_before_rerun": (
            "One preregistered horizon-0 exact-300ms candidate using the transported features."
            if warranted
            else "A new causal decision or feature family; horizon-0 threshold and parameter retuning is prohibited."
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

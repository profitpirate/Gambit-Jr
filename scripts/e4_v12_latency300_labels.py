#!/usr/bin/env python3
"""Build exact 300 ms labels for the consumed V12 development epoch."""

from __future__ import annotations

import json
import os
import pickle
from collections import Counter
from pathlib import Path

import e4_v12_adaptive_exit_search as adaptive
import e4_v12_online_conformal_precision as conformal
import e4_v12_profit_survival_search as base
import numpy as np

LATENCY_MS = 300
CACHE_ROOT = Path(".tmp-relative-price-capped-actor-holdout")
PNL_PATH = CACHE_ROOT / "latency300-pnl.npy"
EXECUTABLE_PATH = CACHE_ROOT / "latency300-executable.npy"
VISITED_PATH = CACHE_ROOT / "latency300-visited.npy"
PROGRESS_PATH = CACHE_ROOT / "latency300-progress.json"
METADATA_PATH = CACHE_ROOT / "latency300-pnl.json"


def atomic_npy(path: Path, values: np.ndarray) -> None:
    """Persist a NumPy array without exposing a partial checkpoint."""
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


def checkpoint(
    pnl: np.ndarray,
    executable: np.ndarray,
    visited: np.ndarray,
    processed_runs: set[str],
    manifest_sha256: str,
) -> None:
    atomic_npy(PNL_PATH, pnl)
    atomic_npy(EXECUTABLE_PATH, executable)
    atomic_npy(VISITED_PATH, visited)
    base.write_json(
        PROGRESS_PATH,
        {
            "version": "e4-v12-latency300-label-progress-v1",
            "dataset_manifest_sha256": manifest_sha256,
            "latency_ms": LATENCY_MS,
            "policy_index": conformal.POLICY_INDEX,
            "processed_runs": sorted(processed_runs, key=int),
            "visited_rows": int(visited.sum()),
            "executable_rows": int(executable.sum()),
            "no_quote_rows": int(np.sum(visited & ~executable)),
            "production_paths_changed": 0,
        },
    )


def build(root: Path) -> dict[str, object]:
    """Replay every consumed row at exactly 300 ms after its decision."""
    with (root / conformal.CACHE).open("rb") as handle:
        payload = pickle.load(handle)
    rows = payload["rows"]
    recent_traces = payload["traces"]
    row_index = {
        (str(row.run_id), str(row.mint)): index for index, row in enumerate(rows)
    }
    if len(row_index) != len(rows):
        raise ValueError("combined rows do not have unique run/mint keys")
    pnl = np.full(len(rows), np.nan, dtype=np.float64)
    executable = np.zeros(len(rows), dtype=bool)
    visited = np.zeros(len(rows), dtype=bool)
    processed_runs: set[str] = set()
    nominal_position = base.STARTING_BANKROLL_SOL * base.POSITION_FRACTION

    def consume(trace: base.Trace) -> None:
        key = (str(trace.run_id), str(trace.mint))
        index = row_index.get(key)
        if index is None:
            return
        visited[index] = True
        outcome = adaptive.adaptive_outcome(
            trace,
            250,
            adaptive.POLICIES[conformal.POLICY_INDEX],
            latency_ms=LATENCY_MS,
        )
        if outcome is None:
            return
        pnl[index] = base.net_pnl(outcome, 0.001, nominal_position)
        executable[index] = True

    for trace in recent_traces.values():
        consume(trace)
        processed_runs.add(str(trace.run_id))
    checkpoint(
        pnl,
        executable,
        visited,
        processed_runs,
        payload["manifest_sha256"],
    )
    print(
        f"recent traces complete: {int(visited.sum())}/{len(rows)} rows",
        flush=True,
    )

    creator_counts: Counter[str] = Counter()
    specs = adaptive.capture_specs(root, include_holdout=True)
    for position, spec in enumerate(specs, 1):
        if spec.run_id in processed_runs:
            continue
        print(
            f"parse {position}/{len(specs)} run={spec.run_id} "
            f"visited={int(visited.sum())}",
            flush=True,
        )
        actual_sha256 = base.sha256_path(spec.events_path)
        if actual_sha256 != spec.expected_sha256:
            raise ValueError(f"capture hash changed: {spec.run_id}")
        traces, parse_errors = base.load_capture(spec, creator_counts)
        if parse_errors:
            raise ValueError(
                f"capture {spec.run_id} contains {parse_errors} parse errors"
            )
        for trace in traces:
            consume(trace)
        processed_runs.add(spec.run_id)
        checkpoint(
            pnl,
            executable,
            visited,
            processed_runs,
            payload["manifest_sha256"],
        )
        del traces

    if not bool(np.all(visited)):
        missing = np.flatnonzero(~visited)
        examples = [
            (str(rows[index].run_id), str(rows[index].mint))
            for index in missing[:10]
        ]
        raise ValueError(
            f"{len(missing)} combined rows lack a source trace: {examples}"
        )
    metadata: dict[str, object] = {
        "version": "e4-v12-latency300-labels-v1",
        "dataset_manifest_sha256": payload["manifest_sha256"],
        "latency_ms": LATENCY_MS,
        "decision_horizon_ms": 250,
        "policy_index": conformal.POLICY_INDEX,
        "policy": adaptive.POLICIES[conformal.POLICY_INDEX].key,
        "rows": len(rows),
        "capture_windows": len(processed_runs),
        "visited_rows": int(visited.sum()),
        "executable_rows": int(executable.sum()),
        "no_quote_rows": int(np.sum(~executable)),
        "profitable_rows": int(np.sum(pnl[executable] > 0)),
        "negative_or_zero_rows": int(np.sum(pnl[executable] <= 0)),
        "pnl_sha256": base.sha256_path(PNL_PATH),
        "executable_sha256": base.sha256_path(EXECUTABLE_PATH),
        "all_source_hashes_verified": True,
        "active_untouched_live_data_used": False,
        "production_paths_changed": 0,
    }
    base.write_json(METADATA_PATH, metadata)
    return metadata


def main() -> None:
    result = build(Path.cwd())
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

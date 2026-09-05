#!/usr/bin/env python3
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from scripts import e4_v12_golden_thesis_search as golden
from scripts import e4_v12_identity_cluster_registry as identity_registry
from scripts import e4_v12_true_latency_replay_v3  # noqa: F401 - dynamic fees

_ORIGINAL_LOAD_RUNS = golden.load_runs
_ORIGINAL_APPLY_EVENT = golden.apply_event
_ORIGINAL_MEMORY = golden.IntentMemory
_REGISTRY: dict[str, Any] = {"wallet_to_cluster": {}, "clusters": {}}


def _canonical(address: str) -> str:
    value = str(address or "")
    cluster = (_REGISTRY.get("wallet_to_cluster") or {}).get(value)
    return f"cluster:{cluster}" if cluster else value


def load_runs_clustered(pairs):
    global _REGISTRY
    runs = _ORIGINAL_LOAD_RUNS(pairs)
    create_times = [
        launch.create_ns
        for run in runs
        for launch in run.launches.values()
        if launch.create_ns > 0
    ]
    if create_times:
        _REGISTRY = identity_registry.build_registry(
            Path.cwd(),
            "HEAD",
            min(create_times) / 1_000_000_000.0,
        )
    for run in runs:
        for launch in run.launches.values():
            launch.creator = _canonical(launch.creator)
    return runs


def apply_event_clustered(launch, state, row: Mapping[str, Any]) -> None:
    trader = str(row.get("trader") or "")
    if trader:
        mapped = dict(row)
        mapped["trader"] = _canonical(trader)
        _ORIGINAL_APPLY_EVENT(launch, state, mapped)
    else:
        _ORIGINAL_APPLY_EVENT(launch, state, row)


def seeded_cluster_memory():
    memory = _ORIGINAL_MEMORY()
    for cluster_id, row in (_REGISTRY.get("clusters") or {}).items():
        key = f"cluster:{cluster_id}"
        memory.creator_attempts[key] = golden.integer(row.get("trades"))
        memory.creator_wins[key] = golden.integer(row.get("wins"))
        memory.creator_losses[key] = golden.integer(row.get("losses"))
    return memory


golden.load_runs = load_runs_clustered
golden.apply_event = apply_event_clustered
golden.IntentMemory = seeded_cluster_memory


if __name__ == "__main__":
    raise SystemExit(golden.main())

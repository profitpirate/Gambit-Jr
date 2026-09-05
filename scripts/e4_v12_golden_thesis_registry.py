#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts import e4_v12_baseline_registry as baseline
from scripts import e4_v12_golden_thesis_search as golden

_ORIGINAL_LOAD_RUNS = golden.load_runs
_ORIGINAL_MEMORY = golden.IntentMemory
_BASELINE: dict[str, Any] = {"creators": {}}


def load_runs_with_causal_registry(pairs):
    global _BASELINE
    runs = _ORIGINAL_LOAD_RUNS(pairs)
    create_times = [
        launch.create_ns
        for run in runs
        for launch in run.launches.values()
        if launch.create_ns > 0
    ]
    if create_times:
        _BASELINE = baseline.build_registry(
            Path.cwd(),
            "HEAD",
            min(create_times) / 1_000_000_000.0,
        )
    return runs


def seeded_memory():
    memory = _ORIGINAL_MEMORY()
    for creator, row in (_BASELINE.get("creators") or {}).items():
        memory.creator_attempts[str(creator)] = golden.integer(row.get("trades"))
        memory.creator_wins[str(creator)] = golden.integer(row.get("wins"))
        memory.creator_losses[str(creator)] = golden.integer(row.get("losses"))
    return memory


golden.load_runs = load_runs_with_causal_registry
golden.IntentMemory = seeded_memory


if __name__ == "__main__":
    raise SystemExit(golden.main())

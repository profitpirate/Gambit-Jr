#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from scripts import e4_v12_baseline_registry as baseline
from scripts import e4_v12_reactive_profit_model as model

_ORIGINAL_BUILD_ROWS = model.build_rows


def build_rows_with_causal_registry(runs: Sequence[model.golden.RunData]):
    if not runs:
        return []
    cutoff_ns = min(
        launch.create_ns
        for run in runs
        for launch in run.launches.values()
        if launch.create_ns > 0
    )
    registry = baseline.build_registry(Path.cwd(), "HEAD", cutoff_ns / 1_000_000_000.0)
    memory = model.Memory()
    for creator, row in (registry.get("creators") or {}).items():
        memory.creator_attempts[str(creator)] = model.integer(row.get("trades"))
        memory.creator_wins[str(creator)] = model.integer(row.get("wins"))
        memory.creator_losses[str(creator)] = model.integer(row.get("losses"))

    output = []
    for run in runs:
        current = []
        launches = sorted(
            run.launches.values(),
            key=lambda launch: (
                model.integer(launch.e4_buy.get("received_ns")) if launch.e4_buy is not None else 2**63 - 1,
                launch.mint,
            ),
        )
        for launch in launches:
            row = model.source_feature_row(run, launch, memory)
            if row is not None:
                row["baseline_registry_creator"] = bool(launch.creator in (registry.get("creators") or {}))
                current.append(row)
        output.extend(current)
        for row in current:
            launch = run.launches[str(row["mint"])]
            _, buyers = model.pre_source_state(launch)
            memory.observe(launch, buyers, bool(row["e4_won"]))
    return output


model.build_rows = build_rows_with_causal_registry


if __name__ == "__main__":
    raise SystemExit(model.main())

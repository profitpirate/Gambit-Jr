#!/usr/bin/env python3
from __future__ import annotations

from typing import Sequence

from scripts import e4_v12_clustered_preimpact as clustered  # patches golden load/apply
from scripts import e4_v12_reactive_profit_model as model


def build_rows_clustered(runs: Sequence[model.golden.RunData]):
    memory = model.Memory()
    for cluster_id, row in (clustered._REGISTRY.get("clusters") or {}).items():
        key = f"cluster:{cluster_id}"
        memory.creator_attempts[key] = model.integer(row.get("trades"))
        memory.creator_wins[key] = model.integer(row.get("wins"))
        memory.creator_losses[key] = model.integer(row.get("losses"))

    output = []
    for run in runs:
        current = []
        launches = sorted(
            run.launches.values(),
            key=lambda launch: (
                model.integer(launch.e4_buy.get("received_ns"))
                if launch.e4_buy is not None
                else 2**63 - 1,
                launch.mint,
            ),
        )
        for launch in launches:
            row = model.source_feature_row(run, launch, memory)
            if row is not None:
                row["identity_cluster"] = (
                    launch.creator.removeprefix("cluster:")
                    if launch.creator.startswith("cluster:")
                    else ""
                )
                row["clustered_creator"] = bool(row["identity_cluster"])
                current.append(row)
        output.extend(current)
        # Outcomes become historical only after the complete window, avoiding
        # learning from a trade that could still be open at a later entry.
        for row in current:
            launch = run.launches[str(row["mint"])]
            _, buyers = model.pre_source_state(launch)
            memory.observe(launch, buyers, bool(row["e4_won"]))
    return output


model.build_rows = build_rows_clustered


if __name__ == "__main__":
    raise SystemExit(model.main())

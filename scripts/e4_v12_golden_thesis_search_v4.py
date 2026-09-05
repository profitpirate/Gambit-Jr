#!/usr/bin/env python3
from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

import numpy as np

import e4_v12_golden_thesis_search_v2 as base
import e4_v12_golden_thesis_search_v3 as v3


def focused_tune(
    train: list[dict[str, Any]],
    validation: list[dict[str, Any]],
    run_map: Mapping[str, base.replay.RunData],
    latencies: Sequence[float],
):
    best = None
    flag_pairs = (
        (False, False),
        (True, False),
        (False, True),
        (True, True),
    )
    for spec in base.specs():
        model = base.fit_model(train, spec)
        scored = [dict(row) for row in validation]
        values = base.probabilities(model, scored)
        for row, probability in zip(scored, values):
            row["probability"] = float(probability)
        scored.sort(key=lambda row: (base.integer(row["decision_ns"]), str(row["mint"])))
        thresholds = sorted(set(float(np.quantile(values, q)) for q in (
            0.97, 0.98, 0.99, 0.995, 0.998, 0.999
        )))
        shortlist = []
        for threshold in thresholds:
            for margin in (0.0, 0.05, 0.10, 0.15):
                for cooldown in (0.0, 250.0):
                    for max_age in (50.0, 150.0, 400.0):
                        for identity_top, seed_velocity_top in flag_pairs:
                            gate = base.Gate(
                                threshold,
                                margin,
                                cooldown,
                                max_age,
                                identity_top,
                                seed_velocity_top,
                            )
                            predictions, selection = v3.select_scored(scored, gate)
                            if selection["predictions"] < 4:
                                continue
                            if (
                                selection["winning_precision"] < 0.35
                                and selection["intent_precision"] < 0.50
                            ):
                                continue
                            selection_score = (
                                selection["winning_precision_wilson_low"],
                                selection["winning_precision"],
                                selection["intent_precision"],
                                selection["winning_e4_true"],
                                selection["median_lead_ms"] or 0.0,
                                -selection["predictions"],
                            )
                            shortlist.append((selection_score, gate, predictions, selection))
        shortlist.sort(key=lambda item: item[0], reverse=True)
        for _, gate, predictions, selection in shortlist[:24]:
            for floor in (200, 400, 600, 800, 1_000):
                grid = base.economic_grid(
                    run_map,
                    predictions,
                    floor_bps=floor,
                    latencies=latencies,
                )
                passed = base.economics_pass(grid, 4)
                worst_wr = min(base.finite(row.get("win_rate")) for row in grid.values())
                worst_pf = min(base.finite(row.get("profit_factor")) for row in grid.values())
                total_pnl = sum(base.finite(row.get("net_pnl_sol")) for row in grid.values())
                objective = (
                    int(passed),
                    worst_wr,
                    selection["winning_precision_wilson_low"],
                    worst_pf,
                    total_pnl,
                    selection["winning_e4_true"],
                    -selection["predictions"],
                )
                if best is None or objective > best[0]:
                    best = (objective, spec, model, gate, floor, selection, grid)
        print(json.dumps({
            "spec": spec.as_dict(),
            "shortlisted_gates": len(shortlist),
            "best": list(best[0]) if best else None,
        }), flush=True)
    if best is None:
        raise RuntimeError("no precision-first gate produced enough validation trades")
    return best


base.tune = focused_tune
base.select_predictions = lambda rows, model, gate: v3.select_scored(
    sorted(
        [
            {**dict(row), "probability": float(probability)}
            for row, probability in zip(rows, base.probabilities(model, rows))
        ],
        key=lambda row: (base.integer(row["decision_ns"]), str(row["mint"])),
    ),
    gate,
)


if __name__ == "__main__":
    raise SystemExit(base.main())

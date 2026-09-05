#!/usr/bin/env python3
from __future__ import annotations

import json
from collections import defaultdict, deque
from typing import Any, Mapping, Sequence

import numpy as np

import e4_v12_golden_thesis_search_v2 as base


def select_scored(
    scored_rows: Sequence[dict[str, Any]],
    gate: base.Gate,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    recent: deque[dict[str, Any]] = deque()
    predicted_mints: set[tuple[str, str]] = set()
    predictions: list[dict[str, Any]] = []
    last_global_by_run: dict[str, int] = defaultdict(lambda: -10**30)
    for row in scored_rows:
        run_id = str(row["run_id"])
        now_ns = base.integer(row["decision_ns"])
        while recent and (
            str(recent[0]["run_id"]) != run_id
            or base.integer(recent[0]["decision_ns"]) < now_ns - 250_000_000
        ):
            recent.popleft()
        recent.append(row)
        key = (run_id, str(row["mint"]))
        if key in predicted_mints:
            continue
        probability = base.finite(row.get("probability"))
        if probability < gate.threshold or base.finite(row.get("age_ms")) > gate.maximum_age_ms:
            continue
        if gate.require_identity_top and not bool(row.get("current_is_identity_top")):
            continue
        if gate.require_seed_or_velocity_top and not (
            bool(row.get("current_is_seed_top"))
            or bool(row.get("current_is_velocity_top"))
        ):
            continue
        best_other = max(
            (
                base.finite(item.get("probability"))
                for item in recent
                if item["run_id"] == run_id and item["mint"] != row["mint"]
            ),
            default=0.0,
        )
        margin = probability - best_other
        if margin < gate.minimum_margin:
            continue
        if now_ns - last_global_by_run[run_id] < int(gate.cooldown_ms * 1e6):
            continue
        prediction = {
            name: row.get(name)
            for name in (
                "run_id", "mint", "decision_ns", "decision_sequence",
                "decision_event_id", "decision_signature", "decision_event_index",
                "lead_ms", "source_won", "target_intent", "target",
            )
        }
        prediction.update({
            "score": probability,
            "family": "v12_golden_profitable_intent",
            "probability": probability,
            "margin": margin,
            "entry_fraction": 0.0185,
        })
        predictions.append(prediction)
        predicted_mints.add(key)
        last_global_by_run[run_id] = now_ns
    true = sum(bool(row.get("target")) for row in predictions)
    intent = sum(bool(row.get("target_intent")) for row in predictions)
    leads = [base.finite(row.get("lead_ms")) for row in predictions if row.get("target")]
    return predictions, {
        "predictions": len(predictions),
        "winning_e4_true": true,
        "e4_intent_true": intent,
        "winning_precision": true / len(predictions) if predictions else 0.0,
        "intent_precision": intent / len(predictions) if predictions else 0.0,
        "winning_precision_wilson_low": base.wilson_lower(true, len(predictions)),
        "median_lead_ms": float(np.median(leads)) if leads else None,
    }


def optimized_tune(
    train: list[dict[str, Any]],
    validation: list[dict[str, Any]],
    run_map: Mapping[str, base.replay.RunData],
    latencies: Sequence[float],
):
    best = None
    for spec in base.specs():
        model = base.fit_model(train, spec)
        scored = [dict(row) for row in validation]
        values = base.probabilities(model, scored)
        for row, probability in zip(scored, values):
            row["probability"] = float(probability)
        scored.sort(key=lambda row: (base.integer(row["decision_ns"]), str(row["mint"])))
        thresholds = sorted(set(float(np.quantile(values, q)) for q in (
            0.93, 0.95, 0.97, 0.98, 0.99, 0.995, 0.998, 0.999
        )))
        shortlist = []
        for threshold in thresholds:
            for margin in (0.0, 0.05, 0.10, 0.15):
                for cooldown in (0.0, 100.0, 250.0):
                    for max_age in (50.0, 150.0, 400.0, 1_000.0):
                        for identity_top in (False, True):
                            for seed_velocity_top in (False, True):
                                gate = base.Gate(
                                    threshold,
                                    margin,
                                    cooldown,
                                    max_age,
                                    identity_top,
                                    seed_velocity_top,
                                )
                                predictions, selection = select_scored(scored, gate)
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
        for _, gate, predictions, selection in shortlist[:36]:
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
        raise RuntimeError("no candidate gate produced enough validation trades")
    return best


base.tune = optimized_tune
base.select_predictions = lambda rows, model, gate: select_scored(
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

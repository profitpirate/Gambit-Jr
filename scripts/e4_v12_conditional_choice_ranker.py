#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

import e4_v12_conclusive_entry_rerun as base
import e4_v12_ranked_multimode_search as rich

FEATURES = rich.UNION


@dataclass
class ChoiceSet:
    run_index: int
    run_id: str
    timestamp_ns: int
    target_mint: str | None
    target_label: str
    candidates: list[dict[str, Any]]


def snapshot_at(launch: base.Launch, timestamp_ns: int, cutoff_key: tuple[int, int, int, int] | None = None) -> dict[str, Any] | None:
    if timestamp_ns < launch.create_ns:
        return None
    state = base.State(latest_ns=launch.create_ns)
    for event in launch.events:
        key = base.event_key(event)
        received = base.integer(event.get("received_ns"))
        if cutoff_key is not None:
            if key >= cutoff_key:
                break
        elif received >= timestamp_ns:
            break
        base.apply_event(launch, state, event)
    timestamp = max(launch.create_ns, min(timestamp_ns, state.latest_ns or timestamp_ns))
    row = base.snapshot_dict(launch, base.state_copy(state), timestamp, "IGNORED", "CHOICE_CONTROL")
    return row


def target_snapshot(launch: base.Launch) -> tuple[dict[str, Any], tuple[int, int, int, int]] | None:
    marker = base.marker_for(launch)
    if marker is None:
        return None
    label, key, timestamp = marker
    row = snapshot_at(launch, timestamp, key)
    if row is None:
        return None
    row["label"] = label
    row["positive"] = True
    row["stage"] = "PRE_INTENT_CHOICE"
    row["timestamp_ns"] = timestamp
    return row, key


def shape_distance(target: Mapping[str, Any], control: Mapping[str, Any]) -> float:
    return (
        1.8 * abs(base.log1p(target.get("fdv_usd")) - base.log1p(control.get("fdv_usd")))
        + 1.5 * abs(base.log1p(target.get("creator_seed_sol")) - base.log1p(control.get("creator_seed_sol")))
        + 0.4 * abs(base.finite(target.get("age_ms")) - base.finite(control.get("age_ms"))) / 500.0
        + 0.3 * abs(base.integer(target.get("buy_count")) - base.integer(control.get("buy_count")))
        + 0.4 * abs(base.integer(target.get("unique_buyers")) - base.integer(control.get("unique_buyers")))
        + 0.3 * abs(base.integer(target.get("same_slot_buys")) - base.integer(control.get("same_slot_buys")))
    )


def build_choice_sets(
    launches: Mapping[str, base.Launch],
    failed: Mapping[str, list[dict[str, Any]]],
    controls_per_set: int,
    create_window_ms: float,
) -> tuple[list[ChoiceSet], list[dict[str, Any]]]:
    for launch in launches.values():
        if failed.get(launch.mint):
            launch.failed_attempt = failed[launch.mint][0]
    positives = {launch.mint for launch in launches.values() if base.marker_for(launch) is not None}
    by_run: dict[int, list[base.Launch]] = defaultdict(list)
    for launch in launches.values():
        by_run[launch.run_index].append(launch)
    for rows in by_run.values():
        rows.sort(key=lambda launch: launch.create_ns)

    sets: list[ChoiceSet] = []
    all_rows: list[dict[str, Any]] = []
    create_window_ns = int(create_window_ms * 1e6)
    for launch in sorted(launches.values(), key=lambda item: (item.run_index, item.create_ns)):
        target_value = target_snapshot(launch)
        if target_value is None:
            continue
        target, _ = target_value
        if not base.eligible(target):
            continue
        timestamp = base.integer(target["timestamp_ns"])
        candidates: list[tuple[float, dict[str, Any]]] = []
        for control_launch in by_run[launch.run_index]:
            if control_launch.mint == launch.mint or control_launch.mint in positives:
                continue
            if control_launch.create_ns > timestamp:
                break
            if abs(control_launch.create_ns - launch.create_ns) > create_window_ns:
                continue
            control = snapshot_at(control_launch, timestamp)
            if control is None or not base.eligible(control):
                continue
            if base.finite(control.get("age_ms")) > 1500.0:
                continue
            candidates.append((shape_distance(target, control), control))
        candidates.sort(key=lambda pair: pair[0])
        controls = [row for _, row in candidates[:controls_per_set]]
        if not controls:
            continue
        target["choice_set_id"] = f"{launch.run_id}:{launch.mint}"
        for row in controls:
            row["choice_set_id"] = target["choice_set_id"]
        choice = ChoiceSet(
            run_index=launch.run_index,
            run_id=launch.run_id,
            timestamp_ns=timestamp,
            target_mint=launch.mint,
            target_label=str(target["label"]),
            candidates=[target, *controls],
        )
        sets.append(choice)
        all_rows.extend(choice.candidates)

    # Null-choice windows: contemporaneous ignored launches where no observed
    # successful or failed E4 attempt occurred. They teach an absolute abstain
    # threshold instead of forcing one selection in every launch burst.
    rng = random.Random(712)
    positive_by_run: dict[int, list[int]] = defaultdict(list)
    for choice in sets:
        positive_by_run[choice.run_index].append(choice.timestamp_ns)
    for run_index, run_launches in by_run.items():
        ignored = [launch for launch in run_launches if launch.mint not in positives]
        buckets: dict[int, list[base.Launch]] = defaultdict(list)
        for launch in ignored:
            buckets[launch.create_ns // create_window_ns].append(launch)
        bucket_items = list(buckets.items())
        rng.shuffle(bucket_items)
        target_nulls = max(1, len(positive_by_run[run_index]))
        added = 0
        for _, group in bucket_items:
            if added >= target_nulls:
                break
            timestamp = max(launch.create_ns for launch in group) + min(create_window_ns, 400_000_000)
            if any(abs(timestamp - positive_time) <= create_window_ns for positive_time in positive_by_run[run_index]):
                continue
            candidates = []
            for launch in group:
                row = snapshot_at(launch, timestamp)
                if row is not None and base.eligible(row):
                    row["choice_set_id"] = f"{launch.run_id}:NULL:{timestamp}"
                    candidates.append(row)
            if len(candidates) < 2:
                continue
            candidates.sort(
                key=lambda row: (
                    base.log1p(row.get("creator_seed_sol"))
                    + base.integer(row.get("unique_buyers"))
                    + 0.5 * base.integer(row.get("same_slot_buys"))
                ),
                reverse=True,
            )
            candidates = candidates[:controls_per_set]
            choice = ChoiceSet(
                run_index=run_index,
                run_id=candidates[0]["run_id"],
                timestamp_ns=timestamp,
                target_mint=None,
                target_label="NO_CHOICE",
                candidates=candidates,
            )
            sets.append(choice)
            all_rows.extend(candidates)
            added += 1

    return sets, all_rows


def annotate_rows(
    rows: list[dict[str, Any]],
    launches: Mapping[str, base.Launch],
    metadata: Mapping[str, Mapping[str, Any]],
    static_history: Mapping[str, Mapping[str, float]],
) -> None:
    base.add_metadata(rows, metadata)
    base.add_history(rows, static_history)
    base.add_competition(rows)
    rich.add_extended_history(rows)


def vector(row: Mapping[str, Any]) -> np.ndarray:
    values = rich.extended_vector(row)
    return np.asarray([values.get(name, 0.0) for name in FEATURES], dtype=float)


@dataclass
class PairwiseRanker:
    scaler: StandardScaler
    model: LogisticRegression

    def utility(self, row: Mapping[str, Any]) -> float:
        raw = vector(row)
        scale = np.where(self.scaler.scale_ == 0, 1.0, self.scaler.scale_)
        # Difference-model ranking: the scaler mean cancels between candidates.
        return float(np.dot(self.model.coef_[0], raw / scale))

    def export(self) -> dict[str, Any]:
        return {
            "features": FEATURES,
            "scale": [float(value) for value in self.scaler.scale_],
            "coefficient": [float(value) for value in self.model.coef_[0]],
            "intercept": float(self.model.intercept_[0]),
        }


def fit_pairwise(sets: Sequence[ChoiceSet], c: float) -> PairwiseRanker:
    differences: list[np.ndarray] = []
    labels: list[int] = []
    for choice in sets:
        if choice.target_mint is None:
            continue
        target = next((row for row in choice.candidates if row["mint"] == choice.target_mint), None)
        if target is None:
            continue
        target_vector = vector(target)
        for control in choice.candidates:
            if control is target:
                continue
            delta = target_vector - vector(control)
            differences.append(delta)
            labels.append(1)
            differences.append(-delta)
            labels.append(0)
    if not differences:
        raise RuntimeError("no pairwise examples")
    x = np.asarray(differences, dtype=float)
    y = np.asarray(labels)
    scaler = StandardScaler().fit(x)
    model = LogisticRegression(C=c, max_iter=5000, solver="liblinear").fit(scaler.transform(x), y)
    return PairwiseRanker(scaler, model)


@dataclass(frozen=True)
class ChoiceGate:
    minimum_utility: float
    minimum_margin: float
    maximum_rank: int

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def rank_choice(choice: ChoiceSet, ranker: PairwiseRanker) -> list[tuple[float, dict[str, Any]]]:
    return sorted(((ranker.utility(row), row) for row in choice.candidates), key=lambda pair: pair[0], reverse=True)


def evaluate(sets: Sequence[ChoiceSet], ranker: PairwiseRanker, gate: ChoiceGate) -> dict[str, Any]:
    predictions = 0
    true = 0
    false = 0
    positives = sum(choice.target_mint is not None for choice in sets)
    top1_hits = 0
    top2_hits = 0
    success_total = sum(choice.target_label == "SUCCESS" for choice in sets)
    success_hits = 0
    failed_total = sum(choice.target_label == "FAILED_ATTEMPT" for choice in sets)
    failed_hits = 0
    rows = []
    for choice in sets:
        ranked = rank_choice(choice, ranker)
        if not ranked:
            continue
        target_rank = None
        if choice.target_mint is not None:
            for index, (_, row) in enumerate(ranked, start=1):
                if row["mint"] == choice.target_mint:
                    target_rank = index
                    break
            top1_hits += int(target_rank == 1)
            top2_hits += int(target_rank is not None and target_rank <= 2)
        winner_score, winner = ranked[0]
        runner_up = ranked[1][0] if len(ranked) > 1 else -1e9
        margin = winner_score - runner_up
        accepted = winner_score >= gate.minimum_utility and margin >= gate.minimum_margin
        correct = bool(accepted and choice.target_mint is not None and winner["mint"] == choice.target_mint)
        if accepted:
            predictions += 1
            true += int(correct)
            false += int(not correct)
            if correct and choice.target_label == "SUCCESS":
                success_hits += 1
            if correct and choice.target_label == "FAILED_ATTEMPT":
                failed_hits += 1
        rows.append({
            "run_id": choice.run_id,
            "timestamp_ns": choice.timestamp_ns,
            "target_mint": choice.target_mint,
            "target_label": choice.target_label,
            "candidate_count": len(ranked),
            "target_rank": target_rank,
            "winner_mint": winner["mint"],
            "winner_score": winner_score,
            "margin": margin,
            "accepted": accepted,
            "correct": correct,
        })
    return {
        "choice_sets": len(sets),
        "positive_choice_sets": positives,
        "null_choice_sets": len(sets) - positives,
        "predictions": predictions,
        "true": true,
        "false_positives": false,
        "precision": true / predictions if predictions else 0.0,
        "precision_wilson_low": base.wilson_lower(true, predictions),
        "recall": true / positives if positives else 0.0,
        "top1_rank_accuracy": top1_hits / positives if positives else 0.0,
        "top2_rank_accuracy": top2_hits / positives if positives else 0.0,
        "success_recall": success_hits / success_total if success_total else 0.0,
        "failed_attempt_recall": failed_hits / failed_total if failed_total else 0.0,
        "rows": rows,
    }


def tune(train_sets: list[ChoiceSet], validation_sets: list[ChoiceSet]) -> tuple[PairwiseRanker, ChoiceGate, dict[str, Any], float]:
    best = None
    for c in (0.02, 0.05, 0.10, 0.20, 0.50, 1.0, 2.0, 5.0):
        ranker = fit_pairwise(train_sets, c)
        validation_rankings = [rank_choice(choice, ranker) for choice in validation_sets]
        score_values = sorted({score for ranked in validation_rankings for score, _ in ranked})
        margin_values = sorted({ranked[0][0] - ranked[1][0] for ranked in validation_rankings if len(ranked) > 1})
        utility_thresholds = [
            score_values[min(len(score_values) - 1, max(0, int(q * (len(score_values) - 1))))]
            for q in (0.50, 0.65, 0.75, 0.82, 0.88, 0.92, 0.95, 0.97, 0.985)
        ] if score_values else [0.0]
        margin_thresholds = [
            margin_values[min(len(margin_values) - 1, max(0, int(q * (len(margin_values) - 1))))]
            for q in (0.0, 0.25, 0.50, 0.65, 0.75, 0.85, 0.92)
        ] if margin_values else [0.0]
        for utility in utility_thresholds:
            for margin in margin_thresholds:
                gate = ChoiceGate(float(utility), max(0.0, float(margin)), 1)
                result = evaluate(validation_sets, ranker, gate)
                if result["true"] < 4 or result["recall"] < 0.10:
                    continue
                valid = result["precision"] >= 0.60 and result["precision_wilson_low"] >= 0.30
                objective = (
                    int(valid),
                    result["precision_wilson_low"],
                    result["precision"],
                    result["recall"],
                    result["top1_rank_accuracy"],
                    result["true"],
                    -result["false_positives"],
                )
                if best is None or objective > best[0]:
                    best = (objective, ranker, gate, result, c)
    if best is None:
        raise RuntimeError("no conditional choice gate produced four validation true positives")
    return best[1], best[2], best[3], best[4]


def top_drivers(ranker: PairwiseRanker, limit: int = 12) -> list[dict[str, Any]]:
    scale = np.where(ranker.scaler.scale_ == 0, 1.0, ranker.scaler.scale_)
    effective = ranker.model.coef_[0] / scale
    pairs = sorted(zip(FEATURES, effective), key=lambda pair: abs(float(pair[1])), reverse=True)
    return [{"feature": name, "weight": float(weight)} for name, weight in pairs[:limit]]


def main() -> int:
    parser = argparse.ArgumentParser(description="Conditional E4 choice ranker against exact-time ignored alternatives")
    parser.add_argument("--pair", action="append", default=[])
    parser.add_argument("--attempts", action="append", default=[], type=Path)
    parser.add_argument("--metadata-cache", action="append", default=[], type=Path)
    parser.add_argument("--creator-history", type=Path, required=True)
    parser.add_argument("--controls-per-set", type=int, default=30)
    parser.add_argument("--create-window-ms", type=float, default=750.0)
    parser.add_argument("--metadata-concurrency", type=int, default=96)
    parser.add_argument("--metadata-timeout", type=float, default=5.0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-output", type=Path, required=True)
    args = parser.parse_args()

    pairs = [base.parse_pair(value) for value in args.pair]
    launches, run_ids = base.load_launches(pairs)
    failed = base.load_failed_attempts(args.attempts)
    choice_sets, rows = build_choice_sets(launches, failed, args.controls_per_set, args.create_window_ms)
    metadata_cache = base.scan_metadata_cache(args.metadata_cache)
    relevant = {str(row["mint"]): launches[str(row["mint"])] for row in rows if str(row["mint"]) in launches}
    metadata_cache = asyncio.run(base.fill_metadata(relevant, metadata_cache, args.metadata_concurrency, args.metadata_timeout))
    annotate_rows(rows, launches, metadata_cache, base.load_creator_history(args.creator_history))

    live_index = len(run_ids) - 1
    validation_start = max(3, live_index - 3)
    train_sets = [choice for choice in choice_sets if choice.run_index < validation_start]
    validation_sets = [choice for choice in choice_sets if validation_start <= choice.run_index < live_index]
    live_sets = [choice for choice in choice_sets if choice.run_index == live_index]
    pre_live_sets = [choice for choice in choice_sets if choice.run_index < live_index]

    seed_ranker, gate, validation_result, c = tune(train_sets, validation_sets)
    ranker = fit_pairwise(pre_live_sets, c)
    live_result = evaluate(live_sets, ranker, gate)

    folds = []
    total_true = total_predictions = total_positive = 0
    for fold in range(max(3, validation_start), len(run_ids)):
        fold_train = [choice for choice in choice_sets if choice.run_index < fold]
        fold_sets = [choice for choice in choice_sets if choice.run_index == fold]
        if not fold_train or not fold_sets:
            continue
        fold_ranker = fit_pairwise(fold_train, c)
        result = evaluate(fold_sets, fold_ranker, gate)
        result["run_id"] = run_ids[fold]
        result.pop("rows", None)
        folds.append(result)
        total_true += base.integer(result["true"])
        total_predictions += base.integer(result["predictions"])
        total_positive += base.integer(result["positive_choice_sets"])
    walk = {
        "folds": folds,
        "true": total_true,
        "predictions": total_predictions,
        "positive_choice_sets": total_positive,
        "precision": total_true / total_predictions if total_predictions else 0.0,
        "precision_wilson_low": base.wilson_lower(total_true, total_predictions),
        "recall": total_true / total_positive if total_positive else 0.0,
    }

    passed = bool(
        validation_result["precision"] >= 0.60
        and validation_result["recall"] >= 0.10
        and validation_result["true"] >= 4
        and walk["precision"] >= 0.55
        and walk["precision_wilson_low"] >= 0.30
        and walk["true"] >= 8
        and live_result["precision"] >= 0.50
        and live_result["recall"] >= 0.10
        and live_result["true"] >= 2
    )
    status = "LIVE_HOLDOUT_CONFIRMED" if passed else "NOT_CONCLUSIVE"
    drivers = top_drivers(ranker)
    thesis = (
        "E4 does not buy every qualifying launch. It ranks the contemporaneous unsold low-FDV set and attempts only the top candidate when "
        "creator/social authority or prior E4-linked first-buyer topology creates a clear score margin over the alternatives."
    )
    model_payload = {
        "version": "e4-v12-conditional-choice-ranker-v1",
        "status": status,
        "thesis": thesis,
        "guardrails": {
            "minimum_creator_seed_sol": 0.20,
            "minimum_fdv_usd": 2750.0,
            "maximum_fdv_usd": 10000.0,
            "maximum_age_ms": 1500.0,
            "pre_entry_sell_count": 0,
            "mayhem_allowed": False,
            "create_competition_window_ms": args.create_window_ms,
        },
        "gate": gate.as_dict(),
        "ranker": ranker.export(),
        "top_drivers": drivers,
        "training_runs": run_ids[:live_index],
        "live_run": run_ids[live_index],
        "validation": {key: value for key, value in validation_result.items() if key != "rows"},
        "walk_forward": walk,
        "live_holdout": {key: value for key, value in live_result.items() if key != "rows"},
    }
    report = {
        "version": "e4-v12-conditional-choice-report-v1",
        "status": status,
        "coverage": {
            "runs": run_ids,
            "launches": len(launches),
            "choice_sets": len(choice_sets),
            "positive_choice_sets": sum(choice.target_mint is not None for choice in choice_sets),
            "null_choice_sets": sum(choice.target_mint is None for choice in choice_sets),
            "candidate_rows": len(rows),
        },
        "causality": {
            "target": "state immediately before E4's first successful or mapped failed buy attempt",
            "controls": "launches created within the same 750ms competition window and frozen at that exact timestamp",
            "null_windows": "simultaneous launch bursts in which E4 expressed no observable buy intent",
            "history": "only E4 intentions observed before each choice timestamp",
            "live_holdout": run_ids[live_index],
        },
        "thesis": thesis,
        "gate": gate.as_dict(),
        "top_drivers": drivers,
        "validation": validation_result,
        "walk_forward": walk,
        "live_holdout": live_result,
        "safe_to_implement": passed,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.model_output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    args.model_output.write_text(json.dumps(model_payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "status": status,
        "coverage": report["coverage"],
        "gate": gate.as_dict(),
        "top_drivers": drivers,
        "validation": {key: validation_result[key] for key in ("positive_choice_sets", "null_choice_sets", "predictions", "true", "precision", "recall", "top1_rank_accuracy", "top2_rank_accuracy")},
        "walk_forward": walk,
        "live_holdout": {key: live_result[key] for key in ("positive_choice_sets", "null_choice_sets", "predictions", "true", "precision", "recall", "top1_rank_accuracy", "top2_rank_accuracy", "success_recall", "failed_attempt_recall")},
    }, indent=2, sort_keys=True), flush=True)
    return 0 if passed else 5


if __name__ == "__main__":
    raise SystemExit(main())

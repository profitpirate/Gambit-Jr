#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
from sklearn.ensemble import ExtraTreesClassifier, HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.tree import DecisionTreeClassifier

import e4_v12_conclusive_entry_rerun as base


def add_extended_history(rows: list[dict[str, Any]]) -> None:
    host_attempts: Counter[str] = Counter()
    host_successes: Counter[str] = Counter()
    program_attempts: Counter[str] = Counter()
    signature_shape_attempts: Counter[tuple[int, int]] = Counter()
    ordered = sorted(rows, key=lambda row: (base.integer(row["timestamp_ns"]), 0 if not row["positive"] else 1, row["mint"]))
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in ordered:
        grouped[base.integer(row["timestamp_ns"])].append(row)
    for timestamp in sorted(grouped):
        group = grouped[timestamp]
        for row in group:
            host = str(row.get("metadata_host") or "")
            program = str(row.get("token_program") or "")
            shape = (base.integer(row.get("max_buys_one_signature")), base.integer(row.get("create_signature_buys")))
            row.update({
                "prior_host_attempts": host_attempts[host] if host else 0,
                "prior_host_successes": host_successes[host] if host else 0,
                "prior_program_attempts": program_attempts[program] if program else 0,
                "prior_signature_shape_attempts": signature_shape_attempts[shape],
            })
        for row in group:
            if not row["positive"]:
                continue
            host = str(row.get("metadata_host") or "")
            program = str(row.get("token_program") or "")
            shape = (base.integer(row.get("max_buys_one_signature")), base.integer(row.get("create_signature_buys")))
            if host:
                host_attempts[host] += 1
                host_successes[host] += int(row.get("label") == "SUCCESS")
            if program:
                program_attempts[program] += 1
            signature_shape_attempts[shape] += 1


def extended_vector(row: Mapping[str, Any]) -> dict[str, float]:
    values = base.vector(row)
    seed = base.finite(row.get("creator_seed_sol"))
    outside = base.finite(row.get("outside_sol"))
    buyers = max(1.0, base.finite(row.get("unique_buyers")))
    common_seed = min(abs(seed - value) for value in (0.25, 0.5, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 5.0, 6.0, 8.0))
    values.update({
        "prior_host_log": base.log1p(row.get("prior_host_attempts")),
        "prior_host_success_log": base.log1p(row.get("prior_host_successes")),
        "prior_program_log": base.log1p(row.get("prior_program_attempts")),
        "prior_signature_shape_log": base.log1p(row.get("prior_signature_shape_attempts")),
        "seed_roundness": math.exp(-4.0 * common_seed),
        "outside_per_buyer": outside / buyers,
        "buyer_graph_density": base.finite(row.get("sum_prior_buyer_attempts")) / buyers,
        "buyer_success_density": base.finite(row.get("sum_prior_buyer_successes")) / buyers,
        "identity_strength": (
            1.5 * base.log1p(row.get("hist_wins"))
            + 1.25 * base.log1p(row.get("prior_creator_attempts"))
            + 1.0 * base.log1p(row.get("prior_handle_attempts"))
            + 1.0 * base.log1p(row.get("sum_prior_buyer_attempts"))
        ),
        "slot_cluster_strength": (
            base.finite(row.get("same_slot_unique"))
            + 0.5 * base.finite(row.get("same_slot_buys"))
            + 0.75 * base.finite(row.get("known_buyer_count"))
        ),
        "launch_velocity": (
            base.finite(row.get("buy_count")) + base.finite(row.get("unique_buyers"))
        ) / max(0.25, base.finite(row.get("age_ms")) / 1000.0),
        "seed_to_fdv": seed / max(1.0, base.finite(row.get("fdv_usd"))) * 10_000.0,
        "outside_to_fdv": outside / max(1.0, base.finite(row.get("fdv_usd"))) * 10_000.0,
        "no_public_buyers": float(base.integer(row.get("unique_buyers")) == 0),
        "one_public_buyer": float(base.integer(row.get("unique_buyers")) == 1),
        "two_plus_public_buyers": float(base.integer(row.get("unique_buyers")) >= 2),
        "very_early_50ms": float(base.finite(row.get("age_ms")) <= 50.0),
        "very_early_150ms": float(base.finite(row.get("age_ms")) <= 150.0),
        "very_early_400ms": float(base.finite(row.get("age_ms")) <= 400.0),
        "fdv_core_band": float(3_500.0 <= base.finite(row.get("fdv_usd")) <= 7_500.0),
    })
    return values


AUTHORITY = list(dict.fromkeys(base.AUTHORITY_FEATURES + [
    "prior_host_log", "prior_host_success_log", "seed_roundness", "identity_strength",
    "seed_to_fdv", "very_early_50ms", "very_early_150ms", "very_early_400ms", "fdv_core_band",
]))
CLUSTER = list(dict.fromkeys(base.CLUSTER_FEATURES + [
    "prior_signature_shape_log", "outside_per_buyer", "buyer_graph_density", "buyer_success_density",
    "identity_strength", "slot_cluster_strength", "launch_velocity", "seed_to_fdv", "outside_to_fdv",
    "no_public_buyers", "one_public_buyer", "two_plus_public_buyers", "very_early_50ms",
    "very_early_150ms", "very_early_400ms", "fdv_core_band",
]))
UNION = list(dict.fromkeys(AUTHORITY + CLUSTER + ["prior_program_log"]))


def matrix(rows: Sequence[Mapping[str, Any]], names: Sequence[str]) -> np.ndarray:
    return np.asarray([[extended_vector(row).get(name, 0.0) for name in names] for row in rows], dtype=float)


def choose_training(rows: list[dict[str, Any]], ratio: int = 15) -> list[dict[str, Any]]:
    positives = [row for row in rows if row["positive"] and base.eligible(row)]
    negatives = [row for row in rows if not row["positive"] and base.eligible(row)]
    negatives.sort(
        key=lambda row: (
            extended_vector(row)["identity_strength"]
            + extended_vector(row)["slot_cluster_strength"]
            + 0.5 * extended_vector(row)["launch_velocity"]
        ),
        reverse=True,
    )
    return positives + negatives[: max(750, len(positives) * ratio)]


def sample_weights(rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
    positives = sum(bool(row["positive"]) for row in rows)
    negatives = len(rows) - positives
    positive_weight = negatives / max(1, positives)
    return np.asarray([positive_weight if row["positive"] else 1.0 for row in rows], dtype=float)


@dataclass(frozen=True)
class ModelSpec:
    family: str
    depth: int
    leaf: int
    estimators: int = 160

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def fit_model(rows: list[dict[str, Any]], names: Sequence[str], spec: ModelSpec):
    chosen = choose_training(rows)
    x = matrix(chosen, names)
    y = np.asarray([int(row["positive"]) for row in chosen])
    weights = sample_weights(chosen)
    if spec.family == "logit":
        model = Pipeline([
            ("scale", StandardScaler()),
            ("model", LogisticRegression(C=0.4, class_weight="balanced", max_iter=5000, solver="liblinear")),
        ])
        model.fit(x, y)
        return model
    if spec.family == "extra":
        model = ExtraTreesClassifier(
            n_estimators=spec.estimators,
            max_depth=spec.depth,
            min_samples_leaf=spec.leaf,
            max_features="sqrt",
            class_weight="balanced_subsample",
            random_state=712,
            n_jobs=-1,
        )
        model.fit(x, y)
        return model
    if spec.family == "forest":
        model = RandomForestClassifier(
            n_estimators=spec.estimators,
            max_depth=spec.depth,
            min_samples_leaf=spec.leaf,
            max_features="sqrt",
            class_weight="balanced_subsample",
            random_state=712,
            n_jobs=-1,
        )
        model.fit(x, y)
        return model
    if spec.family == "hist":
        model = HistGradientBoostingClassifier(
            max_iter=180,
            learning_rate=0.05,
            max_depth=spec.depth,
            min_samples_leaf=spec.leaf,
            l2_regularization=2.0,
            random_state=712,
        )
        model.fit(x, y, sample_weight=weights)
        return model
    raise ValueError(spec.family)


def score(rows: list[dict[str, Any]], authority_model: Any, cluster_model: Any, union_model: Any) -> list[dict[str, Any]]:
    output = [dict(row) for row in rows]
    indices = [index for index, row in enumerate(output) if base.eligible(row)]
    if not indices:
        return output
    selected = [output[index] for index in indices]
    authority = authority_model.predict_proba(matrix(selected, AUTHORITY))[:, 1]
    cluster = cluster_model.predict_proba(matrix(selected, CLUSTER))[:, 1]
    union = union_model.predict_proba(matrix(selected, UNION))[:, 1]
    for index, a_value, c_value, u_value in zip(indices, authority, cluster, union):
        output[index]["authority_probability"] = float(a_value)
        output[index]["cluster_probability"] = float(c_value)
        output[index]["union_probability"] = float(u_value)
    return output


@dataclass(frozen=True)
class RankedGate:
    authority_threshold: float
    cluster_threshold: float
    union_threshold: float
    minimum_margin: float
    competition_window_ms: float
    top_k: int
    combination: str

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def combined_probability(row: Mapping[str, Any], combination: str) -> tuple[float, str]:
    authority = base.finite(row.get("authority_probability"), -1.0)
    cluster = base.finite(row.get("cluster_probability"), -1.0)
    union = base.finite(row.get("union_probability"), -1.0)
    if combination == "MAX":
        values = [(authority, "LAUNCH_AUTHORITY"), (cluster, "WALLET_CLUSTER"), (union, "UNION")]
        return max(values, key=lambda pair: pair[0])
    if combination == "BLEND":
        identity = max(authority, union)
        cluster_blend = max(cluster, union)
        if identity >= cluster_blend:
            return 0.55 * identity + 0.45 * union, "LAUNCH_AUTHORITY"
        return 0.55 * cluster_blend + 0.45 * union, "WALLET_CLUSTER"
    raise ValueError(combination)


def predict(scored: list[dict[str, Any]], gate: RankedGate) -> dict[str, dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for source in scored:
        a_value = base.finite(source.get("authority_probability"), -1.0)
        c_value = base.finite(source.get("cluster_probability"), -1.0)
        u_value = base.finite(source.get("union_probability"), -1.0)
        qualifies = a_value >= gate.authority_threshold or c_value >= gate.cluster_threshold or u_value >= gate.union_threshold
        if not qualifies:
            continue
        probability, mode = combined_probability(source, gate.combination)
        row = dict(source)
        row["probability"] = probability
        row["mode"] = mode
        candidates.append(row)

    candidates.sort(key=lambda row: (base.integer(row["run_index"]), base.integer(row["timestamp_ns"]), -base.finite(row["probability"])))
    window_ns = int(gate.competition_window_ms * 1e6)
    visible: dict[int, deque[dict[str, Any]]] = defaultdict(deque)
    accepted: dict[str, dict[str, Any]] = {}
    for row in candidates:
        run = base.integer(row["run_index"])
        timestamp = base.integer(row["timestamp_ns"])
        queue = visible[run]
        while queue and base.integer(queue[0]["timestamp_ns"]) < timestamp - window_ns:
            queue.popleft()
        scores = sorted([base.finite(item["probability"]) for item in queue] + [base.finite(row["probability"])], reverse=True)
        current = base.finite(row["probability"])
        rank = 1 + sum(value > current for value in scores)
        second = scores[1] if len(scores) > 1 else 0.0
        margin = current - second if rank == 1 else -1.0
        row["competition_rank"] = rank
        row["competition_margin"] = margin
        queue.append(row)
        if rank > gate.top_k:
            continue
        if rank == 1 and margin < gate.minimum_margin:
            continue
        mint = str(row["mint"])
        if mint not in accepted or timestamp < base.integer(accepted[mint]["timestamp_ns"]):
            accepted[mint] = row
    return accepted


def evaluate(rows: list[dict[str, Any]], predictions: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    return base.metrics(rows, predictions)


def candidate_specs() -> list[ModelSpec]:
    output = [ModelSpec("logit", 0, 0)]
    for family in ("extra", "forest"):
        for depth in (4, 6, 8, 10):
            for leaf in (2, 4, 8, 12):
                output.append(ModelSpec(family, depth, leaf))
    for depth in (3, 5, 7):
        for leaf in (8, 16, 24):
            output.append(ModelSpec("hist", depth, leaf))
    return output


def search(
    train: list[dict[str, Any]],
    validation: list[dict[str, Any]],
) -> tuple[ModelSpec, Any, Any, Any, RankedGate, dict[str, Any]]:
    best = None
    for spec in candidate_specs():
        authority_model = fit_model(train, AUTHORITY, spec)
        cluster_model = fit_model(train, CLUSTER, spec)
        union_model = fit_model(train, UNION, spec)
        scored = score(validation, authority_model, cluster_model, union_model)
        for threshold in (0.72, 0.80, 0.86, 0.90, 0.93, 0.95, 0.97, 0.985, 0.993):
            for split in (-0.04, 0.0, 0.04):
                authority_threshold = min(0.999, max(0.50, threshold + split))
                cluster_threshold = min(0.999, max(0.50, threshold - split))
                for union_delta in (0.0, 0.03, 0.06):
                    union_threshold = min(0.999, threshold + union_delta)
                    for margin in (0.0, 0.025, 0.05, 0.075, 0.10, 0.15):
                        for window in (100.0, 250.0, 500.0, 1000.0):
                            for top_k in (1, 2):
                                for combination in ("MAX", "BLEND"):
                                    gate = RankedGate(authority_threshold, cluster_threshold, union_threshold, margin, window, top_k, combination)
                                    result = evaluate(validation, predict(scored, gate))
                                    if result["true"] < 5 or result["recall"] < 0.10:
                                        continue
                                    valid = result["precision"] >= 0.60 and result["precision_wilson_low"] >= 0.32
                                    objective = (
                                        int(valid),
                                        result["precision_wilson_low"],
                                        result["precision"],
                                        result["recall"],
                                        result["true"],
                                        -result["false_positives"],
                                    )
                                    if best is None or objective > best[0]:
                                        best = (objective, spec, authority_model, cluster_model, union_model, gate, result)
        print(json.dumps({"evaluated_spec": spec.as_dict(), "best": best[0] if best else None}), flush=True)
    if best is None:
        raise RuntimeError("ranked search produced no viable validation rule")
    return best[1], best[2], best[3], best[4], best[5], best[6]


def tree_to_dict(model: DecisionTreeClassifier, names: Sequence[str]) -> dict[str, Any]:
    tree = model.tree_

    def node(index: int) -> dict[str, Any]:
        left = int(tree.children_left[index])
        right = int(tree.children_right[index])
        values = tree.value[index][0]
        probability = float(values[1] / max(1e-12, values.sum())) if len(values) > 1 else 0.0
        if left < 0 or right < 0:
            return {"leaf": True, "probability": probability, "samples": int(tree.n_node_samples[index])}
        return {
            "leaf": False,
            "feature": names[int(tree.feature[index])],
            "threshold": float(tree.threshold[index]),
            "probability": probability,
            "left": node(left),
            "right": node(right),
        }

    return node(0)


def score_tree(model: DecisionTreeClassifier, rows: list[dict[str, Any]], names: Sequence[str]) -> list[dict[str, Any]]:
    output = [dict(row) for row in rows]
    indices = [index for index, row in enumerate(output) if base.eligible(row)]
    if indices:
        values = model.predict_proba(matrix([output[index] for index in indices], names))[:, 1]
        for index, value in zip(indices, values):
            output[index]["authority_probability"] = float(value)
            output[index]["cluster_probability"] = float(value)
            output[index]["union_probability"] = float(value)
    return output


def distill(
    pre_live: list[dict[str, Any]],
    validation: list[dict[str, Any]],
    ensemble_scored_pre_live: list[dict[str, Any]],
    ensemble_predictions: Mapping[str, Mapping[str, Any]],
    ensemble_gate: RankedGate,
) -> tuple[DecisionTreeClassifier, RankedGate, dict[str, Any]]:
    selected_mints = set(ensemble_predictions)
    training = [row for row in ensemble_scored_pre_live if base.eligible(row)]
    x = matrix(training, UNION)
    y = np.asarray([int(str(row["mint"]) in selected_mints) for row in training])
    best = None
    for depth in (3, 4, 5, 6, 7, 8):
        for leaf in (2, 4, 6, 10, 15):
            if len(set(y)) < 2:
                continue
            model = DecisionTreeClassifier(
                max_depth=depth,
                min_samples_leaf=leaf,
                class_weight="balanced",
                random_state=712,
            )
            model.fit(x, y)
            for threshold in (0.50, 0.60, 0.70, 0.80, 0.90, 0.95, 0.98):
                gate = RankedGate(threshold, threshold, threshold, ensemble_gate.minimum_margin, ensemble_gate.competition_window_ms, ensemble_gate.top_k, "MAX")
                result = evaluate(validation, predict(score_tree(model, validation, UNION), gate))
                valid = result["true"] >= 5 and result["precision"] >= 0.55 and result["recall"] >= 0.10
                objective = (int(valid), result["precision_wilson_low"], result["precision"], result["recall"], result["true"])
                if best is None or objective > best[0]:
                    best = (objective, model, gate, result)
    if best is None:
        raise RuntimeError("could not distill ensemble")
    return best[1], best[2], best[3]


def main() -> int:
    parser = argparse.ArgumentParser(description="Ranked nonlinear and distilled rerun of the causal E4 entry thesis")
    parser.add_argument("--pair", action="append", default=[])
    parser.add_argument("--attempts", action="append", default=[], type=Path)
    parser.add_argument("--metadata-cache", action="append", default=[], type=Path)
    parser.add_argument("--creator-history", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-output", type=Path, required=True)
    parser.add_argument("--metadata-concurrency", type=int, default=96)
    parser.add_argument("--metadata-timeout", type=float, default=5.0)
    args = parser.parse_args()
    pairs = [base.parse_pair(value) for value in args.pair]
    if len(pairs) < 6:
        parser.error("at least six chronological samples are required")

    launches, run_ids = base.load_launches(pairs)
    failed = base.load_failed_attempts(args.attempts)
    rows = base.build_snapshots(launches, failed)
    cache = base.scan_metadata_cache(args.metadata_cache)
    relevant = {mint: launch for mint, launch in launches.items() if any(item["mint"] == mint and (item["positive"] or base.eligible(item)) for item in rows)}
    cache = asyncio.run(base.fill_metadata(relevant, cache, args.metadata_concurrency, args.metadata_timeout))
    base.add_metadata(rows, cache)
    base.add_history(rows, base.load_creator_history(args.creator_history))
    base.add_competition(rows)
    add_extended_history(rows)

    live_index = len(run_ids) - 1
    validation_start = max(3, live_index - 3)
    train = [row for row in rows if base.integer(row["run_index"]) < validation_start]
    validation = [row for row in rows if validation_start <= base.integer(row["run_index"]) < live_index]
    pre_live = [row for row in rows if base.integer(row["run_index"]) < live_index]
    live = [row for row in rows if base.integer(row["run_index"]) == live_index]

    spec, authority_seed, cluster_seed, union_seed, gate, validation_result = search(train, validation)
    authority = fit_model(pre_live, AUTHORITY, spec)
    cluster = fit_model(pre_live, CLUSTER, spec)
    union = fit_model(pre_live, UNION, spec)
    live_result = evaluate(live, predict(score(live, authority, cluster, union), gate))

    # Strict walk-forward audit.
    folds = []
    total_true = total_predictions = total_positives = 0
    for fold in range(max(3, validation_start), len(run_ids)):
        fold_train = [row for row in rows if base.integer(row["run_index"]) < fold]
        fold_rows = [row for row in rows if base.integer(row["run_index"]) == fold]
        fa = fit_model(fold_train, AUTHORITY, spec)
        fc = fit_model(fold_train, CLUSTER, spec)
        fu = fit_model(fold_train, UNION, spec)
        result = evaluate(fold_rows, predict(score(fold_rows, fa, fc, fu), gate))
        result["run_id"] = run_ids[fold]
        folds.append(result)
        total_true += base.integer(result["true"])
        total_predictions += base.integer(result["predictions"])
        total_positives += base.integer(result["positives"])
    walk = {
        "folds": folds,
        "true": total_true,
        "predictions": total_predictions,
        "positives": total_positives,
        "precision": total_true / total_predictions if total_predictions else 0.0,
        "precision_wilson_low": base.wilson_lower(total_true, total_predictions),
        "recall": total_true / total_positives if total_positives else 0.0,
    }

    ensemble_scored_pre_live = score(pre_live, authority, cluster, union)
    ensemble_predictions_pre_live = predict(ensemble_scored_pre_live, gate)
    tree, tree_gate, distilled_validation = distill(pre_live, validation, ensemble_scored_pre_live, ensemble_predictions_pre_live, gate)
    distilled_live = evaluate(live, predict(score_tree(tree, live, UNION), tree_gate))

    ensemble_pass = bool(
        validation_result["precision"] >= 0.60
        and validation_result["recall"] >= 0.10
        and validation_result["true"] >= 5
        and walk["precision"] >= 0.55
        and walk["precision_wilson_low"] >= 0.30
        and walk["true"] >= 10
        and live_result["precision"] >= 0.50
        and live_result["recall"] >= 0.10
        and live_result["true"] >= 2
        and live_result["all_true_pre_intent"]
    )
    distilled_pass = bool(
        distilled_validation["precision"] >= 0.55
        and distilled_validation["recall"] >= 0.10
        and distilled_validation["true"] >= 5
        and distilled_live["precision"] >= 0.50
        and distilled_live["recall"] >= 0.10
        and distilled_live["true"] >= 2
        and distilled_live["all_true_pre_intent"]
    )
    status = "LIVE_HOLDOUT_CONFIRMED" if ensemble_pass and distilled_pass else "NOT_CONCLUSIVE"

    model_payload = {
        "version": "e4-v12-ranked-multimode-tree-v1",
        "status": status,
        "thesis": (
            "Within each short launch competition window, enter only the top unsold low-FDV candidate whose score is supported "
            "by either creator/social authority or recurrence of first-slot wallets from E4's prior intent graph."
        ),
        "guardrails": {
            "minimum_creator_seed_sol": 0.20,
            "minimum_fdv_usd": 2750.0,
            "maximum_fdv_usd": 10000.0,
            "maximum_age_ms": 1500.0,
            "pre_entry_sell_count": 0,
            "mayhem_allowed": False,
        },
        "source_model": {"spec": spec.as_dict(), "gate": gate.as_dict()},
        "runtime_gate": tree_gate.as_dict(),
        "runtime_features": UNION,
        "runtime_tree": tree_to_dict(tree, UNION),
        "validation": distilled_validation,
        "live_holdout": distilled_live,
        "ensemble_validation": validation_result,
        "ensemble_walk_forward": walk,
        "ensemble_live_holdout": live_result,
        "training_runs": run_ids[:live_index],
        "live_run": run_ids[live_index],
    }
    report = {
        "version": "e4-v12-ranked-multimode-search-v1",
        "status": status,
        "coverage": {
            "runs": run_ids,
            "launches": len(launches),
            "rows": len(rows),
            "positive_intents": sum(row["positive"] and base.eligible(row) for row in rows),
            "true_ignores": sum((not row["positive"]) and base.eligible(row) for row in rows),
        },
        "causality": {
            "labels": "successful fills plus mapped failed E4 buys versus launches with no observed E4 intent",
            "snapshots": "strictly before the first E4 attempt",
            "identity_memory": "only prior chronological E4 attempts",
            "live_holdout": run_ids[live_index],
        },
        "thesis": model_payload["thesis"],
        "ensemble": {
            "spec": spec.as_dict(),
            "gate": gate.as_dict(),
            "validation": validation_result,
            "walk_forward": walk,
            "live_holdout": live_result,
            "passed": ensemble_pass,
        },
        "distilled_runtime": {
            "gate": tree_gate.as_dict(),
            "validation": distilled_validation,
            "live_holdout": distilled_live,
            "passed": distilled_pass,
        },
        "safe_to_implement": status == "LIVE_HOLDOUT_CONFIRMED",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.model_output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    args.model_output.write_text(json.dumps(model_payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "status": status,
        "spec": spec.as_dict(),
        "gate": gate.as_dict(),
        "ensemble_validation": {key: validation_result[key] for key in ("positives", "predictions", "true", "precision", "recall")},
        "ensemble_walk": {key: walk[key] for key in ("positives", "predictions", "true", "precision", "precision_wilson_low", "recall")},
        "ensemble_live": {key: live_result[key] for key in ("positives", "predictions", "true", "precision", "recall", "modes")},
        "distilled_validation": {key: distilled_validation[key] for key in ("predictions", "true", "precision", "recall")},
        "distilled_live": {key: distilled_live[key] for key in ("predictions", "true", "precision", "recall", "modes")},
    }, indent=2, sort_keys=True), flush=True)
    return 0 if status == "LIVE_HOLDOUT_CONFIRMED" else 4


if __name__ == "__main__":
    raise SystemExit(main())

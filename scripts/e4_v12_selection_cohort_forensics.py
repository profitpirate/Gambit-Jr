#!/usr/bin/env python3
"""Explain E4's causal launch selection and benchmark compact scorers."""

from __future__ import annotations

import argparse
import bisect
import json
import math
import re
import statistics
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import e4_v12_wallet_selection_model as wallet
import numpy as np
from sklearn.ensemble import ExtraTreesClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.preprocessing import StandardScaler
from sklearn.tree import DecisionTreeClassifier, export_text

SCHEMA_VERSION = "e4-v12-selection-cohort-forensics-v1"
TOKEN_RE = re.compile(r"[a-z0-9]{2,}")
COUNT_FEATURES = {
    "creator_prior_launches",
    "creator_prior_e4_attempts",
    "creator_prior_e4_landed",
    "creator_prior_e4_failed",
    "launches_previous_1s",
    "launches_previous_10s",
}
CATEGORICAL_FEATURES = (
    "mayhem_mode",
    "cashback_enabled",
    "metadata_content_addressed",
)
EXPLANATORY_FEATURES = (
    "creator_seed_sol",
    "creator_buy_count",
    "public_buy_sol",
    "public_buy_count",
    "unique_public_buyers",
    "public_signature_count",
    "maximum_public_buy_sol",
    "median_public_buy_sol",
    "buyer_concentration",
    "same_create_signature_buyers",
    "same_create_slot_buyers",
    "creator_prior_launches",
    "creator_prior_e4_attempts",
    "creator_prior_e4_landed",
    "creator_prior_e4_failed",
    "milliseconds_since_creator_launch",
    "launches_previous_1s",
    "launches_previous_10s",
    "name_length",
    "symbol_length",
    "metadata_content_addressed",
    "mayhem_mode",
    "cashback_enabled",
)
FORMULA_FEATURE_NAMES = (
    "known_creator_1plus",
    "known_creator_2plus",
    "creator_seed_1plus",
    "creator_seed_3plus",
    "creator_seed_5plus",
    "public_buyers_1plus_5ms",
    "public_buyers_2plus_5ms",
    "public_buyers_3plus_5ms",
    "public_sol_1plus_5ms",
    "public_sol_3plus_5ms",
    "public_sol_5plus_5ms",
    "cashback_enabled",
    "metadata_not_content_addressed",
    "creator_prior_launches_4orless",
    "creator_gap_300s_plus",
    "mayhem_mode",
)
BUYER_FEATURE_NAMES = (
    "known_selected_buyer_count",
    "buyer_prior_selected_sum",
    "buyer_prior_selected_max",
    "creator_buyer_pair_prior_max",
    "exact_buyer_cluster_prior_count",
    "buyer_prior_launch_sum",
)


def finite(value: Any) -> float:
    return wallet.finite(value)


def feature(row: wallet.LaunchRow, name: str) -> float:
    return row.features[wallet.FEATURE_NAMES.index(name)]


def quantiles(values: Sequence[float]) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "p25": 0.0, "median": 0.0, "p75": 0.0}
    array = np.asarray(values, dtype=float)
    return {
        "mean": float(np.mean(array)),
        "p25": float(np.quantile(array, 0.25)),
        "median": float(np.median(array)),
        "p75": float(np.quantile(array, 0.75)),
    }


def cohort_features(rows: Sequence[wallet.LaunchRow], horizon_ms: int) -> dict[str, Any]:
    rows = [row for row in rows if row.horizon_ms == horizon_ms]
    selected = [row for row in rows if row.selected]
    negative = [row for row in rows if not row.selected]
    output = []
    for name in EXPLANATORY_FEATURES:
        positive_values = [feature(row, name) for row in selected]
        negative_values = [feature(row, name) for row in negative]
        pooled_std = float(np.std([*positive_values, *negative_values])) or 1.0
        output.append(
            {
                "feature": name,
                "selected": quantiles(positive_values),
                "not_selected": quantiles(negative_values),
                "standardised_mean_difference": (
                    statistics.fmean(positive_values) - statistics.fmean(negative_values)
                )
                / pooled_std,
            }
        )
    output.sort(key=lambda item: abs(item["standardised_mean_difference"]), reverse=True)
    return {
        "horizon_ms": horizon_ms,
        "launches": len(rows),
        "selected": len(selected),
        "base_rate": len(selected) / max(len(rows), 1),
        "features": output,
    }


def recurrence_bins(rows: Sequence[wallet.LaunchRow], horizon_ms: int) -> dict[str, Any]:
    unique = [row for row in rows if row.horizon_ms == horizon_ms]

    def label_attempts(value: float) -> str:
        if value == 0:
            return "0"
        if value == 1:
            return "1"
        return "2+"

    def label_launches(value: float) -> str:
        if value == 0:
            return "0"
        if value == 1:
            return "1"
        if value < 5:
            return "2-4"
        return "5+"

    output = {}
    for name, labeller in (
        ("creator_prior_e4_attempts", label_attempts),
        ("creator_prior_launches", label_launches),
    ):
        grouped: dict[str, list[wallet.LaunchRow]] = defaultdict(list)
        for row in unique:
            grouped[labeller(feature(row, name))].append(row)
        output[name] = {
            key: {
                "launches": len(group),
                "selected": sum(row.selected for row in group),
                "selection_rate": sum(row.selected for row in group) / max(len(group), 1),
                "lift_over_base": (
                    (sum(row.selected for row in group) / max(len(group), 1))
                    / (sum(row.selected for row in unique) / max(len(unique), 1))
                ),
            }
            for key, group in sorted(grouped.items())
        }
    return output


def categorical_lift(rows: Sequence[wallet.LaunchRow], horizon_ms: int) -> dict[str, Any]:
    unique = [row for row in rows if row.horizon_ms == horizon_ms]
    base_rate = sum(row.selected for row in unique) / max(len(unique), 1)
    output = {}
    for name in CATEGORICAL_FEATURES:
        present = [row for row in unique if feature(row, name) >= 0.5]
        absent = [row for row in unique if feature(row, name) < 0.5]
        output[name] = {
            "present": {
                "launches": len(present),
                "selected": sum(row.selected for row in present),
                "selection_rate": sum(row.selected for row in present) / max(len(present), 1),
                "lift": (sum(row.selected for row in present) / max(len(present), 1))
                / max(base_rate, 1e-12),
            },
            "absent": {
                "launches": len(absent),
                "selected": sum(row.selected for row in absent),
                "selection_rate": sum(row.selected for row in absent) / max(len(absent), 1),
            },
        }
    combinations: Counter[tuple[int, int, int, bool]] = Counter()
    totals: Counter[tuple[int, int, int]] = Counter()
    for row in unique:
        key = tuple(int(feature(row, name) >= 0.5) for name in CATEGORICAL_FEATURES)
        totals[key] += 1
        combinations[(*key, row.selected)] += 1
    output["combinations"] = [
        {
            "mayhem": bool(key[0]),
            "cashback": bool(key[1]),
            "content_addressed": bool(key[2]),
            "launches": total,
            "selected": combinations[(*key, True)],
            "selection_rate": combinations[(*key, True)] / max(total, 1),
            "lift": (combinations[(*key, True)] / max(total, 1)) / max(base_rate, 1e-12),
        }
        for key, total in totals.most_common()
    ]
    return output


def developer_recurrence(
    rows: Sequence[wallet.LaunchRow],
    traces: Mapping[tuple[str, str], wallet.base.Trace],
    horizon_ms: int,
) -> dict[str, Any]:
    unique = [row for row in rows if row.horizon_ms == horizon_ms]
    selected = [row for row in unique if row.selected]
    selected_counts: Counter[str] = Counter(
        traces[(row.run_id, row.mint)].creator for row in selected
    )
    all_counts: Counter[str] = Counter(traces[(row.run_id, row.mint)].creator for row in unique)
    repeat_selected = sum(count > 1 for count in selected_counts.values())
    attempts_from_repeat = sum(count for count in selected_counts.values() if count > 1)
    top = []
    for creator, count in selected_counts.most_common(25):
        total = all_counts[creator]
        top.append(
            {
                "creator": creator,
                "selected": count,
                "launches": total,
                "selection_rate": count / total,
            }
        )
    return {
        "selected_attempts": len(selected),
        "unique_selected_creators": len(selected_counts),
        "creators_selected_more_than_once": repeat_selected,
        "selected_attempts_from_repeat_creators": attempts_from_repeat,
        "repeat_attempt_share": attempts_from_repeat / max(len(selected), 1),
        "top_creators": top,
    }


def matched_same_time(rows: Sequence[wallet.LaunchRow], horizon_ms: int) -> dict[str, Any]:
    unique = [row for row in rows if row.horizon_ms == horizon_ms]
    by_run: dict[str, list[wallet.LaunchRow]] = defaultdict(list)
    for row in unique:
        by_run[row.run_id].append(row)
    pairs = []
    for run_rows in by_run.values():
        run_rows.sort(key=lambda row: (row.create_ns, row.mint))
        times = [row.create_ns for row in run_rows]
        for positive in (row for row in run_rows if row.selected):
            left = bisect.bisect_left(times, positive.create_ns - 500_000_000)
            right = bisect.bisect_right(times, positive.create_ns + 500_000_000)
            negatives = [row for row in run_rows[left:right] if not row.selected]
            negatives.sort(key=lambda row: abs(row.create_ns - positive.create_ns))
            for negative in negatives[:5]:
                pairs.append((positive, negative))
    comparisons = []
    for name in EXPLANATORY_FEATURES:
        deltas = [feature(positive, name) - feature(negative, name) for positive, negative in pairs]
        comparisons.append(
            {
                "feature": name,
                "pairs": len(deltas),
                "positive_greater_fraction": sum(value > 0 for value in deltas)
                / max(len(deltas), 1),
                "equal_fraction": sum(value == 0 for value in deltas) / max(len(deltas), 1),
                "median_delta": float(np.median(deltas)) if deltas else 0.0,
                "mean_delta": statistics.fmean(deltas) if deltas else 0.0,
            }
        )
    comparisons.sort(
        key=lambda item: abs(item["positive_greater_fraction"] - 0.5), reverse=True
    )
    return {
        "window_ms": 500,
        "negative_matches_per_positive": 5,
        "pairs": len(pairs),
        "comparisons": comparisons,
    }


def token_lift(
    root: Path,
    rows: Sequence[wallet.LaunchRow],
    horizon_ms: int,
) -> list[dict[str, Any]]:
    selected_keys = {
        (row.run_id, row.mint)
        for row in rows
        if row.horizon_ms == horizon_ms and row.selected
    }
    all_keys = {
        (row.run_id, row.mint) for row in rows if row.horizon_ms == horizon_ms
    }
    selected_counter: Counter[str] = Counter()
    all_counter: Counter[str] = Counter()
    for spec in wallet.base.capture_specs(root, include_holdout=True):
        creates = wallet.load_create_rows(spec.events_path)
        for mint, row in creates.items():
            key = (spec.run_id, mint)
            if key not in all_keys:
                continue
            raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
            name = str(raw.get("name") or row.get("name") or "")
            symbol = str(raw.get("symbol") or row.get("symbol") or "")
            tokens = set(TOKEN_RE.findall(f"{name} {symbol}".lower()))
            all_counter.update(tokens)
            if key in selected_keys:
                selected_counter.update(tokens)
    selected_total = len(selected_keys)
    all_total = len(all_keys)
    result = []
    for token, selected_count in selected_counter.items():
        if selected_count < 3:
            continue
        all_count = all_counter[token]
        selected_rate = (selected_count + 1) / (selected_total + 2)
        population_rate = (all_count + 1) / (all_total + 2)
        result.append(
            {
                "token": token,
                "selected_launches": selected_count,
                "all_launches": all_count,
                "smoothed_lift": selected_rate / population_rate,
            }
        )
    result.sort(key=lambda item: (item["smoothed_lift"], item["selected_launches"]), reverse=True)
    return result[:50]


def model_metrics(
    rows: Sequence[wallet.LaunchRow], scores: np.ndarray, threshold: float
) -> dict[str, Any]:
    truth = np.asarray([row.selected for row in rows], dtype=bool)
    predicted = scores >= threshold
    true_positive = int(np.sum(truth & predicted))
    return {
        "launches": len(rows),
        "attempts": int(truth.sum()),
        "predicted": int(predicted.sum()),
        "true_positive": true_positive,
        "precision": true_positive / max(int(predicted.sum()), 1),
        "recall": true_positive / max(int(truth.sum()), 1),
        "average_precision": float(average_precision_score(truth, scores)),
        "roc_auc": float(roc_auc_score(truth, scores)),
    }


def speed_benchmark(model: Any, transform: Any, sample: np.ndarray) -> dict[str, float]:
    def score() -> None:
        matrix = sample
        if transform is not None:
            matrix = transform.transform(matrix)
        model.predict_proba(matrix)

    for _ in range(100):
        score()
    timings = []
    for _ in range(2_000):
        started = time.perf_counter_ns()
        score()
        timings.append(time.perf_counter_ns() - started)
    return {
        "median_microseconds": statistics.median(timings) / 1_000,
        "p95_microseconds": float(np.quantile(timings, 0.95)) / 1_000,
        "p99_microseconds": float(np.quantile(timings, 0.99)) / 1_000,
    }


def compact_model_frontier(rows: Sequence[wallet.LaunchRow]) -> dict[str, Any]:
    train = [row for row in rows if row.split == "train" and row.horizon_ms == 0]
    validation = [row for row in rows if row.split == "validation" and row.horizon_ms == 0]
    holdout = [row for row in rows if row.split == "holdout" and row.horizon_ms == 0]
    x_train = wallet.matrix(train)
    y_train = np.asarray([row.selected for row in train], dtype=int)
    x_validation = wallet.matrix(validation)
    scaler = StandardScaler().fit(x_train)
    families: dict[str, tuple[Any, Any]] = {
        "logistic": (
            LogisticRegression(
                C=0.1,
                class_weight="balanced",
                max_iter=2_000,
                random_state=wallet.RANDOM_SEED,
            ).fit(scaler.transform(x_train), y_train),
            scaler,
        ),
        "decision_tree_depth_4": (
            DecisionTreeClassifier(
                max_depth=4,
                min_samples_leaf=30,
                class_weight="balanced",
                random_state=wallet.RANDOM_SEED,
            ).fit(x_train, y_train),
            None,
        ),
        "decision_tree_depth_6": (
            DecisionTreeClassifier(
                max_depth=6,
                min_samples_leaf=20,
                class_weight="balanced",
                random_state=wallet.RANDOM_SEED,
            ).fit(x_train, y_train),
            None,
        ),
        "extra_trees_32": (
            ExtraTreesClassifier(
                n_estimators=32,
                min_samples_leaf=8,
                max_features=0.7,
                class_weight="balanced",
                n_jobs=1,
                random_state=wallet.RANDOM_SEED,
            ).fit(x_train, y_train),
            None,
        ),
        "extra_trees_64": (
            ExtraTreesClassifier(
                n_estimators=64,
                min_samples_leaf=8,
                max_features=0.7,
                class_weight="balanced",
                n_jobs=1,
                random_state=wallet.RANDOM_SEED,
            ).fit(x_train, y_train),
            None,
        ),
    }
    results = []
    for name, (model, transform) in families.items():
        validation_matrix = x_validation
        if transform is not None:
            validation_matrix = transform.transform(validation_matrix)
        validation_scores = model.predict_proba(validation_matrix)[:, 1]
        ordered = sorted(validation_scores, reverse=True)
        threshold = float(ordered[min(79, len(ordered) - 1)])
        holdout_matrix = wallet.matrix(holdout)
        if transform is not None:
            holdout_matrix = transform.transform(holdout_matrix)
        holdout_scores = model.predict_proba(holdout_matrix)[:, 1]
        record = {
            "family": name,
            "threshold": threshold,
            "validation": model_metrics(validation, validation_scores, threshold),
            "holdout_exploratory": model_metrics(holdout, holdout_scores, threshold),
            "single_row_scoring": speed_benchmark(model, transform, x_validation[:1]),
        }
        if name == "logistic":
            coefficients = sorted(
                zip(wallet.FEATURE_NAMES, model.coef_[0]),
                key=lambda item: abs(item[1]),
                reverse=True,
            )
            record["formula_terms"] = [
                {"feature": feature_name, "coefficient": float(coefficient)}
                for feature_name, coefficient in coefficients[:25]
            ]
            record["intercept"] = float(model.intercept_[0])
        if name == "decision_tree_depth_4":
            record["rules"] = export_text(model, feature_names=list(wallet.FEATURE_NAMES))
        results.append(record)
    results.sort(
        key=lambda item: (
            item["validation"]["average_precision"],
            -item["single_row_scoring"]["median_microseconds"],
        ),
        reverse=True,
    )
    return {
        "selection_used_holdout": False,
        "holdout_is_exploratory_because_prior_experiment_opened_it": True,
        "target_validation_predictions": 80,
        "models": results,
    }


def formula_vector(row: wallet.LaunchRow) -> tuple[float, ...]:
    attempts = feature(row, "creator_prior_e4_attempts")
    seed = feature(row, "creator_seed_sol")
    buyers = feature(row, "unique_public_buyers")
    public_sol = feature(row, "public_buy_sol")
    return (
        float(attempts >= 1),
        float(attempts >= 2),
        float(seed >= 1),
        float(seed >= 3),
        float(seed >= 5),
        float(buyers >= 1),
        float(buyers >= 2),
        float(buyers >= 3),
        float(public_sol >= 1),
        float(public_sol >= 3),
        float(public_sol >= 5),
        float(feature(row, "cashback_enabled") >= 0.5),
        float(feature(row, "metadata_content_addressed") < 0.5),
        float(feature(row, "creator_prior_launches") <= 4),
        float(feature(row, "milliseconds_since_creator_launch") >= 300_000),
        float(feature(row, "mayhem_mode") >= 0.5),
    )


def transparent_formula(rows: Sequence[wallet.LaunchRow]) -> dict[str, Any]:
    train = [row for row in rows if row.split == "train" and row.horizon_ms == 5]
    validation = [row for row in rows if row.split == "validation" and row.horizon_ms == 5]
    holdout = [row for row in rows if row.split == "holdout" and row.horizon_ms == 5]
    x_train = np.asarray([formula_vector(row) for row in train], dtype=float)
    model = LogisticRegression(
        C=0.2,
        class_weight="balanced",
        max_iter=2_000,
        random_state=wallet.RANDOM_SEED,
    ).fit(x_train, [row.selected for row in train])

    def scores(group: Sequence[wallet.LaunchRow]) -> np.ndarray:
        return model.predict_proba(
            np.asarray([formula_vector(row) for row in group], dtype=float)
        )[:, 1]

    validation_scores = scores(validation)
    ordered = sorted(validation_scores, reverse=True)
    candidates = []
    for target in (80, 120, 200, 300, 500):
        threshold = float(ordered[min(target - 1, len(ordered) - 1)])
        metrics = model_metrics(validation, validation_scores, threshold)
        precision = metrics["precision"]
        recall = metrics["recall"]
        f_half = 1.25 * precision * recall / max(0.25 * precision + recall, 1e-12)
        candidates.append((f_half, precision, recall, -target, threshold, metrics))
    _, _, _, _, threshold, validation_metrics = max(candidates)
    holdout_scores = scores(holdout)
    coefficients = [float(value) for value in model.coef_[0]]
    intercept = float(model.intercept_[0])

    def direct_score(vector: Sequence[float]) -> float:
        logit = intercept + sum(
            coefficient * value for coefficient, value in zip(coefficients, vector)
        )
        return 1.0 / (1.0 + math.exp(-max(min(logit, 40.0), -40.0)))

    sample = formula_vector(validation[0])
    timings = []
    for _ in range(10_000):
        started = time.perf_counter_ns()
        direct_score(sample)
        timings.append(time.perf_counter_ns() - started)
    terms = sorted(
        zip(FORMULA_FEATURE_NAMES, coefficients),
        key=lambda item: abs(item[1]),
        reverse=True,
    )
    return {
        "horizon_ms": 5,
        "selection_used_holdout": False,
        "threshold": threshold,
        "intercept": intercept,
        "terms": [
            {"feature": name, "coefficient": coefficient} for name, coefficient in terms
        ],
        "formula": (
            "sigmoid(intercept + sum(coefficient_i * boolean_feature_i)); "
            "select when probability >= threshold"
        ),
        "validation": validation_metrics,
        "holdout_exploratory": model_metrics(holdout, holdout_scores, threshold),
        "direct_formula_speed": {
            "median_microseconds": statistics.median(timings) / 1_000,
            "p95_microseconds": float(np.quantile(timings, 0.95)) / 1_000,
            "p99_microseconds": float(np.quantile(timings, 0.99)) / 1_000,
        },
    }


def causal_buyer_features(
    rows: Sequence[wallet.LaunchRow],
    traces: Mapping[tuple[str, str], wallet.base.Trace],
) -> tuple[dict[tuple[str, str], tuple[float, ...]], dict[str, Any]]:
    unique = [row for row in rows if row.horizon_ms == 5]
    unique.sort(key=lambda row: (row.create_ns, row.run_id, row.mint))
    buyer_selected: Counter[str] = Counter()
    buyer_launches: Counter[str] = Counter()
    pair_selected: Counter[tuple[str, str]] = Counter()
    cluster_selected: Counter[tuple[str, ...]] = Counter()
    features: dict[tuple[str, str], tuple[float, ...]] = {}
    selected_by_buyer: Counter[str] = Counter()
    launches_by_buyer: Counter[str] = Counter()
    selected_with_known_buyer = 0
    for row in unique:
        trace = traces[(row.run_id, row.mint)]
        cutoff = trace.create_ns + 5_000_000
        buyers = []
        seen = set()
        for point in trace.points:
            if point.timestamp_ns > cutoff:
                break
            if (
                point.kind in wallet.base.choice_sets.BUY_KINDS
                and point.trader
                and point.trader not in {trace.creator, wallet.E4_WALLET}
                and point.trader not in seen
            ):
                buyers.append(point.trader)
                seen.add(point.trader)
        cluster = tuple(buyers[:3])
        values = (
            float(sum(buyer_selected[buyer] > 0 for buyer in buyers)),
            float(sum(buyer_selected[buyer] for buyer in buyers)),
            float(max((buyer_selected[buyer] for buyer in buyers), default=0)),
            float(
                max(
                    (pair_selected[(trace.creator, buyer)] for buyer in buyers),
                    default=0,
                )
            ),
            float(cluster_selected[cluster] if cluster else 0),
            float(sum(buyer_launches[buyer] for buyer in buyers)),
        )
        features[(row.run_id, row.mint)] = values
        if row.selected and values[0] > 0:
            selected_with_known_buyer += 1
        for buyer in buyers:
            launches_by_buyer[buyer] += 1
            buyer_launches[buyer] += 1
            if row.selected:
                selected_by_buyer[buyer] += 1
                buyer_selected[buyer] += 1
                pair_selected[(trace.creator, buyer)] += 1
        if row.selected and cluster:
            cluster_selected[cluster] += 1
    base_rate = sum(row.selected for row in unique) / max(len(unique), 1)
    bins: dict[str, list[wallet.LaunchRow]] = defaultdict(list)
    for row in unique:
        count = features[(row.run_id, row.mint)][0]
        bins["0" if count == 0 else "1" if count == 1 else "2+"].append(row)
    top_buyers = []
    for buyer, selected_count in selected_by_buyer.most_common(30):
        total = launches_by_buyer[buyer]
        if selected_count < 2:
            continue
        top_buyers.append(
            {
                "buyer": buyer,
                "selected_launches": selected_count,
                "early_launches": total,
                "selection_rate": selected_count / total,
                "lift": (selected_count / total) / max(base_rate, 1e-12),
            }
        )
    audit = {
        "horizon_ms": 5,
        "selected_attempts": sum(row.selected for row in unique),
        "selected_with_previously_selected_early_buyer": selected_with_known_buyer,
        "selected_with_known_buyer_share": selected_with_known_buyer
        / max(sum(row.selected for row in unique), 1),
        "known_buyer_bins": {
            key: {
                "launches": len(group),
                "selected": sum(row.selected for row in group),
                "selection_rate": sum(row.selected for row in group) / max(len(group), 1),
                "lift": (
                    sum(row.selected for row in group) / max(len(group), 1)
                )
                / max(base_rate, 1e-12),
            }
            for key, group in sorted(bins.items())
        },
        "top_recurring_early_buyers": top_buyers,
    }
    return features, audit


def buyer_augmented_frontier(
    rows: Sequence[wallet.LaunchRow],
    buyer_features: Mapping[tuple[str, str], tuple[float, ...]],
) -> dict[str, Any]:
    unique = [row for row in rows if row.horizon_ms == 5]

    def vector(row: wallet.LaunchRow) -> tuple[float, ...]:
        return (*formula_vector(row), *buyer_features[(row.run_id, row.mint)])

    train = [row for row in unique if row.split == "train"]
    validation = [row for row in unique if row.split == "validation"]
    holdout = [row for row in unique if row.split == "holdout"]
    x_train = np.asarray([vector(row) for row in train], dtype=float)
    y_train = np.asarray([row.selected for row in train], dtype=int)
    models = {
        "buyer_logistic": LogisticRegression(
            C=0.2,
            class_weight="balanced",
            max_iter=2_000,
            random_state=wallet.RANDOM_SEED,
        ).fit(x_train, y_train),
        "buyer_tree_depth_6": DecisionTreeClassifier(
            max_depth=6,
            min_samples_leaf=20,
            class_weight="balanced",
            random_state=wallet.RANDOM_SEED,
        ).fit(x_train, y_train),
        "buyer_extra_trees_32": ExtraTreesClassifier(
            n_estimators=32,
            min_samples_leaf=8,
            max_features=0.8,
            class_weight="balanced",
            n_jobs=1,
            random_state=wallet.RANDOM_SEED,
        ).fit(x_train, y_train),
    }
    output = []
    for family, model in models.items():
        validation_scores = model.predict_proba(
            np.asarray([vector(row) for row in validation], dtype=float)
        )[:, 1]
        ordered = sorted(validation_scores, reverse=True)
        candidates = []
        for target in (80, 120, 200, 300, 500):
            threshold = float(ordered[min(target - 1, len(ordered) - 1)])
            metrics = model_metrics(validation, validation_scores, threshold)
            precision = metrics["precision"]
            recall = metrics["recall"]
            f_half = 1.25 * precision * recall / max(0.25 * precision + recall, 1e-12)
            candidates.append((f_half, -target, threshold, metrics))
        _, _, threshold, validation_metrics = max(candidates)
        holdout_scores = model.predict_proba(
            np.asarray([vector(row) for row in holdout], dtype=float)
        )[:, 1]
        record = {
            "family": family,
            "threshold": threshold,
            "validation": validation_metrics,
            "holdout_exploratory": model_metrics(holdout, holdout_scores, threshold),
        }
        if family == "buyer_logistic":
            names = (*FORMULA_FEATURE_NAMES, *BUYER_FEATURE_NAMES)
            record["terms"] = [
                {"feature": name, "coefficient": float(coefficient)}
                for name, coefficient in sorted(
                    zip(names, model.coef_[0]),
                    key=lambda item: abs(item[1]),
                    reverse=True,
                )
            ]
        output.append(record)
    output.sort(key=lambda row: row["validation"]["average_precision"], reverse=True)
    return {
        "selection_used_holdout": False,
        "features": [*FORMULA_FEATURE_NAMES, *BUYER_FEATURE_NAMES],
        "models": output,
    }


def _rule_metrics(rows: Sequence[wallet.LaunchRow], selected: set[tuple[str, str]]) -> dict[str, Any]:
    positives = sum(row.selected for row in rows)
    predicted = [row for row in rows if (row.run_id, row.mint) in selected]
    true_positive = sum(row.selected for row in predicted)
    return {
        "launches": len(rows),
        "attempts": positives,
        "predicted": len(predicted),
        "true_positive": true_positive,
        "precision": true_positive / max(len(predicted), 1),
        "recall": true_positive / max(positives, 1),
    }


def transparent_network_rules(
    rows: Sequence[wallet.LaunchRow],
    buyer_features: Mapping[tuple[str, str], tuple[float, ...]],
) -> dict[str, Any]:
    """Evaluate predeclared O(1) identity/network gates without fitting holdout."""
    unique = [row for row in rows if row.horizon_ms == 5]

    def values(row: wallet.LaunchRow) -> dict[str, float]:
        buyer = buyer_features[(row.run_id, row.mint)]
        return {
            "prior": feature(row, "creator_prior_e4_attempts"),
            "seed": feature(row, "creator_seed_sol"),
            "buyers": buyer[0],
            "public_sol": feature(row, "public_buy_sol"),
            "mayhem": feature(row, "mayhem_mode"),
        }

    predicates = {
        "repeat_creator": lambda value: value["prior"] >= 1 and value["mayhem"] < 0.5,
        "strong_repeat_creator": lambda value: (
            value["prior"] >= 2 and value["seed"] >= 1 and value["mayhem"] < 0.5
        ),
        "two_known_buyers": lambda value: (
            value["buyers"] >= 2 and value["seed"] >= 1 and value["mayhem"] < 0.5
        ),
        "creator_and_known_buyer": lambda value: (
            value["prior"] >= 1 and value["buyers"] >= 1 and value["mayhem"] < 0.5
        ),
        "repeat_creator_or_two_known_buyers": lambda value: (
            (value["prior"] >= 1 or value["buyers"] >= 2)
            and value["seed"] >= 1
            and value["mayhem"] < 0.5
        ),
        "cold_three_buyer_flow": lambda value: (
            value["prior"] == 0
            and value["buyers"] >= 3
            and value["public_sol"] >= 1
            and value["seed"] >= 1
            and value["mayhem"] < 0.5
        ),
    }
    output = {}
    for name, predicate in predicates.items():
        selected = {
            (row.run_id, row.mint) for row in unique if predicate(values(row))
        }
        splits = {
            split: _rule_metrics(
                [row for row in unique if row.split == split], selected
            )
            for split in ("train", "validation", "holdout")
        }
        by_run = []
        for run_id in sorted({row.run_id for row in unique}):
            run_rows = [row for row in unique if row.run_id == run_id]
            result = _rule_metrics(run_rows, selected)
            if result["predicted"]:
                by_run.append({"run_id": run_id, **result})
        output[name] = {
            "splits": splits,
            "all": _rule_metrics(unique, selected),
            "runs_with_predictions": len(by_run),
            "run_precision": quantiles([row["precision"] for row in by_run]),
        }

    joint: dict[str, list[wallet.LaunchRow]] = defaultdict(list)
    for row in unique:
        value = values(row)
        creator_bin = "0" if value["prior"] == 0 else "1" if value["prior"] == 1 else "2+"
        buyer_bin = "0" if value["buyers"] == 0 else "1" if value["buyers"] == 1 else "2+"
        joint[f"creator={creator_bin}|known_buyers={buyer_bin}"].append(row)
    joint_table = {
        key: _rule_metrics(group, {(row.run_id, row.mint) for row in group})
        for key, group in sorted(joint.items())
    }

    cached = [values(row) for row in unique]
    prior_values = np.asarray([value["prior"] for value in cached])
    buyer_values = np.asarray([value["buyers"] for value in cached])
    seed_values = np.asarray([value["seed"] for value in cached])
    public_sol_values = np.asarray([value["public_sol"] for value in cached])
    launch_values = np.asarray(
        [feature(row, "creator_prior_launches") for row in unique]
    )
    non_mayhem = np.asarray([value["mayhem"] < 0.5 for value in cached])
    truth = np.asarray([row.selected for row in unique])
    split_masks = {
        split: np.asarray([row.split == split for row in unique])
        for split in ("train", "validation", "holdout")
    }

    def mask_metrics(chosen: np.ndarray, split: str) -> dict[str, Any]:
        in_split = split_masks[split]
        predicted = chosen & in_split
        true_positive = int(np.sum(predicted & truth))
        predicted_count = int(np.sum(predicted))
        attempt_count = int(np.sum(in_split & truth))
        return {
            "launches": int(np.sum(in_split)),
            "attempts": attempt_count,
            "predicted": predicted_count,
            "true_positive": true_positive,
            "precision": true_positive / max(predicted_count, 1),
            "recall": true_positive / max(attempt_count, 1),
        }

    frontier = []
    for prior in (0, 1, 2, 3, 5):
        for buyers in (0, 1, 2, 3):
            for seed in (0, 1, 2, 3, 5):
                for public_sol in (0, 0.5, 1, 2, 3):
                    for maximum_launches in (4, 10, 20, 1_000_000):
                        chosen = (
                            (prior_values >= prior)
                            & (buyer_values >= buyers)
                            & (seed_values >= seed)
                            & (public_sol_values >= public_sol)
                            & (launch_values <= maximum_launches)
                            & non_mayhem
                        )
                        train_metrics = mask_metrics(chosen, "train")
                        validation_metrics = mask_metrics(chosen, "validation")
                        if (
                            train_metrics["predicted"] < 20
                            or validation_metrics["predicted"] < 15
                        ):
                            continue
                        stable_precision = min(
                            train_metrics["precision"], validation_metrics["precision"]
                        )
                        stable_recall = min(
                            train_metrics["recall"], validation_metrics["recall"]
                        )
                        score = stable_precision * math.sqrt(stable_recall)
                        frontier.append(
                            {
                                "selection_score": score,
                                "parameters": {
                                    "minimum_prior_creator_attempts": prior,
                                    "minimum_known_early_buyers": buyers,
                                    "minimum_creator_seed_sol": seed,
                                    "minimum_public_sol_by_5ms": public_sol,
                                    "maximum_prior_creator_launches": maximum_launches,
                                    "reject_mayhem": True,
                                },
                                "train": train_metrics,
                                "validation": validation_metrics,
                                "holdout_exploratory": mask_metrics(chosen, "holdout"),
                            }
                        )
    frontier.sort(
        key=lambda row: (
            row["selection_score"],
            row["validation"]["precision"],
            row["validation"]["recall"],
        ),
        reverse=True,
    )
    return {
        "decision_horizon_ms": 5,
        "implementation_shape": (
            "constant-time hash lookups and numeric comparisons; no ensemble inference"
        ),
        "rules": output,
        "creator_buyer_joint_table": joint_table,
        "validation_selected_conjunctions": frontier[:25],
    }


def create_event_rule_search(rows: Sequence[wallet.LaunchRow]) -> dict[str, Any]:
    """Search only information delivered atomically with CREATE."""
    unique = [row for row in rows if row.horizon_ms == 0]
    truth = np.asarray([row.selected for row in unique])
    split_masks = {
        split: np.asarray([row.split == split for row in unique])
        for split in ("train", "validation", "holdout")
    }
    arrays = {
        name: np.asarray([feature(row, name) for row in unique])
        for name in (
            "creator_prior_e4_attempts",
            "creator_seed_sol",
            "creator_prior_launches",
            "milliseconds_since_creator_launch",
            "cashback_enabled",
            "metadata_content_addressed",
            "mayhem_mode",
        )
    }

    def metrics_for(mask: np.ndarray, split: str) -> dict[str, Any]:
        in_split = split_masks[split]
        predicted = mask & in_split
        predicted_count = int(np.sum(predicted))
        true_positive = int(np.sum(predicted & truth))
        attempts = int(np.sum(in_split & truth))
        return {
            "launches": int(np.sum(in_split)),
            "attempts": attempts,
            "predicted": predicted_count,
            "true_positive": true_positive,
            "precision": true_positive / max(predicted_count, 1),
            "recall": true_positive / max(attempts, 1),
        }

    candidates = []
    for prior in (0, 1, 2, 3, 5):
        for seed in (0, 0.1, 0.5, 1, 2, 3, 5):
            for maximum_launches in (2, 4, 10, 20, 1_000_000):
                for minimum_gap_ms in (0, 30_000, 60_000, 150_000, 300_000):
                    for cashback in (-1, 0, 1):
                        for content_addressed in (-1, 0, 1):
                            mask = (
                                (arrays["creator_prior_e4_attempts"] >= prior)
                                & (arrays["creator_seed_sol"] >= seed)
                                & (arrays["creator_prior_launches"] <= maximum_launches)
                                & (
                                    arrays["milliseconds_since_creator_launch"]
                                    >= minimum_gap_ms
                                )
                                & (arrays["mayhem_mode"] < 0.5)
                            )
                            if cashback >= 0:
                                mask &= arrays["cashback_enabled"] == cashback
                            if content_addressed >= 0:
                                mask &= (
                                    arrays["metadata_content_addressed"]
                                    == content_addressed
                                )
                            train = metrics_for(mask, "train")
                            validation = metrics_for(mask, "validation")
                            if train["predicted"] < 15 or validation["predicted"] < 10:
                                continue
                            stable_precision = min(
                                train["precision"], validation["precision"]
                            )
                            stable_recall = min(train["recall"], validation["recall"])
                            candidates.append(
                                {
                                    "selection_score": stable_precision
                                    * math.sqrt(stable_recall),
                                    "parameters": {
                                        "minimum_prior_creator_attempts": prior,
                                        "minimum_creator_seed_sol": seed,
                                        "maximum_prior_creator_launches": maximum_launches,
                                        "minimum_creator_gap_ms": minimum_gap_ms,
                                        "cashback": cashback,
                                        "metadata_content_addressed": content_addressed,
                                        "reject_mayhem": True,
                                    },
                                    "train": train,
                                    "validation": validation,
                                    "holdout_exploratory": metrics_for(mask, "holdout"),
                                }
                            )
    candidates.sort(
        key=lambda row: (
            row["selection_score"],
            row["validation"]["precision"],
            row["validation"]["recall"],
        ),
        reverse=True,
    )
    return {
        "decision_horizon_ms": 0,
        "causal_contract": "CREATE event fields and prior state only",
        "validation_selected_conjunctions": candidates[:25],
    }


def e4_decision_delay_audit(
    rows: Sequence[wallet.LaunchRow],
    traces: Mapping[tuple[str, str], wallet.base.Trace],
) -> dict[str, Any]:
    selected = [row for row in rows if row.horizon_ms == 0 and row.selected]
    delays = []
    for row in selected:
        trace = traces[(row.run_id, row.mint)]
        point = next(
            (
                point
                for point in trace.points
                if point.kind in wallet.base.choice_sets.BUY_KINDS
                and point.trader == wallet.E4_WALLET
            ),
            None,
        )
        if point is not None:
            delays.append((point.timestamp_ns - trace.create_ns) / 1_000_000)
    return {
        "attempts": len(selected),
        "observed_buy_delays": len(delays),
        "delay_ms": quantiles(delays),
        "share_at_or_below": {
            str(limit): sum(delay <= limit for delay in delays) / max(len(delays), 1)
            for limit in (1, 2, 5, 10, 20, 50, 250)
        },
    }


def markdown_report(result: Mapping[str, Any]) -> str:
    dev = result["developer_recurrence"]
    frontier = result["compact_model_frontier"]["models"]
    lines = [
        "# E4 selection cohort forensics",
        "",
        "This report explains E4 intent; it is not an approved production model.",
        "",
        "## Developer recurrence",
        "",
        f"- E4 attempts: {dev['selected_attempts']}",
        f"- Unique selected creators: {dev['unique_selected_creators']}",
        f"- Creators selected more than once: {dev['creators_selected_more_than_once']}",
        f"- Attempt share from repeat-selected creators: {dev['repeat_attempt_share']:.2%}",
        "",
        "## Compact scoring frontier",
        "",
        "| model | validation AP | precision | recall | median score time | exploratory holdout AP |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in frontier:
        lines.append(
            f"| {row['family']} | {row['validation']['average_precision']:.4f} | "
            f"{row['validation']['precision']:.2%} | {row['validation']['recall']:.2%} | "
            f"{row['single_row_scoring']['median_microseconds']:.1f} us | "
            f"{row['holdout_exploratory']['average_precision']:.4f} |"
        )
    lines.extend(
        [
            "",
            "The final ten captures are exploratory for this revision because an earlier model already opened them.",
            "A new future epoch is required for approval.",
        ]
    )
    return "\n".join(lines) + "\n"


def run(root: Path) -> dict[str, Any]:
    development_rows, development_traces, development_audit = wallet.extract_dataset(
        root, include_holdout=False
    )
    holdout_rows, holdout_traces, holdout_audit = wallet.extract_dataset(
        root, include_holdout=True, only_holdout=True
    )
    rows = [*development_rows, *holdout_rows]
    traces = {**development_traces, **holdout_traces}
    buyer_features, buyer_audit = causal_buyer_features(rows, traces)
    result = {
        "schema_version": SCHEMA_VERSION,
        "scientific_status": "EXPLORATORY_POST_HOLDOUT_FORENSICS",
        "oracle_is_selection_model": False,
        "production_paths_changed": 0,
        "development_audit": development_audit,
        "holdout_audit": holdout_audit,
        "cohorts": {
            str(horizon): cohort_features(rows, horizon) for horizon in (0, 5, 20, 250)
        },
        "recurrence": recurrence_bins(rows, 0),
        "categorical_lift": categorical_lift(rows, 0),
        "developer_recurrence": developer_recurrence(rows, traces, 0),
        "same_time_matched": {
            str(horizon): matched_same_time(rows, horizon) for horizon in (0, 5, 20, 250)
        },
        "name_symbol_token_lift": token_lift(root, rows, 0),
        "compact_model_frontier": compact_model_frontier(rows),
        "transparent_5ms_formula": transparent_formula(rows),
        "early_buyer_recurrence": buyer_audit,
        "buyer_augmented_frontier": buyer_augmented_frontier(rows, buyer_features),
        "transparent_network_rules": transparent_network_rules(rows, buyer_features),
        "create_event_rule_search": create_event_rule_search(rows),
        "e4_decision_delay_audit": e4_decision_delay_audit(rows, traces),
    }
    wallet.write_json(root / "artifacts/e4-v12-selection-cohort-forensics.json", result)
    (root / "artifacts/e4-v12-selection-cohort-forensics.md").write_text(
        markdown_report(result), encoding="utf-8"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    result = run(args.repo_root.resolve())
    print(
        json.dumps(
            {
                "scientific_status": result["scientific_status"],
                "developer_recurrence": result["developer_recurrence"],
                "top_tokens": result["name_symbol_token_lift"][:10],
                "compact_models": result["compact_model_frontier"]["models"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

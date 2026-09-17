#!/usr/bin/env python3
"""Test E4's strict creator/social/network accept-reject hypothesis."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import e4_v12_wallet_selection_model as wallet
import numpy as np
from sklearn.ensemble import ExtraTreesClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.tree import DecisionTreeClassifier, export_text

SCHEMA_VERSION = "e4-v12-social-network-selector-v1"
SOURCE = Path(
    ".tmp-choice-set-source/research/33889403746/e4-v12-global-attempt-intent.json"
)
STRICT_FEATURES = (
    "log_creator_seed_sol",
    "log_fdv_usd",
    "log_hist_trades",
    "hist_win_rate",
    "prior_creator_attempts",
    "prior_handle_attempts",
    "known_handle",
    "elite_creator",
    "cashback",
    "mayhem",
    "metadata_ok",
    "tweet_within_0_5s",
    "tweet_within_30s",
    "tweet_within_30m",
    "ipfs_metadata",
)
NETWORK_FEATURES = (
    "known_buyer_count",
    "known_buyer_2plus",
    "buyer_selected_sum",
    "buyer_selected_max",
    "buyer_max_smoothed_rate",
)


def finite(value: Any, default: float = 0.0) -> float:
    return wallet.finite(value, default)


def load_rows(root: Path) -> list[dict[str, Any]]:
    payload = json.loads((root / SOURCE).read_text(encoding="utf-8"))
    rows = [dict(row) for row in payload["rows"]]
    rows.sort(
        key=lambda row: (
            int(row.get("run_index") or 0),
            int(row.get("create_ns") or 0),
            str(row.get("mint") or ""),
        )
    )
    buyer_selected: Counter[str] = Counter()
    buyer_launches: Counter[str] = Counter()
    for row in rows:
        buyers = [str(value) for value in row.get("first_buyers") or [] if value]
        rates = [
            (buyer_selected[buyer] + 1) / (buyer_launches[buyer] + 20)
            for buyer in buyers
        ]
        row["known_buyer_count"] = sum(buyer_selected[buyer] > 0 for buyer in buyers)
        row["buyer_selected_sum"] = sum(buyer_selected[buyer] for buyer in buyers)
        row["buyer_selected_max"] = max(
            (buyer_selected[buyer] for buyer in buyers), default=0
        )
        row["buyer_max_smoothed_rate"] = max(rates, default=0.0)
        for buyer in buyers:
            buyer_launches[buyer] += 1
            if row.get("positive"):
                buyer_selected[buyer] += 1
    return rows


def strict_vector(row: Mapping[str, Any], *, include_network: bool) -> tuple[float, ...]:
    tweet_age = finite(row.get("tweet_age_seconds"), 1e99)
    values = (
        math.log1p(max(finite(row.get("creator_seed_sol")), 0.0)),
        math.log1p(max(finite(row.get("fdv_usd")), 0.0)),
        math.log1p(max(finite(row.get("hist_trades")), 0.0)),
        finite(row.get("hist_rate")),
        finite(row.get("prior_creator_attempts")),
        finite(row.get("prior_handle_attempts")),
        float(bool(row.get("known_handle"))),
        float(bool(row.get("elite_creator"))),
        float(bool(row.get("cashback"))),
        float(bool(row.get("mayhem"))),
        float(bool(row.get("metadata_ok"))),
        float(0 <= tweet_age <= 0.5),
        float(0 <= tweet_age <= 30),
        float(0 <= tweet_age <= 1_800),
        float("ipfs" in str(row.get("metadata_host") or "").lower()),
    )
    if not include_network:
        return values
    return (
        *values,
        finite(row.get("known_buyer_count")),
        float(finite(row.get("known_buyer_count")) >= 2),
        finite(row.get("buyer_selected_sum")),
        finite(row.get("buyer_selected_max")),
        finite(row.get("buyer_max_smoothed_rate")),
    )


def metrics(
    rows: Sequence[Mapping[str, Any]], scores: np.ndarray, threshold: float
) -> dict[str, Any]:
    truth = np.asarray([bool(row.get("positive")) for row in rows])
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


def choose_threshold(
    rows: Sequence[Mapping[str, Any]], scores: np.ndarray
) -> tuple[float, dict[str, Any]]:
    ordered = sorted(scores, reverse=True)
    candidates = []
    for target in (30, 50, 80, 120, 200):
        threshold = float(ordered[min(target - 1, len(ordered) - 1)])
        result = metrics(rows, scores, threshold)
        precision = result["precision"]
        recall = result["recall"]
        f_half = 1.25 * precision * recall / max(0.25 * precision + recall, 1e-12)
        candidates.append((f_half, precision, recall, -target, threshold, result))
    _, _, _, _, threshold, result = max(candidates)
    return threshold, result


def speed(model: Any, sample: np.ndarray) -> dict[str, float]:
    for _ in range(100):
        model.predict_proba(sample)
    timings = []
    for _ in range(2_000):
        started = time.perf_counter_ns()
        model.predict_proba(sample)
        timings.append(time.perf_counter_ns() - started)
    return {
        "median_microseconds": statistics.median(timings) / 1_000,
        "p95_microseconds": float(np.quantile(timings, 0.95)) / 1_000,
    }


def fit_family(
    fit_rows: Sequence[Mapping[str, Any]],
    calibration: Sequence[Mapping[str, Any]],
    holdout: Sequence[Mapping[str, Any]],
    *,
    include_network: bool,
) -> list[dict[str, Any]]:
    x_fit = np.asarray(
        [strict_vector(row, include_network=include_network) for row in fit_rows],
        dtype=float,
    )
    y_fit = np.asarray([bool(row.get("positive")) for row in fit_rows], dtype=int)
    names = STRICT_FEATURES + (NETWORK_FEATURES if include_network else ())
    models = {
        "logistic": LogisticRegression(
            C=0.1,
            class_weight="balanced",
            max_iter=2_000,
            random_state=wallet.RANDOM_SEED,
        ).fit(x_fit, y_fit),
        "tree_depth_5": DecisionTreeClassifier(
            max_depth=5,
            min_samples_leaf=15,
            class_weight="balanced",
            random_state=wallet.RANDOM_SEED,
        ).fit(x_fit, y_fit),
        "extra_trees_32": ExtraTreesClassifier(
            n_estimators=32,
            min_samples_leaf=6,
            max_features=0.8,
            class_weight="balanced",
            n_jobs=1,
            random_state=wallet.RANDOM_SEED,
        ).fit(x_fit, y_fit),
    }
    x_calibration = np.asarray(
        [strict_vector(row, include_network=include_network) for row in calibration],
        dtype=float,
    )
    x_holdout = np.asarray(
        [strict_vector(row, include_network=include_network) for row in holdout],
        dtype=float,
    )
    results = []
    for family, model in models.items():
        calibration_scores = model.predict_proba(x_calibration)[:, 1]
        threshold, calibration_metrics = choose_threshold(calibration, calibration_scores)
        holdout_scores = model.predict_proba(x_holdout)[:, 1]
        record = {
            "family": f"{'network' if include_network else 'strict'}_{family}",
            "feature_names": list(names),
            "threshold": threshold,
            "calibration": calibration_metrics,
            "holdout": metrics(holdout, holdout_scores, threshold),
            "single_row_scoring": speed(model, x_calibration[:1]),
        }
        if family == "logistic":
            record["intercept"] = float(model.intercept_[0])
            record["terms"] = [
                {"feature": name, "coefficient": float(coefficient)}
                for name, coefficient in sorted(
                    zip(names, model.coef_[0]),
                    key=lambda item: abs(item[1]),
                    reverse=True,
                )
            ]
        if family == "tree_depth_5":
            record["rules"] = export_text(model, feature_names=list(names))
        results.append(record)
    return results


def cohort_lifts(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    base = sum(bool(row.get("positive")) for row in rows) / max(len(rows), 1)
    predicates = {
        "known_handle": lambda row: bool(row.get("known_handle")),
        "elite_creator": lambda row: bool(row.get("elite_creator")),
        "prior_creator_attempt": lambda row: finite(row.get("prior_creator_attempts")) > 0,
        "prior_handle_attempt": lambda row: finite(row.get("prior_handle_attempts")) > 0,
        "tweet_within_30s": lambda row: 0
        <= finite(row.get("tweet_age_seconds"), 1e99)
        <= 30,
        "two_known_early_buyers": lambda row: finite(row.get("known_buyer_count")) >= 2,
    }
    output = {}
    for name, predicate in predicates.items():
        group = [row for row in rows if predicate(row)]
        selected = sum(bool(row.get("positive")) for row in group)
        rate = selected / max(len(group), 1)
        output[name] = {
            "launches": len(group),
            "selected": selected,
            "selection_rate": rate,
            "lift": rate / max(base, 1e-12),
        }
    return output


def transparent_social_rule_search(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Search transparent CREATE-time social/identity conjunctions."""
    truth = np.asarray([bool(row.get("positive")) for row in rows])
    run_index = np.asarray([int(row.get("run_index") or 0) for row in rows])
    split_masks = {
        "fit": run_index <= 5,
        "calibration": (run_index >= 6) & (run_index <= 7),
        "holdout": run_index >= 8,
    }
    seed = np.asarray([finite(row.get("creator_seed_sol")) for row in rows])
    prior_creator = np.asarray(
        [finite(row.get("prior_creator_attempts")) for row in rows]
    )
    prior_handle = np.asarray(
        [finite(row.get("prior_handle_attempts")) for row in rows]
    )
    known_handle = np.asarray([bool(row.get("known_handle")) for row in rows])
    elite_creator = np.asarray([bool(row.get("elite_creator")) for row in rows])
    tweet_age = np.asarray(
        [finite(row.get("tweet_age_seconds"), 1e99) for row in rows]
    )
    cashback = np.asarray([bool(row.get("cashback")) for row in rows])
    mayhem = np.asarray([bool(row.get("mayhem")) for row in rows])
    metadata_ok = np.asarray([bool(row.get("metadata_ok")) for row in rows])

    identities = {
        "known_handle": known_handle,
        "elite_creator": elite_creator,
        "prior_handle": prior_handle >= 1,
        "known_or_elite": known_handle | elite_creator,
        "any_identity": known_handle | elite_creator | (prior_handle >= 1),
    }

    def metrics_for(mask: np.ndarray, split: str) -> dict[str, Any]:
        eligible = mask & split_masks[split]
        predicted = int(np.sum(eligible))
        true_positive = int(np.sum(eligible & truth))
        attempts = int(np.sum(split_masks[split] & truth))
        return {
            "launches": int(np.sum(split_masks[split])),
            "attempts": attempts,
            "predicted": predicted,
            "true_positive": true_positive,
            "precision": true_positive / max(predicted, 1),
            "recall": true_positive / max(attempts, 1),
        }

    candidates = []
    for identity_name, identity_mask in identities.items():
        for maximum_tweet_age in (0.5, 5, 30, 300, 1_800, 1e99):
            for minimum_seed in (0, 0.5, 1, 2, 3):
                for require_cashback in (False, True):
                    for require_metadata in (False, True):
                        for cold_creator_only in (False, True):
                            mask = (
                                identity_mask
                                & (tweet_age <= maximum_tweet_age)
                                & (seed >= minimum_seed)
                                & ~mayhem
                            )
                            if require_cashback:
                                mask &= cashback
                            if require_metadata:
                                mask &= metadata_ok
                            if cold_creator_only:
                                mask &= prior_creator == 0
                            fit = metrics_for(mask, "fit")
                            calibration = metrics_for(mask, "calibration")
                            if fit["predicted"] < 8 or calibration["predicted"] < 5:
                                continue
                            stable_precision = min(
                                fit["precision"], calibration["precision"]
                            )
                            stable_recall = min(fit["recall"], calibration["recall"])
                            candidates.append(
                                {
                                    "selection_score": stable_precision
                                    * math.sqrt(stable_recall),
                                    "parameters": {
                                        "identity": identity_name,
                                        "maximum_tweet_age_seconds": maximum_tweet_age,
                                        "minimum_creator_seed_sol": minimum_seed,
                                        "require_cashback": require_cashback,
                                        "require_metadata": require_metadata,
                                        "cold_creator_only": cold_creator_only,
                                        "reject_mayhem": True,
                                    },
                                    "fit": fit,
                                    "calibration": calibration,
                                    "holdout_exploratory": metrics_for(mask, "holdout"),
                                }
                            )
    candidates.sort(
        key=lambda row: (
            row["selection_score"],
            row["calibration"]["precision"],
            row["calibration"]["recall"],
        ),
        reverse=True,
    )
    return {
        "causal_contract": "CREATE metadata plus previously observed identity state",
        "validation_selected_conjunctions": candidates[:25],
    }


def run(root: Path) -> dict[str, Any]:
    rows = load_rows(root)
    fit = [row for row in rows if int(row.get("run_index") or 0) <= 5]
    calibration = [
        row for row in rows if 6 <= int(row.get("run_index") or 0) <= 7
    ]
    holdout = [row for row in rows if int(row.get("run_index") or 0) >= 8]
    result = {
        "schema_version": SCHEMA_VERSION,
        "source": str(SOURCE),
        "split": {"fit_runs": [0, 5], "calibration_runs": [6, 7], "holdout_runs": [8, 11]},
        "post_entry_fields_excluded": [
            "buy_count",
            "outside_sol",
            "same_slot_buys",
            "unique_buyers",
            "price_multiple",
        ],
        "network_timing_warning": (
            "first_buyers lack per-buyer timestamps in this source; network model is diagnostic, "
            "while strict model is the causally admissible result"
        ),
        "cohort_lifts": cohort_lifts(rows),
        "transparent_social_rule_search": transparent_social_rule_search(rows),
        "models": [
            *fit_family(fit, calibration, holdout, include_network=False),
            *fit_family(fit, calibration, holdout, include_network=True),
        ],
        "production_paths_changed": 0,
        "golden_thesis_approved": False,
    }
    result["models"].sort(key=lambda row: row["calibration"]["average_precision"], reverse=True)
    wallet.write_json(root / "artifacts/e4-v12-social-network-selector.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    result = run(args.repo_root.resolve())
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

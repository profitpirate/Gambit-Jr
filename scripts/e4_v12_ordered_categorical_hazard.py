#!/usr/bin/env python3
"""Ordered categorical hazard models for creator, text and URI identity.

Hashed identities can destroy exact recurrence and introduce collisions.  This
lane uses CatBoost's ordered categorical statistics, fitted only on training
windows, while preserving explicit cold-start categories for unseen holdout
values.  It remains launch-time, autonomous and paper-only.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

import numpy as np
from catboost import CatBoostClassifier, Pool

from scripts import e4_v12_allout_profit_hazard as base
from scripts import e4_v12_allout_profit_hazard_strict  # noqa: F401
from scripts.e4_v12_allout_profit_hazard_stream import load_corpus_stream

VERSION = "e4-v12-ordered-categorical-hazard-v1"
CATEGORICAL = (
    "creator",
    "metadata_uri_host",
    "token_program",
    "name",
    "symbol",
)


def safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe(item) for item in value]
    if isinstance(value, np.ndarray):
        return safe(value.tolist())
    if isinstance(value, np.generic):
        return safe(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(safe(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def raw_categories(path: Path) -> np.ndarray:
    opener = gzip.open if path.suffix == ".gz" else open
    rows = []
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            rows.append(
                [
                    str(row.get(field) or "__MISSING__")[:512]
                    for field in CATEGORICAL
                ]
            )
    return np.asarray(rows, dtype=object)


def combine_features(corpus: base.Corpus, categories: np.ndarray) -> tuple[np.ndarray, list[int]]:
    numeric = corpus.x_general.astype(object)
    output = np.concatenate((numeric, categories), axis=1)
    cat_indexes = list(range(numeric.shape[1], output.shape[1]))
    return output, cat_indexes


def fit_catboost(
    x: np.ndarray,
    cat_indexes: list[int],
    target: np.ndarray,
    train: np.ndarray,
    seed: int,
    depth: int,
) -> CatBoostClassifier:
    positives = max(1, int(target[train].sum()))
    negatives = max(1, int((~target[train]).sum()))
    model = CatBoostClassifier(
        iterations=650,
        depth=depth,
        learning_rate=0.035,
        l2_leaf_reg=10.0,
        random_strength=0.7,
        bagging_temperature=0.5,
        loss_function="Logloss",
        eval_metric="Logloss",
        class_weights=[1.0, negatives / positives],
        random_seed=seed,
        verbose=False,
        allow_writing_files=False,
        thread_count=-1,
        one_hot_max_size=12,
        od_type="Iter",
        od_wait=80,
    )
    pool = Pool(x[train], label=target[train].astype(int), cat_features=cat_indexes)
    model.fit(pool)
    return model


def score(model: CatBoostClassifier, x: np.ndarray, cat_indexes: list[int]) -> np.ndarray:
    pool = Pool(x, cat_features=cat_indexes)
    return np.asarray(model.predict_proba(pool)[:, 1], dtype=np.float64)


def experiment(
    corpus: base.Corpus,
    categories: np.ndarray,
    mode: str,
    seed: int,
    depth: int,
) -> dict[str, Any]:
    x, cat_indexes = combine_features(corpus, categories)
    train = corpus.splits == "train"
    validation = corpus.splits == "validation"
    holdout = corpus.splits == "holdout"
    candidates = []

    intent_model = None
    intent_scores = None
    if mode in {"intent", "multitask"}:
        intent_model = fit_catboost(
            x, cat_indexes, corpus.selected, train, seed, depth
        )
        intent_scores = score(intent_model, x, cat_indexes)

    for policy_index, policy in enumerate(corpus.policies):
        profit_target = corpus.pnl[policy].min(axis=1) > 0
        if mode in {"profit", "multitask"}:
            profit_model = fit_catboost(
                x,
                cat_indexes,
                profit_target,
                train,
                seed + 100 + policy_index,
                depth,
            )
            profit_scores = score(profit_model, x, cat_indexes)
        else:
            profit_scores = None
        if mode == "intent":
            combined = intent_scores
        elif mode == "profit":
            combined = profit_scores
        elif mode == "multitask":
            assert intent_scores is not None and profit_scores is not None
            combined = np.sqrt(
                np.clip(intent_scores, 1e-9, 1.0)
                * np.clip(profit_scores, 1e-9, 1.0)
            )
        else:
            raise ValueError(mode)
        assert combined is not None
        for threshold in base.threshold_candidates(combined, validation):
            indexes = np.flatnonzero(validation & (combined >= threshold))
            if len(indexes) < 8:
                continue
            metrics = base.multi_latency_economics(
                indexes, combined, corpus, policy
            )
            candidates.append(
                {
                    "policy": policy,
                    "threshold": threshold,
                    "validation": metrics,
                    "eligible": base.validation_eligible(metrics),
                    "scores": combined,
                }
            )
    if not candidates:
        return {
            "version": VERSION,
            "status": "NOT_CONCLUSIVE",
            "reason": "no ordered-categorical candidate reached eight validation trades",
        }
    winner = max(
        candidates,
        key=lambda row: (1 if row["eligible"] else 0, *base.objective(row["validation"])),
    )
    holdout_indexes = np.flatnonzero(
        holdout & (winner["scores"] >= winner["threshold"])
    )
    holdout_metrics = base.multi_latency_economics(
        holdout_indexes, winner["scores"], corpus, winner["policy"]
    )
    passed, failures = base.historical_pass(holdout_metrics)

    train_values = {
        field: set(categories[train, index].tolist())
        for index, field in enumerate(CATEGORICAL)
    }
    unseen = {
        field: float(
            np.mean(
                [value not in train_values[field] for value in categories[holdout, index]]
            )
        )
        for index, field in enumerate(CATEGORICAL)
    }
    return {
        "version": VERSION,
        "status": "HISTORICAL_CANDIDATE" if passed else "NOT_CONCLUSIVE",
        "mode": mode,
        "seed": seed,
        "depth": depth,
        "categorical_fields": CATEGORICAL,
        "unseen_holdout_fraction": unseen,
        "winner": {
            "policy": winner["policy"],
            "threshold": winner["threshold"],
            "validation": winner["validation"],
            "holdout": holdout_metrics,
            "historical_pass": passed,
            "failed_requirements": failures,
        },
        "top_validation_candidates": [
            {
                "policy": row["policy"],
                "threshold": row["threshold"],
                "eligible": row["eligible"],
                "validation": {
                    key: value
                    for key, value in row["validation"].items()
                    if key != "latencies"
                },
            }
            for row in sorted(
                candidates,
                key=lambda value: (
                    1 if value["eligible"] else 0,
                    *base.objective(value["validation"]),
                ),
                reverse=True,
            )[:100]
        ],
        "production_paths_changed": 0,
        "live_trading": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--mode", choices=("intent", "profit", "multitask"), required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--depth", type=int, choices=(5, 6, 7), default=6)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    corpus = load_corpus_stream(args.corpus, 0)
    categories = raw_categories(args.corpus)
    if len(categories) != len(corpus.rows):
        raise ValueError("categorical and numeric row counts differ")
    report = experiment(corpus, categories, args.mode, args.seed, args.depth)
    digest = hashlib.sha256(args.corpus.read_bytes()).hexdigest()
    report["corpus_sha256"] = digest
    report["experiment_id"] = "e4x-ordered-cat-" + hashlib.sha256(
        json.dumps(
            {
                "version": VERSION,
                "mode": args.mode,
                "seed": args.seed,
                "depth": args.depth,
                "corpus": digest,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    write_json(args.output, report)
    print(json.dumps({"status":report.get("status"),"experiment_id":report["experiment_id"],"winner":{key:value for key,value in (report.get("winner") or {}).items() if key in {"policy","historical_pass","failed_requirements"}}},indent=2,sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

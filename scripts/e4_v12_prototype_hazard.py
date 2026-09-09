#!/usr/bin/env python3
"""Prototype and nearest-neighbour entry hazard over causal launch features.

A narrow proprietary strategy may recur as small local launch archetypes that
large global classifiers average away.  This lane scores each launch by its
nearest prior selected/profitable prototypes and explicitly abstains when the
nearest negative archetype is equally close.  Only training-window prototypes
are indexed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

import numpy as np
from sklearn.decomposition import PCA
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import RobustScaler

from scripts import e4_v12_allout_profit_hazard as base
from scripts import e4_v12_allout_profit_hazard_strict  # noqa: F401
from scripts.e4_v12_allout_profit_hazard_stream import load_corpus_stream

VERSION = "e4-v12-prototype-hazard-v1"


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


def representation(
    corpus: base.Corpus,
    identity: bool,
    train: np.ndarray,
    components: int,
    seed: int,
) -> tuple[np.ndarray, dict[str, Any]]:
    raw = corpus.x_identity if identity else corpus.x_general
    scaler = RobustScaler(quantile_range=(10.0, 90.0))
    scaler.fit(raw[train])
    scaled = np.clip(scaler.transform(raw), -15, 15).astype(np.float32)
    effective = min(components, scaled.shape[1], max(2, int(train.sum()) - 1))
    pca = PCA(n_components=effective, whiten=True, random_state=seed)
    pca.fit(scaled[train])
    reduced = pca.transform(scaled).astype(np.float32)
    return reduced, {
        "identity_features": identity,
        "components": effective,
        "explained_variance_ratio_sum": float(pca.explained_variance_ratio_.sum()),
        "scaler_fit_split": "train",
        "pca_fit_split": "train",
    }


def neighbour_score(
    representation: np.ndarray,
    train: np.ndarray,
    positive: np.ndarray,
    k: int,
) -> tuple[np.ndarray, dict[str, Any]]:
    positive_indices = np.flatnonzero(train & positive)
    negative_indices = np.flatnonzero(train & ~positive)
    if len(positive_indices) < k or len(negative_indices) < k:
        raise ValueError("insufficient prototypes for requested k")
    positive_nn = NearestNeighbors(
        n_neighbors=k,
        metric="euclidean",
        algorithm="auto",
        n_jobs=-1,
    ).fit(representation[positive_indices])
    negative_nn = NearestNeighbors(
        n_neighbors=k,
        metric="euclidean",
        algorithm="auto",
        n_jobs=-1,
    ).fit(representation[negative_indices])
    positive_distance = positive_nn.kneighbors(representation, return_distance=True)[0].mean(axis=1)
    negative_distance = negative_nn.kneighbors(representation, return_distance=True)[0].mean(axis=1)
    # Positive when selected/profitable prototypes are relatively closer.
    score = np.log1p(negative_distance) - np.log1p(positive_distance)
    return score.astype(np.float64), {
        "k": k,
        "positive_prototypes": len(positive_indices),
        "negative_prototypes": len(negative_indices),
        "distance_metric": "euclidean_in_train_fitted_whitened_PCA_space",
    }


def run(
    corpus: base.Corpus,
    mode: str,
    identity: bool,
    components: int,
    seed: int,
) -> dict[str, Any]:
    train = corpus.splits == "train"
    validation = corpus.splits == "validation"
    holdout = corpus.splits == "holdout"
    reduced, audit = representation(corpus, identity, train, components, seed)
    candidates = []

    intent_cache: dict[int, np.ndarray] = {}
    for k in (1, 3, 5, 10, 20):
        if int(corpus.selected[train].sum()) >= k:
            intent_cache[k] = neighbour_score(
                reduced, train, corpus.selected, k
            )[0]

    for policy in corpus.policies:
        profit_target = corpus.pnl[policy].min(axis=1) > 0
        for k in (1, 3, 5, 10, 20):
            try:
                profit_score, score_audit = neighbour_score(
                    reduced, train, profit_target, k
                )
            except ValueError:
                continue
            if mode == "intent":
                if k not in intent_cache:
                    continue
                score = intent_cache[k]
            elif mode == "profit":
                score = profit_score
            elif mode == "multitask":
                if k not in intent_cache:
                    continue
                intent = intent_cache[k]
                # Rank-normalised sum keeps both distance scales comparable.
                intent_rank = np.argsort(np.argsort(intent, kind="stable"), kind="stable") / max(1, len(intent) - 1)
                profit_rank = np.argsort(np.argsort(profit_score, kind="stable"), kind="stable") / max(1, len(profit_score) - 1)
                score = np.sqrt(np.clip(intent_rank, 1e-9, 1) * np.clip(profit_rank, 1e-9, 1))
            else:
                raise ValueError(mode)
            for threshold in base.threshold_candidates(score, validation):
                indices = np.flatnonzero(validation & (score >= threshold))
                if len(indices) < 8:
                    continue
                metrics = base.multi_latency_economics(
                    indices, score, corpus, policy
                )
                candidates.append(
                    {
                        "policy": policy,
                        "k": k,
                        "threshold": threshold,
                        "validation": metrics,
                        "eligible": base.validation_eligible(metrics),
                        "score_audit": score_audit,
                        "scores": score,
                    }
                )
    if not candidates:
        return {
            "version": VERSION,
            "status": "NOT_CONCLUSIVE",
            "reason": "no prototype candidate reached eight validation trades",
            "representation": audit,
        }
    winner = max(
        candidates,
        key=lambda row: (
            1 if row["eligible"] else 0,
            *base.objective(row["validation"]),
        ),
    )
    holdout_indices = np.flatnonzero(
        holdout & (winner["scores"] >= winner["threshold"])
    )
    holdout_metrics = base.multi_latency_economics(
        holdout_indices, winner["scores"], corpus, winner["policy"]
    )
    passed, failures = base.historical_pass(holdout_metrics)
    return {
        "version": VERSION,
        "status": "HISTORICAL_CANDIDATE" if passed else "NOT_CONCLUSIVE",
        "mode": mode,
        "identity_features": identity,
        "seed": seed,
        "representation": audit,
        "winner": {
            "policy": winner["policy"],
            "k": winner["k"],
            "threshold": winner["threshold"],
            "score_audit": winner["score_audit"],
            "validation": winner["validation"],
            "holdout": holdout_metrics,
            "historical_pass": passed,
            "failed_requirements": failures,
        },
        "top_validation_candidates": [
            {
                "policy": row["policy"],
                "k": row["k"],
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
    parser.add_argument("--identity", action="store_true")
    parser.add_argument("--components", type=int, default=48)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    corpus = load_corpus_stream(args.corpus, 0)
    report = run(corpus, args.mode, args.identity, args.components, args.seed)
    digest = hashlib.sha256(args.corpus.read_bytes()).hexdigest()
    report["corpus_sha256"] = digest
    report["experiment_id"] = "e4x-prototype-" + hashlib.sha256(
        json.dumps(
            {
                "version": VERSION,
                "mode": args.mode,
                "identity": args.identity,
                "components": args.components,
                "seed": args.seed,
                "corpus": digest,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    write_json(args.output, report)
    print(json.dumps({"status":report.get("status"),"experiment_id":report["experiment_id"],"winner":{key:value for key,value in (report.get("winner") or {}).items() if key in {"policy","k","historical_pass","failed_requirements"}}},indent=2,sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

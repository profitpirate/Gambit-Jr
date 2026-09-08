#!/usr/bin/env python3
"""Precision-first selective ensemble with abstention and OOD control.

The target is not maximum recall.  It is a small set of launch-time decisions
whose expected utility remains positive across every simulated latency.  The
model therefore combines diverse learners, penalises disagreement and distance
from the training distribution, and calibrates abstention on validation only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

import numpy as np
from sklearn.ensemble import ExtraTreesClassifier, HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from scripts import e4_v12_allout_profit_hazard as base
from scripts import e4_v12_allout_profit_hazard_strict  # noqa: F401
from scripts.e4_v12_allout_profit_hazard_stream import load_corpus_stream

VERSION = "e4-v12-selective-ensemble-v1"


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


def fit_members(x: np.ndarray, y: np.ndarray, seed: int) -> list[Any]:
    members: list[Any] = []
    for offset in range(3):
        extra = ExtraTreesClassifier(
            n_estimators=420,
            max_depth=10 + 2 * offset,
            min_samples_leaf=4 + offset,
            max_features="sqrt",
            class_weight="balanced_subsample",
            n_jobs=-1,
            random_state=seed + offset,
        )
        extra.fit(x, y.astype(int))
        members.append(extra)
    for offset in range(2):
        hgb = HistGradientBoostingClassifier(
            learning_rate=0.035 + 0.01 * offset,
            max_iter=320,
            max_leaf_nodes=15,
            min_samples_leaf=25 + 10 * offset,
            l2_regularization=3.0 + 2.0 * offset,
            random_state=seed + 10 + offset,
        )
        hgb.fit(x, y.astype(int), sample_weight=base.balanced_weights(y))
        members.append(hgb)
    for c_index, c_value in enumerate((0.03, 0.10)):
        logistic = make_pipeline(
            StandardScaler(),
            LogisticRegression(
                C=c_value,
                class_weight="balanced",
                max_iter=4_000,
                solver="liblinear",
                random_state=seed + 20 + c_index,
            ),
        )
        logistic.fit(x, y.astype(int))
        members.append(logistic)
    return members


def robust_ood(x: np.ndarray, train_mask: np.ndarray) -> np.ndarray:
    train = x[train_mask].astype(np.float64, copy=False)
    median = np.nanmedian(train, axis=0)
    q25 = np.nanquantile(train, 0.25, axis=0)
    q75 = np.nanquantile(train, 0.75, axis=0)
    scale = np.maximum(1e-6, q75 - q25)
    z = np.abs((x.astype(np.float64, copy=False) - median) / scale)
    z = np.clip(z, 0, 20)
    # The upper-tail mean catches multiple moderately novel dimensions without
    # allowing one extremely scaled field to dominate the entire score.
    width = max(1, min(20, z.shape[1]))
    partitioned = np.partition(z, z.shape[1] - width, axis=1)[:, -width:]
    raw = partitioned.mean(axis=1)
    train_q95 = float(np.quantile(raw[train_mask], 0.95))
    return raw / max(1e-9, train_q95)


def ensemble_scores(
    corpus: base.Corpus,
    policy: str,
    identity: bool,
    seed: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    x = corpus.x_identity if identity else corpus.x_general
    train = corpus.splits == "train"
    target = corpus.pnl[policy].min(axis=1) > 0
    members = fit_members(x[train], target[train], seed)
    member_scores = np.asarray(
        [base.predict_score(member, x) for member in members], dtype=np.float32
    )
    mean = member_scores.mean(axis=0).astype(np.float64)
    disagreement = member_scores.std(axis=0).astype(np.float64)
    ood = robust_ood(x, train)
    return mean, disagreement, ood, {
        "policy": policy,
        "identity_features": identity,
        "members": [type(member).__name__ for member in members],
        "train_positive_rate": float(target[train].mean()),
    }


def calibration_grid(
    mean: np.ndarray,
    disagreement: np.ndarray,
    ood: np.ndarray,
    validation: np.ndarray,
) -> list[tuple[float, float, float, float]]:
    output = []
    for disagreement_weight in (0.0, 0.5, 1.0, 1.5, 2.0, 3.0):
        for ood_weight in (0.0, 0.03, 0.07, 0.12, 0.20):
            conservative = mean - disagreement_weight * disagreement - ood_weight * ood
            values = conservative[validation]
            for quantile in (0.90, 0.94, 0.96, 0.975, 0.985, 0.99, 0.995, 0.997, 0.999, 0.9995):
                threshold = float(np.quantile(values, quantile))
                for maximum_ood in (0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 10.0):
                    output.append(
                        (
                            disagreement_weight,
                            ood_weight,
                            threshold,
                            maximum_ood,
                        )
                    )
    return output


def run(corpus: base.Corpus, identity: bool, seed: int) -> dict[str, Any]:
    validation = corpus.splits == "validation"
    holdout = corpus.splits == "holdout"
    candidates = []
    cached: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]] = {}
    for policy_index, policy in enumerate(corpus.policies):
        mean, disagreement, ood, model = ensemble_scores(
            corpus, policy, identity, seed + policy_index * 31
        )
        cached[policy] = (mean, disagreement, ood, model)
        for disagreement_weight, ood_weight, threshold, maximum_ood in calibration_grid(
            mean, disagreement, ood, validation
        ):
            conservative = mean - disagreement_weight * disagreement - ood_weight * ood
            indexes = np.flatnonzero(
                validation & (conservative >= threshold) & (ood <= maximum_ood)
            )
            if len(indexes) < 8:
                continue
            metrics = base.multi_latency_economics(
                indexes, conservative, corpus, policy
            )
            candidates.append(
                {
                    "policy": policy,
                    "disagreement_weight": disagreement_weight,
                    "ood_weight": ood_weight,
                    "threshold": threshold,
                    "maximum_ood": maximum_ood,
                    "validation": metrics,
                    "eligible": base.validation_eligible(metrics),
                    "model": model,
                }
            )
    if not candidates:
        return {
            "version": VERSION,
            "status": "NOT_CONCLUSIVE",
            "reason": "no selective configuration produced eight validation trades",
        }
    winner = max(
        candidates,
        key=lambda row: (
            1 if row["eligible"] else 0,
            *base.objective(row["validation"]),
        ),
    )
    mean, disagreement, ood, _ = cached[winner["policy"]]
    conservative = (
        mean
        - winner["disagreement_weight"] * disagreement
        - winner["ood_weight"] * ood
    )
    indexes = np.flatnonzero(
        holdout
        & (conservative >= winner["threshold"])
        & (ood <= winner["maximum_ood"])
    )
    holdout_metrics = base.multi_latency_economics(
        indexes, conservative, corpus, winner["policy"]
    )
    passed, failures = base.historical_pass(holdout_metrics)
    return {
        "version": VERSION,
        "status": "HISTORICAL_CANDIDATE" if passed else "NOT_CONCLUSIVE",
        "seed": seed,
        "identity_features": identity,
        "winner": {
            "policy": winner["policy"],
            "disagreement_weight": winner["disagreement_weight"],
            "ood_weight": winner["ood_weight"],
            "threshold": winner["threshold"],
            "maximum_ood": winner["maximum_ood"],
            "model": winner["model"],
            "validation": winner["validation"],
            "holdout": holdout_metrics,
            "historical_pass": passed,
            "failed_requirements": failures,
        },
        "top_validation_candidates": sorted(
            candidates,
            key=lambda row: (1 if row["eligible"] else 0, *base.objective(row["validation"])),
            reverse=True,
        )[:100],
        "production_paths_changed": 0,
        "live_trading": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--identity", action="store_true")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    corpus = load_corpus_stream(args.corpus, 0)
    report = run(corpus, args.identity, args.seed)
    corpus_hash = hashlib.sha256(args.corpus.read_bytes()).hexdigest()
    report["corpus_sha256"] = corpus_hash
    report["experiment_id"] = "e4x-selective-" + hashlib.sha256(
        json.dumps(
            {
                "version": VERSION,
                "identity": args.identity,
                "seed": args.seed,
                "corpus": corpus_hash,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    write_json(args.output, report)
    print(json.dumps({"status": report.get("status"), "experiment_id": report["experiment_id"], "winner": {key: value for key, value in (report.get("winner") or {}).items() if key in {"policy", "historical_pass", "failed_requirements"}}}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

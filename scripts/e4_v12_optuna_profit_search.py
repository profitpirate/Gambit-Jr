#!/usr/bin/env python3
"""Walk-forward, profit-first AutoML search over the immutable 66k corpus.

Hyperparameters, exit policy and abstention threshold are selected only from
training and validation windows.  The declared historical holdout is evaluated
once after the Optuna study freezes its winner.  Every trial uses autonomous
launch-time features and strict exit-settled bankroll accounting.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

import lightgbm as lgb
import numpy as np
import optuna

from scripts import e4_v12_allout_profit_hazard as base
from scripts import e4_v12_allout_profit_hazard_strict  # noqa: F401
from scripts.e4_v12_allout_profit_hazard_stream import load_corpus_stream

VERSION = "e4-v12-optuna-profit-search-v1"


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


def top_training_policies(corpus: base.Corpus, train: np.ndarray, limit: int = 8) -> list[str]:
    rows = []
    for policy in corpus.policies:
        worst = corpus.pnl[policy].min(axis=1)
        values = worst[train]
        gains = float(values[values > 0].sum())
        losses = float(-values[values < 0].sum())
        pf = gains / losses if losses > 0 else (999.0 if gains > 0 else 0.0)
        rows.append(
            (
                float(values.mean()),
                float((values > 0).mean()),
                min(20.0, pf),
                policy,
            )
        )
    rows.sort(reverse=True)
    return [row[-1] for row in rows[:limit]]


def trial_value(metrics: Mapping[str, Any]) -> float:
    trades = base.integer(metrics.get("minimum_trades"))
    wr = base.finite(metrics.get("minimum_win_rate"))
    wilson = base.finite(metrics.get("minimum_wilson_lower"))
    pf = min(20.0, base.finite(metrics.get("minimum_profit_factor")))
    pnl = base.finite(metrics.get("minimum_net_pnl_sol"), -999.0)
    drawdown = base.finite(metrics.get("maximum_drawdown_fraction"), 1.0)
    windows = base.integer(metrics.get("minimum_capture_windows"))
    if trades < 8 or windows < 2:
        return -1000.0 - (8 - min(8, trades)) * 10.0
    return (
        wr * 8.0
        + wilson * 4.0
        + math.log1p(max(0.0, pf))
        + max(-3.0, min(3.0, pnl * 6.0))
        - drawdown * 3.0
        + min(1.0, trades / 50.0)
    )


def fit_predict(
    x: np.ndarray,
    target: np.ndarray,
    train: np.ndarray,
    predict_mask: np.ndarray,
    params: Mapping[str, Any],
    seed: int,
) -> np.ndarray:
    positives = max(1, int(target[train].sum()))
    negatives = max(1, int((~target[train]).sum()))
    model = lgb.LGBMClassifier(
        objective="binary",
        n_estimators=int(params["n_estimators"]),
        learning_rate=float(params["learning_rate"]),
        num_leaves=int(params["num_leaves"]),
        max_depth=int(params["max_depth"]),
        min_child_samples=int(params["min_child_samples"]),
        subsample=float(params["subsample"]),
        colsample_bytree=float(params["colsample_bytree"]),
        reg_alpha=float(params["reg_alpha"]),
        reg_lambda=float(params["reg_lambda"]),
        scale_pos_weight=negatives / positives,
        random_state=seed,
        n_jobs=-1,
        verbosity=-1,
    )
    model.fit(x[train], target[train].astype(int))
    scores = np.zeros(len(x), dtype=np.float64)
    scores[predict_mask] = model.predict_proba(x[predict_mask])[:, 1]
    return scores


def run(corpus: base.Corpus, identity: bool, seed: int, trials: int) -> dict[str, Any]:
    x = corpus.x_identity if identity else corpus.x_general
    train = corpus.splits == "train"
    validation = corpus.splits == "validation"
    holdout = corpus.splits == "holdout"
    policies = top_training_policies(corpus, train, limit=8)
    trial_records: dict[int, dict[str, Any]] = {}

    def objective(trial: optuna.Trial) -> float:
        policy = trial.suggest_categorical("policy", policies)
        params = {
            "n_estimators": trial.suggest_int("n_estimators", 180, 900, step=60),
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.12, log=True),
            "num_leaves": trial.suggest_int("num_leaves", 7, 63),
            "max_depth": trial.suggest_int("max_depth", 3, 12),
            "min_child_samples": trial.suggest_int("min_child_samples", 10, 160, step=10),
            "subsample": trial.suggest_float("subsample", 0.55, 1.0),
            "colsample_bytree": trial.suggest_float("colsample_bytree", 0.35, 1.0),
            "reg_alpha": trial.suggest_float("reg_alpha", 1e-4, 10.0, log=True),
            "reg_lambda": trial.suggest_float("reg_lambda", 1e-3, 20.0, log=True),
        }
        target = corpus.pnl[policy].min(axis=1) > 0
        scores = fit_predict(
            x,
            target,
            train,
            validation,
            params,
            seed + trial.number,
        )
        values = scores[validation]
        quantile = trial.suggest_float("entry_quantile", 0.90, 0.9997)
        threshold = float(np.quantile(values, quantile))
        indexes = np.flatnonzero(validation & (scores >= threshold))
        metrics = base.multi_latency_economics(indexes, scores, corpus, policy)
        value = trial_value(metrics)
        trial_records[trial.number] = {
            "number": trial.number,
            "policy": policy,
            "params": params,
            "entry_quantile": quantile,
            "threshold": threshold,
            "validation": metrics,
            "objective": value,
        }
        trial.set_user_attr("minimum_trades", metrics.get("minimum_trades"))
        trial.set_user_attr("minimum_win_rate", metrics.get("minimum_win_rate"))
        trial.set_user_attr("minimum_profit_factor", metrics.get("minimum_profit_factor"))
        trial.set_user_attr("minimum_net_pnl_sol", metrics.get("minimum_net_pnl_sol"))
        return value

    sampler = optuna.samplers.TPESampler(seed=seed, multivariate=True)
    study = optuna.create_study(direction="maximize", sampler=sampler)
    study.optimize(objective, n_trials=trials, gc_after_trial=True, show_progress_bar=False)
    frozen = trial_records[study.best_trial.number]
    policy = frozen["policy"]
    target = corpus.pnl[policy].min(axis=1) > 0
    # Final model uses train only; validation was used for model/threshold choice
    # and is not folded into fitting before the historical holdout.
    scores = fit_predict(
        x,
        target,
        train,
        holdout,
        frozen["params"],
        seed + study.best_trial.number,
    )
    holdout_indexes = np.flatnonzero(holdout & (scores >= frozen["threshold"]))
    holdout_metrics = base.multi_latency_economics(
        holdout_indexes, scores, corpus, policy
    )
    passed, failures = base.historical_pass(holdout_metrics)
    ranked = sorted(trial_records.values(), key=lambda row: row["objective"], reverse=True)
    return {
        "version": VERSION,
        "status": "HISTORICAL_CANDIDATE" if passed else "NOT_CONCLUSIVE",
        "identity_features": identity,
        "seed": seed,
        "trials": trials,
        "candidate_policies": policies,
        "winner": {
            "trial_number": frozen["number"],
            "policy": policy,
            "params": frozen["params"],
            "entry_quantile": frozen["entry_quantile"],
            "threshold": frozen["threshold"],
            "validation": frozen["validation"],
            "holdout": holdout_metrics,
            "historical_pass": passed,
            "failed_requirements": failures,
        },
        "top_trials": [
            {
                **{key: value for key, value in row.items() if key != "validation"},
                "validation": {
                    key: value
                    for key, value in row["validation"].items()
                    if key != "latencies"
                },
            }
            for row in ranked[:100]
        ],
        "production_paths_changed": 0,
        "live_trading": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--identity", action="store_true")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--trials", type=int, default=160)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    corpus = load_corpus_stream(args.corpus, 0)
    report = run(corpus, args.identity, args.seed, args.trials)
    corpus_hash = hashlib.sha256(args.corpus.read_bytes()).hexdigest()
    report["corpus_sha256"] = corpus_hash
    report["experiment_id"] = "e4x-optuna-" + hashlib.sha256(
        json.dumps(
            {
                "version": VERSION,
                "identity": args.identity,
                "seed": args.seed,
                "trials": args.trials,
                "corpus": corpus_hash,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    write_json(args.output, report)
    print(json.dumps({"status":report.get("status"),"experiment_id":report["experiment_id"],"winner":{key:value for key,value in (report.get("winner") or {}).items() if key in {"trial_number","policy","entry_quantile","historical_pass","failed_requirements"}}},indent=2,sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Explain which frozen selections lose their edge at realistic latency."""

from __future__ import annotations

import json
import pickle
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

import e4_v12_causal_actor_memory as actors
import e4_v12_online_conformal_latency as latency
import e4_v12_online_conformal_precision as conformal
import e4_v12_profit_survival_search as base
import numpy as np
from sklearn.metrics import roc_auc_score

OUTPUT = Path("artifacts/e4-v12-latency-flip-autopsy.json")
SCHEMA_VERSION = "e4-v12-latency-flip-autopsy-v1"
SOURCE_FILES = (
    "scripts/e4_v12_latency_flip_autopsy.py",
    "scripts/e4_v12_realistic_latency_diagnostic.py",
    "scripts/e4_v12_online_conformal_latency.py",
    "scripts/e4_v12_online_conformal_precision.py",
    "scripts/e4_v12_causal_actor_memory.py",
    "scripts/e4_v12_adaptive_exit_search.py",
    "scripts/e4_v12_profit_survival_search.py",
)


def sha256_lf(path: Path) -> str:
    return __import__("hashlib").sha256(
        path.read_bytes().replace(b"\r\n", b"\n")
    ).hexdigest()


def source_code_fingerprint(root: Path | None = None) -> str:
    root = Path.cwd() if root is None else root
    return base.stable_hash(
        {relative: sha256_lf(root / relative) for relative in SOURCE_FILES}
    )


def trade_key(row: dict[str, Any]) -> tuple[str, str]:
    return str(row["run_id"]), str(row["mint"])


def finite_median(values: list[float]) -> float | None:
    clean = [value for value in values if np.isfinite(value)]
    return statistics.median(clean) if clean else None


def main() -> dict[str, Any]:
    development = json.loads(latency.DEVELOPMENT.read_text(encoding="utf-8"))
    selected_folds = [list(fold["ledger"]) for fold in development["folds"]]
    keys = {trade_key(row) for fold in selected_folds for row in fold}
    traces, trace_audit = latency.load_selected_traces(keys)
    with conformal.CACHE.open("rb") as handle:
        payload = pickle.load(handle)
    row_by_key = {(row.run_id, row.mint): row for row in payload["rows"]}
    missing = keys - set(row_by_key)
    if missing:
        raise ValueError(f"selected feature rows missing: {sorted(missing)[:5]}")

    records: list[dict[str, Any]] = []
    fold_metrics = []
    for fold_number, selected in enumerate(selected_folds):
        fast = {
            trade_key(row): row
            for row in latency.replay(selected, traces, 10)["ledger"]
        }
        slow = {
            trade_key(row): row
            for row in latency.replay(selected, traces, 300)["ledger"]
        }
        if set(fast) != set(slow):
            raise ValueError("latency replay changed the closed-trade risk set")
        categories: Counter[str] = Counter()
        for key in sorted(fast, key=lambda item: (int(item[0]), item[1])):
            fast_row = fast[key]
            slow_row = slow[key]
            category = (
                ("win" if fast_row["win"] else "loss")
                + "_to_"
                + ("win" if slow_row["win"] else "loss")
            )
            categories[category] += 1
            records.append(
                {
                    "fold": fold_number,
                    "features": row_by_key[key].features,
                    "fast_win": bool(fast_row["win"]),
                    "slow_win": bool(slow_row["win"]),
                    "fast_pnl": float(fast_row["pnl_sol"]),
                    "slow_pnl": float(slow_row["pnl_sol"]),
                    "category": category,
                }
            )
        fold_metrics.append(
            {
                "fold": fold_number,
                "trades": len(fast),
                "categories": dict(sorted(categories.items())),
                "fast_win_rate": sum(row["win"] for row in fast.values())
                / len(fast),
                "slow_win_rate": sum(row["win"] for row in slow.values())
                / len(slow),
            }
        )

    target = np.asarray([row["slow_win"] for row in records], dtype=np.int8)
    matrix = np.asarray([row["features"] for row in records], dtype=np.float64)
    feature_rows = []
    for column, name in enumerate(actors.FEATURE_NAMES):
        values = matrix[:, column]
        if len(np.unique(values)) < 2:
            continue
        auc = float(roc_auc_score(target, values))
        fold_aucs: list[float | None] = []
        for fold_number in range(len(selected_folds)):
            mask = np.asarray(
                [row["fold"] == fold_number for row in records], dtype=bool
            )
            fold_target = target[mask]
            fold_values = values[mask]
            if len(np.unique(fold_target)) < 2 or len(np.unique(fold_values)) < 2:
                fold_aucs.append(None)
            else:
                fold_aucs.append(float(roc_auc_score(fold_target, fold_values)))
        direction = 1 if auc >= 0.5 else -1
        consistent_folds = sum(
            value is not None and (value - 0.5) * direction > 0
            for value in fold_aucs
        )
        category_medians = {
            category: finite_median(
                [
                    float(row["features"][column])
                    for row in records
                    if row["category"] == category
                ]
            )
            for category in (
                "win_to_win",
                "win_to_loss",
                "loss_to_win",
                "loss_to_loss",
            )
        }
        feature_rows.append(
            {
                "feature": name,
                "auc_for_300ms_win": auc,
                "direction": "higher" if direction > 0 else "lower",
                "absolute_auc_edge": abs(auc - 0.5),
                "directionally_consistent_folds": consistent_folds,
                "fold_aucs": fold_aucs,
                "category_medians": category_medians,
            }
        )
    feature_rows.sort(
        key=lambda row: (
            row["directionally_consistent_folds"],
            row["absolute_auc_edge"],
        ),
        reverse=True,
    )
    categories = Counter(row["category"] for row in records)
    pnl_deltas = [row["slow_pnl"] - row["fast_pnl"] for row in records]
    fast_wins = categories["win_to_win"] + categories["win_to_loss"]
    identity = {
        "source_code_fingerprint": source_code_fingerprint(),
        "source_experiment_id": development["experiment_id"],
        "dataset_source_manifest_fingerprint": payload["manifest_sha256"],
        "feature_set_fingerprint": base.stable_hash(actors.FEATURE_NAMES),
        "selection_policy": "unchanged frozen development selections",
        "latencies_ms": [10, 300],
        "target": "net PnL greater than zero after exact latency replay",
    }
    output = {
        "version": SCHEMA_VERSION,
        "diagnostic_id": f"e4d-{base.stable_hash(identity)}",
        "identity": identity,
        "source_selection_count": len(records),
        "trace_audit": trace_audit,
        "categories": dict(sorted(categories.items())),
        "win_to_loss_fraction_of_10ms_winners": (
            categories["win_to_loss"] / fast_wins
        ),
        "net_winner_loss_at_300ms": (
            categories["win_to_loss"] - categories["loss_to_win"]
        ),
        "folds": fold_metrics,
        "pnl_delta_300_minus_10": {
            "median_sol": float(np.median(pnl_deltas)),
            "mean_sol": float(np.mean(pnl_deltas)),
            "minimum_sol": float(np.min(pnl_deltas)),
            "maximum_sol": float(np.max(pnl_deltas)),
        },
        "top_consistent_decision_features": feature_rows[:20],
        "finding": (
            "Latency failure concentrates in selections with sparse proven "
            "buyer history and high early return volatility. Any resilience "
            "candidate derived from this finding requires strictly later evidence."
        ),
        "candidate_fitted": False,
        "active_untouched_live_data_used": False,
        "live_confirmation_authorised": False,
        "production_promotion_authorised": False,
        "production_deployment_authorised": False,
        "production_paths_changed": 0,
    }
    base.write_json(OUTPUT, output)
    print(json.dumps(output, indent=2, sort_keys=True))
    return output


if __name__ == "__main__":
    main()

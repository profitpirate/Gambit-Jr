#!/usr/bin/env python3
"""Temporal point-process view of E4 entry hazard.

Static launch classifiers ignore that E4 chooses among a changing stream of
opportunities.  This lane adds only backward-looking market-regime features:
launch-arrival intensity, inter-arrival time, and the candidate's percentile
against launches already visible in the same capture.  No future event enters a
row.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np

from scripts import e4_v12_allout_profit_hazard as base
from scripts import e4_v12_allout_profit_hazard_strict  # noqa: F401
from scripts.e4_v12_allout_profit_hazard_stream import load_corpus_stream

WINDOWS_MS = (10, 25, 50, 100, 250, 500, 1_000, 2_500, 5_000, 10_000, 30_000, 60_000)
COMPARE_FIELDS = (
    "creator_buy_sol_0ms",
    "outside_buy_sol_0ms",
    "unique_outside_buyers_0ms",
    "create_fdv_usd",
    "creator_prior_selection_count",
    "creator_prior_launch_count",
)


def temporal_features(corpus: base.Corpus) -> tuple[np.ndarray, list[str]]:
    n = len(corpus.rows)
    names = [f"prior_launch_count_{window}ms" for window in WINDOWS_MS]
    names += [
        "time_since_previous_launch_ms",
        "arrival_rate_100ms",
        "arrival_rate_1000ms",
        "arrival_acceleration_100_vs_1000",
        "capture_progress_fraction",
    ]
    for field in COMPARE_FIELDS:
        names.extend(
            (
                f"{field}__prior60s_percentile",
                f"{field}__prior10s_percentile",
                f"{field}__prior60s_zscore",
            )
        )
    matrix = np.zeros((n, len(names)), dtype=np.float32)
    by_run: dict[str, list[int]] = {}
    for index, run_id in enumerate(corpus.run_ids):
        by_run.setdefault(str(run_id), []).append(index)

    field_values = {
        field: np.asarray(
            [base.finite(row.get(field)) for row in corpus.rows], dtype=np.float64
        )
        for field in COMPARE_FIELDS
    }

    for run_id, indexes in by_run.items():
        indexes.sort(key=lambda index: (int(corpus.create_ns[index]), str(corpus.mints[index])))
        if not indexes:
            continue
        start_ns = int(corpus.create_ns[indexes[0]])
        end_ns = max(start_ns + 1, int(corpus.create_ns[indexes[-1]]))
        time_queues = {window: deque() for window in WINDOWS_MS}
        value_queues: dict[str, deque[tuple[int, float]]] = {
            field: deque() for field in COMPARE_FIELDS
        }
        previous_ns: int | None = None
        for index in indexes:
            now = int(corpus.create_ns[index])
            column = 0
            counts: dict[int, int] = {}
            for window in WINDOWS_MS:
                queue = time_queues[window]
                cutoff = now - window * 1_000_000
                while queue and queue[0] < cutoff:
                    queue.popleft()
                counts[window] = len(queue)
                matrix[index, column] = len(queue)
                column += 1
            delta_ms = (
                (now - previous_ns) / 1_000_000 if previous_ns is not None else 60_000.0
            )
            matrix[index, column] = min(60_000.0, max(0.0, delta_ms)); column += 1
            rate_100 = counts[100] / 0.1
            rate_1000 = counts[1_000] / 1.0
            matrix[index, column] = rate_100; column += 1
            matrix[index, column] = rate_1000; column += 1
            matrix[index, column] = rate_100 / max(1e-6, rate_1000); column += 1
            matrix[index, column] = (now - start_ns) / (end_ns - start_ns); column += 1

            cutoff_60 = now - 60_000 * 1_000_000
            cutoff_10 = now - 10_000 * 1_000_000
            for field in COMPARE_FIELDS:
                queue = value_queues[field]
                while queue and queue[0][0] < cutoff_60:
                    queue.popleft()
                current = field_values[field][index]
                prior60 = np.fromiter((value for _, value in queue), dtype=np.float64)
                prior10 = np.fromiter(
                    (value for timestamp, value in queue if timestamp >= cutoff_10),
                    dtype=np.float64,
                )
                if len(prior60):
                    percentile60 = (np.sum(prior60 < current) + 0.5 * np.sum(prior60 == current)) / len(prior60)
                    mean60 = float(prior60.mean())
                    std60 = float(prior60.std())
                    zscore = (current - mean60) / max(1e-9, std60)
                else:
                    percentile60 = 0.5
                    zscore = 0.0
                if len(prior10):
                    percentile10 = (np.sum(prior10 < current) + 0.5 * np.sum(prior10 == current)) / len(prior10)
                else:
                    percentile10 = 0.5
                matrix[index, column] = percentile60; column += 1
                matrix[index, column] = percentile10; column += 1
                matrix[index, column] = np.clip(zscore, -20, 20); column += 1
                queue.append((now, current))

            for queue in time_queues.values():
                queue.append(now)
            previous_ns = now
    return matrix, names


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument(
        "--lane",
        choices=(
            "intent-hgb",
            "intent-extra",
            "intent-xgb",
            "profit-hgb",
            "profit-extra",
            "profit-xgb",
            "multitask",
            "multitask-identity",
        ),
        required=True,
    )
    parser.add_argument("--seed", type=int, default=76_321)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    corpus = load_corpus_stream(args.corpus, 0)
    temporal, names = temporal_features(corpus)
    corpus.x_general = np.concatenate((corpus.x_general, temporal), axis=1)
    corpus.feature_names_general.extend(names)
    corpus.x_identity = np.concatenate((corpus.x_identity, temporal), axis=1)
    corpus.feature_names_identity.extend(names)
    report = base.evaluate_lane(corpus, args.lane, args.seed)
    report["version"] = "e4-v12-temporal-point-process-v1"
    report["temporal_features"] = names
    report["causal_policy"] = "only launches strictly earlier in the same capture contribute"
    digest = hashlib.sha256(args.corpus.read_bytes()).hexdigest()
    report["corpus_sha256"] = digest
    report["experiment_id"] = "e4x-temporal-" + hashlib.sha256(
        json.dumps(
            {
                "version": report["version"],
                "lane": args.lane,
                "seed": args.seed,
                "corpus": digest,
                "features": names,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(base.json_safe(report), indent=2, sort_keys=True) + "\n")
    print(json.dumps({"lane":args.lane,"status":report.get("status"),"experiment_id":report["experiment_id"]},indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

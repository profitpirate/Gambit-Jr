#!/usr/bin/env python3
"""Memory-bounded entry point for the all-out profit-hazard tournament."""
from __future__ import annotations

import gzip
import json
import math
import statistics
from pathlib import Path
from typing import Any

import numpy as np

from scripts import e4_v12_allout_profit_hazard as base


def load_corpus_stream(path: Path, horizon_ms: int) -> base.Corpus:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        first_line = next((line for line in handle if line.strip()), "")
    if not first_line:
        raise ValueError("empty launch corpus")
    sample = json.loads(first_line)
    policies = base.discover_policies(sample)
    if not policies:
        raise ValueError("no common 0/1/2/5/10ms policies found")

    numeric_fields = list(base.BASE_NUMERIC) + [
        f"{stem}_{horizon_ms}ms" for stem in base.WINDOW_STEMS
    ]
    identity_fields = [field for field, _ in base.CATEGORICAL_FIELDS]
    outcome_fields = {
        base.policy_pnl_key(latency, policy)
        for policy in policies
        for latency in base.LATENCIES
    }
    outcome_fields.update(
        key
        for policy in policies
        if not policy.startswith("hold_")
        for latency in base.LATENCIES
        if (key := base.policy_exit_key(latency, policy)) is not None
    )
    retained = set(numeric_fields) | set(identity_fields) | outcome_fields | {
        "split",
        "create_ns",
        "source_run_id",
        "mint",
        "selected_by_e4",
        "landed_successfully",
        "decision_delay_ms",
        "creator_buy_sol_0ms",
        "outside_buy_sol_0ms",
        "creator_prior_selection_count",
    }

    rows: list[dict[str, Any]] = []
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            source = json.loads(line)
            rows.append({key: source.get(key) for key in retained})
    if len(rows) != 66_000:
        raise ValueError(f"expected 66,000 launch rows, got {len(rows)}")

    train_rows = [row for row in rows if str(row.get("split")) == "train"]
    medians: dict[str, float] = {}
    for field in numeric_fields:
        clean = []
        for row in train_rows:
            raw = row.get(field)
            if raw is None:
                continue
            value = base.finite(raw, float("nan"))
            if math.isfinite(value):
                clean.append(value)
        medians[field] = statistics.median(clean) if clean else 0.0

    general_names: list[str] = []
    for field in numeric_fields:
        general_names.extend((field, f"{field}__missing"))
    identity_names = list(general_names)
    for field, buckets in base.CATEGORICAL_FIELDS:
        identity_names.extend(f"hash:{field}:{index}" for index in range(buckets))

    x_general = np.zeros((len(rows), len(general_names)), dtype=np.float32)
    x_identity = np.zeros((len(rows), len(identity_names)), dtype=np.float32)
    for row_index, row in enumerate(rows):
        offset = 0
        for field in numeric_fields:
            raw = row.get(field)
            missing = raw is None
            value = base.finite(raw, medians[field]) if not missing else medians[field]
            x_general[row_index, offset] = value
            x_general[row_index, offset + 1] = float(missing)
            x_identity[row_index, offset] = value
            x_identity[row_index, offset + 1] = float(missing)
            offset += 2
        for field, buckets in base.CATEGORICAL_FIELDS:
            value = str(row.get(field) or "")
            if value:
                bucket, sign = base._hash_bucket(value, field, buckets)
                x_identity[row_index, offset + bucket] = sign
            offset += buckets

    pnl: dict[str, np.ndarray] = {}
    exit_delay: dict[str, np.ndarray] = {}
    for policy in policies:
        pnl[policy] = np.asarray(
            [
                [
                    base.finite(
                        row.get(base.policy_pnl_key(latency, policy)),
                        -base.ENTRY_BUDGET_SOL,
                    )
                    for latency in base.LATENCIES
                ]
                for row in rows
            ],
            dtype=np.float64,
        )
        delays = np.zeros((len(rows), len(base.LATENCIES)), dtype=np.float64)
        if policy.startswith("hold_"):
            hold_ms = base.integer(policy.removeprefix("hold_").removesuffix("ms"))
            for column, latency in enumerate(base.LATENCIES):
                delays[:, column] = hold_ms + latency
        else:
            for column, latency in enumerate(base.LATENCIES):
                key = base.policy_exit_key(latency, policy)
                delays[:, column] = np.asarray(
                    [base.finite(row.get(key), 60_000.0 + latency) for row in rows],
                    dtype=np.float64,
                )
        exit_delay[policy] = delays

    return base.Corpus(
        rows=rows,
        numeric_fields=numeric_fields,
        feature_names_general=general_names,
        feature_names_identity=identity_names,
        x_general=x_general,
        x_identity=x_identity,
        splits=np.asarray([str(row.get("split") or "") for row in rows], dtype=object),
        create_ns=np.asarray([base.integer(row.get("create_ns")) for row in rows], dtype=np.int64),
        run_ids=np.asarray([str(row.get("source_run_id") or "") for row in rows], dtype=object),
        mints=np.asarray([str(row.get("mint") or "") for row in rows], dtype=object),
        selected=np.asarray([bool(row.get("selected_by_e4")) for row in rows], dtype=bool),
        landed=np.asarray([bool(row.get("landed_successfully")) for row in rows], dtype=bool),
        policies=policies,
        pnl=pnl,
        exit_delay_ms=exit_delay,
    )


base.load_corpus = load_corpus_stream


if __name__ == "__main__":
    raise SystemExit(base.main())

#!/usr/bin/env python3
from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts import e4_v12_failed_intent_registry as failed_registry
from scripts import e4_v12_golden_thesis_registry  # noqa: F401 - causal pre-capture whitelist
from scripts import e4_v12_golden_thesis_search as golden
from scripts import e4_v12_true_latency_replay_v3  # noqa: F401 - dynamic fees

_ORIGINAL_BUILD_DATASET = golden.build_dataset


def _first_buyers_before_ns(launch: golden.Launch, timestamp_ns: int) -> list[str]:
    buyers: list[str] = []
    for row in launch.events:
        received = golden.integer(row.get("received_ns"))
        if received >= timestamp_ns:
            break
        if str(row.get("kind") or "").upper() not in golden.BUY_KINDS:
            continue
        trader = str(row.get("trader") or "")
        if trader and trader not in {golden.E4_WALLET, launch.creator} and trader not in buyers:
            buyers.append(trader)
        if len(buyers) >= 12:
            break
    return buyers


def _load_registry() -> dict[str, list[dict[str, Any]]]:
    raw = os.getenv("E4_FAILED_INTENT_REGISTRY", "").strip()
    if not raw:
        return {}
    path = Path(raw)
    if not path.exists():
        return {}
    try:
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    rows = payload.get("attempts_by_mint") if isinstance(payload, Mapping) else None
    return {str(key): [dict(item) for item in value] for key, value in (rows or {}).items() if isinstance(value, list)}


def build_dataset_failed_aware(
    runs: Sequence[golden.RunData],
    horizon_ms: float,
) -> list[dict[str, Any]]:
    rows = _ORIGINAL_BUILD_DATASET(runs, horizon_ms)
    attempts_by_mint = _load_registry()
    if not attempts_by_mint:
        return rows

    launches_by_mint = {
        launch.mint: launch
        for run in runs
        for launch in run.launches.values()
    }
    events: list[dict[str, Any]] = []
    current_failed_mints: set[str] = set()
    for mint, attempts in attempts_by_mint.items():
        launch = launches_by_mint.get(mint)
        if launch is None:
            continue
        for attempt in attempts:
            attempt_ns = failed_registry.estimate_attempt_ns(
                attempt,
                launch.create_ns,
                launch.create_slot,
                launch.events,
            )
            if attempt_ns <= 0:
                continue
            buyers = _first_buyers_before_ns(launch, attempt_ns)
            events.append(
                {
                    "timestamp_ns": attempt_ns,
                    "mint": mint,
                    "creator": launch.creator,
                    "buyers": buyers,
                }
            )
            current_failed_mints.add(mint)
    events.sort(key=lambda row: (golden.integer(row["timestamp_ns"]), row["mint"]))

    creator_attempts: Counter[str] = Counter()
    buyer_attempts: Counter[str] = Counter()
    pair_attempts: Counter[str] = Counter()
    pointer = 0
    ordered = sorted(
        rows,
        key=lambda row: (
            golden.integer(row.get("decision_ns")),
            str(row.get("mint") or ""),
        ),
    )
    for row in ordered:
        timestamp_ns = golden.integer(row.get("decision_ns"))
        while pointer < len(events) and golden.integer(events[pointer]["timestamp_ns"]) < timestamp_ns:
            event = events[pointer]
            creator = str(event.get("creator") or "")
            if creator:
                creator_attempts[creator] += 1
            for buyer in event.get("buyers") or []:
                buyer = str(buyer or "")
                if not buyer:
                    continue
                buyer_attempts[buyer] += 1
                if creator:
                    pair_attempts[f"{creator}|{buyer}"] += 1
            pointer += 1

        creator = str(row.get("creator") or "")
        buyers = [str(value or "") for value in row.get("first_buyers") or [] if value]
        extra_buyer_counts = [buyer_attempts[value] for value in buyers]
        extra_pair_counts = [pair_attempts[f"{creator}|{value}"] for value in buyers]
        row["prior_creator_attempts"] = golden.integer(row.get("prior_creator_attempts")) + creator_attempts[creator]
        row["known_buyer_count"] = max(
            golden.integer(row.get("known_buyer_count")),
            sum(
                golden.integer((row.get("feature_values") or {}).get("max_prior_buyer_attempts_log")) > 0
                for _ in ()
            ) + sum(value > 0 for value in extra_buyer_counts),
        )
        row["max_prior_buyer_attempts"] = max(
            golden.integer(row.get("max_prior_buyer_attempts")),
            max(extra_buyer_counts, default=0),
        )
        row["sum_prior_buyer_attempts"] = golden.integer(row.get("sum_prior_buyer_attempts")) + sum(extra_buyer_counts)
        row["max_creator_buyer_pair"] = max(
            golden.integer(row.get("max_creator_buyer_pair")),
            max(extra_pair_counts, default=0),
        )
        row["failed_intent_history_count"] = creator_attempts[creator]
        row["current_mint_had_failed_e4_intent"] = str(row.get("mint") or "") in current_failed_mints

    # A failed source transaction is not a genuine E4 ignore. It is still not
    # labelled a profitable winner: economic replay decides whether entering it
    # would have made money. The flag is retained for diagnostics and the
    # current mint is excluded from the supervised negative class by marking it
    # as too early for a definitive outcome. The model remains trained on
    # successful profitable E4 trades versus genuine ignores.
    return [
        row
        for row in rows
        if not (
            bool(row.get("current_mint_had_failed_e4_intent"))
            and not bool(row.get("positive"))
        )
    ]


golden.build_dataset = build_dataset_failed_aware


if __name__ == "__main__":
    raise SystemExit(golden.main())

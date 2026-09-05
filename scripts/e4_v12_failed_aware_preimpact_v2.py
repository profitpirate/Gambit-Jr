#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts import e4_v12_failed_intent_registry as failed_registry
from scripts import e4_v12_golden_thesis_registry  # noqa: F401
from scripts import e4_v12_golden_thesis_search as golden
from scripts import e4_v12_true_latency_replay_v3  # noqa: F401

_ORIGINAL_BUILD_DATASET = golden.build_dataset


def _load_attempts() -> dict[str, list[dict[str, Any]]]:
    path = Path(os.getenv("E4_FAILED_INTENT_REGISTRY", ""))
    if not str(path) or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    rows = payload.get("attempts_by_mint") if isinstance(payload, Mapping) else None
    return {
        str(mint): [dict(item) for item in values if isinstance(item, Mapping)]
        for mint, values in (rows or {}).items()
        if isinstance(values, list)
    }


def _buyers_before(launch: golden.Launch, timestamp_ns: int) -> list[str]:
    output: list[str] = []
    for event in launch.events:
        if golden.integer(event.get("received_ns")) >= timestamp_ns:
            break
        if str(event.get("kind") or "").upper() not in golden.BUY_KINDS:
            continue
        trader = str(event.get("trader") or "")
        if trader and trader not in {golden.E4_WALLET, launch.creator} and trader not in output:
            output.append(trader)
        if len(output) >= 12:
            break
    return output


def build_dataset_failed_aware_v2(runs: Sequence[golden.RunData], horizon_ms: float) -> list[dict[str, Any]]:
    rows = _ORIGINAL_BUILD_DATASET(runs, horizon_ms)
    attempts_by_mint = _load_attempts()
    if not attempts_by_mint:
        return rows
    launches = {launch.mint: launch for run in runs for launch in run.launches.values()}
    failed_events: list[dict[str, Any]] = []
    by_mint_ns: dict[str, list[int]] = {}
    for mint, attempts in attempts_by_mint.items():
        launch = launches.get(mint)
        if launch is None:
            continue
        values: list[int] = []
        for attempt in attempts:
            attempt_ns = failed_registry.estimate_attempt_ns(attempt, launch.create_ns, launch.create_slot, launch.events)
            if attempt_ns <= 0:
                continue
            values.append(attempt_ns)
            failed_events.append({"timestamp_ns": attempt_ns, "mint": mint, "creator": launch.creator, "buyers": _buyers_before(launch, attempt_ns)})
        if values:
            by_mint_ns[mint] = sorted(set(values))
    failed_events.sort(key=lambda event: (golden.integer(event["timestamp_ns"]), event["mint"]))

    creator_attempts: Counter[str] = Counter()
    buyer_attempts: Counter[str] = Counter()
    pair_attempts: Counter[str] = Counter()
    pointer = 0
    for row in sorted(rows, key=lambda item: (golden.integer(item.get("decision_ns")), str(item.get("mint") or ""))):
        now_ns = golden.integer(row.get("decision_ns"))
        while pointer < len(failed_events) and golden.integer(failed_events[pointer]["timestamp_ns"]) < now_ns:
            event = failed_events[pointer]
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
        buyers = [str(value) for value in row.get("first_buyers") or [] if value]
        extra_buyers = [buyer_attempts[buyer] for buyer in buyers]
        extra_pairs = [pair_attempts[f"{creator}|{buyer}"] for buyer in buyers]
        row["prior_creator_attempts"] = golden.integer(row.get("prior_creator_attempts")) + creator_attempts[creator]
        row["known_buyer_count"] = max(golden.integer(row.get("known_buyer_count")), sum(value > 0 for value in extra_buyers))
        row["max_prior_buyer_attempts"] = max(golden.integer(row.get("max_prior_buyer_attempts")), max(extra_buyers, default=0))
        row["sum_prior_buyer_attempts"] = golden.integer(row.get("sum_prior_buyer_attempts")) + sum(extra_buyers)
        row["max_creator_buyer_pair"] = max(golden.integer(row.get("max_creator_buyer_pair")), max(extra_pairs, default=0))
        row["failed_intent_history_count"] = creator_attempts[creator]

        mint = str(row.get("mint") or "")
        future_attempts = [timestamp for timestamp in by_mint_ns.get(mint, ()) if timestamp > now_ns]
        if future_attempts:
            lead_ms = (future_attempts[0] - now_ns) / 1_000_000.0
            if 0.0 < lead_ms <= horizon_ms:
                row["positive"] = True
                row["intent_label"] = "FAILED_ATTEMPT"
                row["intent_ns"] = future_attempts[0]
                row["lead_ms"] = lead_ms
                row["failed_e4_intent_target"] = True
    return rows


golden.build_dataset = build_dataset_failed_aware_v2


if __name__ == "__main__":
    raise SystemExit(golden.main())

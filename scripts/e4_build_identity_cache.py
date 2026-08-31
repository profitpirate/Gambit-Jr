#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping


def finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def creator_profile(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    entries = len(rows)
    costs = [value for row in rows if (value := finite(row.get("sol_cost"))) is not None]
    direct = sum(
        not any(str(program).startswith("AZiv") for program in row.get("programs") or [])
        for row in rows
    )
    same_slot = sum(int(row.get("slot_delay") or 0) == 0 for row in rows)
    sparse_public = sum(int(row.get("noncreator_buyers_before_entry") or 0) <= 1 for row in rows)
    no_early_sells = sum(int(row.get("sells_before_entry") or 0) == 0 for row in rows)
    j7 = sum(str(row.get("metadata_host") or "") == "metadata.j7tracker.io" for row in rows)

    repeat_score = min(1.0, math.log2(entries + 1) / 3.0)
    median_cost = statistics.median(costs) if costs else 0.0
    size_score = min(1.0, median_cost / 8.0)
    direct_ratio = direct / entries
    same_slot_ratio = same_slot / entries
    sparse_ratio = sparse_public / entries
    clean_ratio = no_early_sells / entries
    j7_ratio = j7 / entries

    # This is an E4-selection-affinity score, not a token-success probability.
    # One-off creators cannot exceed the known-creator threshold merely because
    # E4 once selected them. Repeat use is required for production activation.
    affinity = (
        0.40 * repeat_score
        + 0.18 * direct_ratio
        + 0.15 * same_slot_ratio
        + 0.10 * sparse_ratio
        + 0.10 * size_score
        + 0.05 * clean_ratio
        + 0.02 * j7_ratio
    )
    if entries < 2:
        affinity = min(0.70, affinity)
    elif entries == 2:
        affinity = min(0.86, max(0.75, affinity))
    elif entries >= 3:
        affinity = min(0.97, max(0.82, affinity))

    return {
        "confidence": round(affinity, 6),
        "meaning": "E4 historical creator-selection affinity; not win probability",
        "e4_entries": entries,
        "median_entry_cost_sol": median_cost,
        "direct_pump_ratio": direct_ratio,
        "same_slot_ratio": same_slot_ratio,
        "creator_or_near_creator_only_ratio": sparse_ratio,
        "no_early_sell_ratio": clean_ratio,
        "j7_metadata_ratio": j7_ratio,
    }


def build(source: Mapping[str, Any]) -> dict[str, Any]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in source.get("entries") or []:
        if not isinstance(row, Mapping):
            continue
        creator = str(row.get("creator") or "").strip()
        if creator:
            grouped[creator].append(row)

    profiles = {
        creator: creator_profile(rows)
        for creator, rows in grouped.items()
    }
    active = {
        creator: profile
        for creator, profile in profiles.items()
        if profile["e4_entries"] >= 2 and profile["confidence"] >= 0.75
    }
    return {
        "schema": "gambit-e4-creator-affinity-v1",
        "source_report_version": source.get("report_version"),
        "source_entries": source.get("unique_entries"),
        "profiles": profiles,
        "production_cache": active,
        "profile_count": len(profiles),
        "production_cache_count": len(active),
        "warning": "Positive-selection affinity only. Production entry still requires post-create confirmation unless explicitly prearmed.",
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Build conservative E4 creator affinity cache")
    value.add_argument("--input", type=Path, required=True)
    value.add_argument("--output", type=Path, required=True)
    value.add_argument("--cache-output", type=Path)
    return value


def main() -> int:
    args = parser().parse_args()
    result = build(json.loads(args.input.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    cache = args.cache_output or args.output.with_name("creator-profiles.json")
    cache.write_text(json.dumps(result["production_cache"], indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "cache": str(cache),
                "profiles": result["profile_count"],
                "active": result["production_cache_count"],
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path

import e4_v12_conclusive_entry_rerun as base


def main() -> int:
    parser = argparse.ArgumentParser(description="Export strictly causal E4 creator/buyer memory for V12 runtime")
    parser.add_argument("--pair", action="append", default=[], metavar="BATCH:EVENTS")
    parser.add_argument("--attempts", action="append", default=[], type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.pair:
        parser.error("at least one chronological live sample is required")

    launches, run_ids = base.load_launches([base.parse_pair(value) for value in args.pair])
    failed = base.load_failed_attempts(args.attempts)
    rows = base.build_snapshots(launches, failed)
    positives = sorted(
        (row for row in rows if row.get("positive")),
        key=lambda row: (base.integer(row.get("timestamp_ns")), str(row.get("mint") or "")),
    )

    creators: dict[str, Counter[str]] = {}
    buyers: dict[str, Counter[str]] = {}
    creator_buyer: Counter[str] = Counter()
    signature_shapes: Counter[str] = Counter()

    for row in positives:
        creator = str(row.get("creator") or "")
        label = str(row.get("label") or "")
        if creator:
            counter = creators.setdefault(creator, Counter())
            counter["attempts"] += 1
            counter["successes"] += int(label == "SUCCESS")
            counter["failed_attempts"] += int(label == "FAILED_ATTEMPT")
        for buyer in row.get("first_buyers") or []:
            buyer = str(buyer or "")
            if not buyer:
                continue
            counter = buyers.setdefault(buyer, Counter())
            counter["attempts"] += 1
            counter["successes"] += int(label == "SUCCESS")
            if creator:
                creator_buyer[f"{creator}|{buyer}"] += 1
        shape = f"{base.integer(row.get('max_buys_one_signature'))}|{base.integer(row.get('create_signature_buys'))}"
        signature_shapes[shape] += 1

    payload = {
        "version": "e4-v12-causal-runtime-state-v1",
        "generated_at_unix": int(time.time()),
        "source_runs": run_ids,
        "cutoff_rule": "All counters contain only E4 intentions observed before deployment; future runtime updates occur after each intention timestamp.",
        "coverage": {
            "launches": len(launches),
            "intent_rows": len(positives),
            "successful_entries": sum(row.get("label") == "SUCCESS" for row in positives),
            "failed_attempts": sum(row.get("label") == "FAILED_ATTEMPT" for row in positives),
            "creators": len(creators),
            "first_buyers": len(buyers),
            "creator_buyer_pairs": len(creator_buyer),
            "signature_shapes": len(signature_shapes),
        },
        "creators": {key: dict(value) for key, value in creators.items()},
        "buyers": {key: dict(value) for key, value in buyers.items()},
        "creator_buyer_pairs": dict(creator_buyer),
        "signature_shapes": dict(signature_shapes),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload["coverage"], indent=2, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

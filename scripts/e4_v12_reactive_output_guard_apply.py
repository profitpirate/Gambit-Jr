#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

import e4_v12_reactive_output_guard_search as search
import e4_v12_true_latency_replay as replay


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply a frozen E4-confirmed output-guard thesis")
    parser.add_argument("--pair", action="append", default=[])
    parser.add_argument("--rule", type=Path, required=True)
    parser.add_argument("--latencies-ms", default="0,1,2,5,10")
    parser.add_argument("--minimum-live-trades", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    frozen = json.loads(args.rule.read_text(encoding="utf-8"))
    if frozen.get("status") != "HISTORICAL_GOLDEN_CONFIRMED":
        raise SystemExit("reactive thesis was not historically confirmed")
    rule = search.Rule(**frozen["rule"])
    runs = [replay.load_run(*replay.parse_pair(value)) for value in args.pair]
    if not runs:
        parser.error("at least one --pair is required")
    candidates = search.build_candidates(runs)
    live_id = runs[-1].run_id
    live = [row for row in candidates if row.run_id == live_id]
    run_map = {run.run_id: run for run in runs}
    latencies = [search.finite(value) for value in args.latencies_ms.split(",") if value.strip()]
    grid = {
        str(latency): search.evaluate(run_map, live, rule, latency)
        for latency in latencies
    }
    passed = search.passes(grid, args.minimum_live_trades)
    payload = {
        "version": "e4-v12-reactive-output-guard-live-v1",
        "status": "FRESH_LIVE_GOLDEN_CONFIRMED" if passed else "NOT_CONCLUSIVE",
        "live_run": live_id,
        "rule": rule.as_dict(),
        "live_candidates": len(live),
        "economics": search.compact(grid),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

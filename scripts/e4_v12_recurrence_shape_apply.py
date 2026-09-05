#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

import e4_v12_recurrence_shape_search as search
import e4_v12_true_latency_replay as replay


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply a frozen creator-buyer recurrence thesis")
    parser.add_argument("--pair", action="append", default=[])
    parser.add_argument("--rule", type=Path, required=True)
    parser.add_argument("--latencies-ms", default="0,1,2,5,10")
    parser.add_argument("--minimum-live-trades", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    frozen = json.loads(args.rule.read_text(encoding="utf-8"))
    if frozen.get("status") != "HISTORICAL_GOLDEN_CONFIRMED":
        raise SystemExit("recurrence thesis was not historically confirmed")
    rule = search.Rule(**frozen["rule"])
    runs = [replay.load_run(*replay.parse_pair(value)) for value in args.pair]
    if not runs:
        parser.error("at least one --pair is required")
    rows = search.build_snapshots(runs)
    live_index = len(runs) - 1
    live_rows = [row for row in rows if search.integer(row.get("run_index")) == live_index]
    predictions = search.select(live_rows, rule)
    run_map = {run.run_id: run for run in runs}
    latencies = [search.finite(value) for value in args.latencies_ms.split(",") if value.strip()]
    economics = search.grid(run_map, predictions, rule, latencies)
    passed = search.passes(economics, args.minimum_live_trades)
    payload = {
        "version": "e4-v12-recurrence-shape-live-v1",
        "status": "FRESH_LIVE_GOLDEN_CONFIRMED" if passed else "NOT_CONCLUSIVE",
        "live_run": runs[-1].run_id,
        "live_snapshots": len(live_rows),
        "predictions": predictions,
        "rule": rule.as_dict(),
        "economics": search.compact(economics),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "status": payload["status"],
        "live_run": payload["live_run"],
        "predictions": len(predictions),
        "economics": payload["economics"],
    }, indent=2, sort_keys=True), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

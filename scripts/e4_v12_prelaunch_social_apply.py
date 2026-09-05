#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import e4_v12_golden_thesis_search_v2 as golden
import e4_v12_prelaunch_social_search as search
import e4_v12_true_latency_replay as replay


async def async_main(args: argparse.Namespace) -> int:
    frozen = json.loads(args.rule.read_text(encoding="utf-8"))
    if frozen.get("status") != "HISTORICAL_GOLDEN_CONFIRMED":
        raise SystemExit("prelaunch-social thesis was not historically confirmed")
    rule = search.Rule(**frozen["rule"])
    runs = [replay.load_run(*replay.parse_pair(value)) for value in args.pair]
    if not runs:
        raise SystemExit("at least one --pair is required")
    launches = search.launch_rows(runs)
    metadata = search.scan_cache(args.metadata_cache)
    for launch in launches.values():
        if launch.get("embedded_social") and launch["mint"] not in metadata:
            metadata[launch["mint"]] = dict(launch["embedded_social"])
    metadata = await search.fill_metadata(
        launches,
        metadata,
        concurrency=args.metadata_concurrency,
        timeout_seconds=args.metadata_timeout_seconds,
    )
    snapshots = search.build_snapshots(runs, launches, metadata)
    live_index = len(runs) - 1
    live_rows = [row for row in snapshots if search.integer(row.get("run_index")) == live_index]
    predictions = search.select(live_rows, rule)
    run_map = {run.run_id: run for run in runs}
    latencies = [search.finite(value) for value in args.latencies_ms.split(",") if value.strip()]
    economics = golden.economic_grid(
        run_map,
        predictions,
        floor_bps=rule.output_shortfall_bps,
        latencies=latencies,
    )
    passed = golden.economics_pass(economics, args.minimum_live_trades)
    payload = {
        "version": "e4-v12-prelaunch-social-live-v1",
        "status": "FRESH_LIVE_GOLDEN_CONFIRMED" if passed else "NOT_CONCLUSIVE",
        "live_run": runs[-1].run_id,
        "live_social_snapshots": len(live_rows),
        "predictions": predictions,
        "rule": rule.as_dict(),
        "economics": search.compact(economics),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    args.metadata_output.parent.mkdir(parents=True, exist_ok=True)
    args.metadata_output.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "status": payload["status"],
        "live_run": payload["live_run"],
        "live_social_snapshots": len(live_rows),
        "predictions": len(predictions),
        "economics": payload["economics"],
    }, indent=2, sort_keys=True), flush=True)
    return 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply frozen prelaunch social-intent thesis")
    parser.add_argument("--pair", action="append", default=[])
    parser.add_argument("--rule", type=Path, required=True)
    parser.add_argument("--metadata-cache", action="append", default=[], type=Path)
    parser.add_argument("--metadata-output", type=Path, required=True)
    parser.add_argument("--metadata-concurrency", type=int, default=128)
    parser.add_argument("--metadata-timeout-seconds", type=float, default=2.5)
    parser.add_argument("--latencies-ms", default="0,1,2,5,10")
    parser.add_argument("--minimum-live-trades", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    return asyncio.run(async_main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

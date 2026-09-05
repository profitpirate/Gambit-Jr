#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

import e4_v12_golden_thesis_search_v2 as golden
import e4_v12_true_latency_replay as replay


def load_search_module():
    source = Path(__file__).with_name("e4_v12_prior_whitelist_search.py")
    text = source.read_text(encoding="utf-8")
    broken = '''            if (
                not state.creator
                or state.sell_count > 0
                or state.fdV_usd <= 0 if False else False
            ):
                continue
'''
    repaired = '''            if not state.creator or state.sell_count > 0:
                continue
'''
    if broken not in text:
        raise RuntimeError("expected whitelist eligibility guard was not found")
    text = text.replace(broken, repaired)
    temporary = Path(tempfile.gettempdir()) / "e4_v12_prior_whitelist_search_repaired.py"
    temporary.write_text(text, encoding="utf-8")
    name = "e4_v12_prior_whitelist_search_repaired"
    spec = importlib.util.spec_from_file_location(name, temporary)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load repaired whitelist module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply a frozen causal prior-whitelist thesis")
    parser.add_argument("--pair", action="append", default=[])
    parser.add_argument("--prior-registry", type=Path, required=True)
    parser.add_argument("--rule", type=Path, required=True)
    parser.add_argument("--latencies-ms", default="0,1,2,5,10")
    parser.add_argument("--minimum-live-trades", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    search = load_search_module()
    frozen = json.loads(args.rule.read_text(encoding="utf-8"))
    if frozen.get("status") != "HISTORICAL_GOLDEN_CONFIRMED":
        raise SystemExit("prior-whitelist thesis was not historically confirmed")
    rule = search.Rule(**frozen["rule"])
    runs = [replay.load_run(*replay.parse_pair(value)) for value in args.pair]
    if not runs:
        parser.error("at least one --pair is required")
    rows = search.build_snapshots(runs, args.prior_registry)
    live_index = len(runs) - 1
    live_rows = [row for row in rows if search.integer(row.get("run_index")) == live_index]
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
        "version": "e4-v12-prior-whitelist-live-v1",
        "status": "FRESH_LIVE_GOLDEN_CONFIRMED" if passed else "NOT_CONCLUSIVE",
        "live_run": runs[-1].run_id,
        "live_snapshots": len(live_rows),
        "predictions": predictions,
        "rule": rule.as_dict(),
        "prior_registry_ref": json.loads(args.prior_registry.read_text(encoding="utf-8")).get("causal_ref"),
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

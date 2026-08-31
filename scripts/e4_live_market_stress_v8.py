#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence


def _load_v7():
    path = Path(__file__).with_name("e4_live_market_stress_v7.py")
    name = "e4_live_market_stress_v8_base"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


V7 = _load_v7()
_TARGET_LAUNCHES = 300
_ORIGINAL_CAPTURE = V7.BASE.capture_native_pump
_ORIGINAL_GATES = V7.market_gates


async def _capture_exact_launches(seconds: float, ws_urls: Sequence[str]):
    events, diagnostics = await _ORIGINAL_CAPTURE(seconds, ws_urls)
    ordered_mints: list[str] = []
    seen: set[str] = set()
    for event in sorted(events, key=lambda item: (item.received_ns, item.slot, item.event_index)):
        if event.kind != V7.core.EventKind.CREATE.value or event.mint in seen:
            continue
        seen.add(event.mint)
        ordered_mints.append(event.mint)
    captured_before_cap = len(ordered_mints)
    selected = set(ordered_mints[:_TARGET_LAUNCHES])
    if captured_before_cap >= _TARGET_LAUNCHES:
        events = [event for event in events if event.mint in selected]
    diagnostics = {
        **dict(diagnostics),
        "captured_launches_before_exact_cap": captured_before_cap,
        "exact_launch_target": _TARGET_LAUNCHES,
        "exact_launch_cap_applied": captured_before_cap >= _TARGET_LAUNCHES,
    }
    print(
        json.dumps(
            {
                "event": "exact_launch_sample_prepared",
                "captured_before_cap": captured_before_cap,
                "tested_launches": min(captured_before_cap, _TARGET_LAUNCHES),
                "target": _TARGET_LAUNCHES,
            },
            separators=(",", ":"),
        ),
        flush=True,
    )
    return events, diagnostics


async def _disable_backfill_for_live_holdout(*_: Any, **__: Any):
    # Certification is intentionally based on launches observed live by the
    # streaming path. A degraded stream must fail rather than silently replace
    # the sample with retrospective RPC data.
    return []


def _exact_market_gates(capture, builder, primary, fresh):
    result = _ORIGINAL_GATES(capture, builder, primary, fresh)
    observed = int(capture.get("new_launches") or 0)
    passed = observed == _TARGET_LAUNCHES
    result["checks"].insert(
        0,
        {
            "name": "exact_live_launch_sample",
            "passed": passed,
            "detail": f"launches={observed} target={_TARGET_LAUNCHES}",
        },
    )
    if not passed and "exact_live_launch_sample" not in result["failed"]:
        result["failed"].insert(0, "exact_live_launch_sample")
    result["market_hypothesis_pass"] = not result["failed"]
    result["classification"] = (
        "MARKET_HYPOTHESIS_PASS"
        if result["market_hypothesis_pass"]
        else "MARKET_HYPOTHESIS_FAIL"
    )
    return result


V7.BASE.capture_native_pump = _capture_exact_launches
V7.BASE.backfill_pump_events = _disable_backfill_for_live_holdout
V7.market_gates = _exact_market_gates


if __name__ == "__main__":
    args = V7.parser().parse_args()
    _TARGET_LAUNCHES = int(args.minimum_launches)
    os.environ["E4_EXACT_LAUNCHES"] = str(_TARGET_LAUNCHES)
    raise SystemExit(asyncio.run(V7.run_with_heartbeat(args)))

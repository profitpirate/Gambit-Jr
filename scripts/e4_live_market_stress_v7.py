#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path

# Apply the full production policy, lifecycle and official-SDK builder patches
# before loading the existing real-market harness.
from memecoin_bot import e4_hardening_v7  # noqa: F401


def _load_base():
    path = Path(__file__).with_name("e4_live_market_stress.py")
    name = "e4_live_market_stress_v7_base"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


async def _run_with_heartbeat(base, args) -> int:
    task = asyncio.create_task(base.main_async(args))
    started = time.monotonic()
    heartbeat = 0
    while not task.done():
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=30.0)
        except asyncio.TimeoutError:
            heartbeat += 1
            print(
                json.dumps(
                    {
                        "event": "e4_v7_certification_heartbeat",
                        "heartbeat": heartbeat,
                        "elapsed_seconds": round(time.monotonic() - started, 1),
                        "capture_target_seconds": args.capture_seconds,
                        "minimum_real_launches": args.minimum_launches,
                    },
                    separators=(",", ":"),
                ),
                flush=True,
            )
    return await task


if __name__ == "__main__":
    base = _load_base()
    arguments = base.parser().parse_args()
    raise SystemExit(asyncio.run(_run_with_heartbeat(base, arguments)))

#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

# Apply the current production E4 hardening before loading the shared live
# harness. V6 includes V5 lifecycle/receipt fixes plus evidence-backed entry,
# relative sizing, confidence partials and local-builder warmup.
from memecoin_bot import e4_hardening_v6  # noqa: F401


def _load_base():
    path = Path(__file__).with_name("e4_live_market_stress.py")
    name = "e4_live_market_stress_base"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    base = _load_base()
    raise SystemExit(asyncio.run(base.main_async(base.parser().parse_args())))

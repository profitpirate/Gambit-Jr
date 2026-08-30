#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

# Apply every production hardening patch before loading the base harness. The
# base module imports v2 for helper aliases; Python's module cache preserves the
# v3 policy and receipt-clock patches applied here.
from memecoin_bot import e4_hardening_v3  # noqa: F401


def _load_base():
    path = Path(__file__).with_name("e4_live_market_stress.py")
    spec = importlib.util.spec_from_file_location("e4_live_market_stress_base", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    base = _load_base()
    raise SystemExit(asyncio.run(base.main_async(base.parser().parse_args())))

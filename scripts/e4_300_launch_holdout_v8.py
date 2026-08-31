#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

from memecoin_bot import e4_hardening_v8 as v8


def load_holdout():
    path = Path(__file__).with_name("e4_300_launch_holdout.py")
    name = "e4_300_launch_holdout_v8_base"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    holdout = load_holdout()
    base = holdout.load_base()
    previous_anchor_to_live = base.anchor_to_live

    def anchor_to_live_v8(item, *args, **kwargs):
        event = previous_anchor_to_live(item, *args, **kwargs)
        if event is not None:
            # CreateEvent raw fields include creator/URI/token-program information.
            # This keeps the live holdout semantically aligned with production
            # canonical rows where V8 receives those facts through Event.from_row.
            v8.observe_context(event.mint, item)
        return event

    base.anchor_to_live = anchor_to_live_v8
    holdout.load_base = lambda: base
    raise SystemExit(asyncio.run(holdout.main()))

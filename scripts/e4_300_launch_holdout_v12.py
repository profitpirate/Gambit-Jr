#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

from memecoin_bot import e4_hardening_v12 as v12


def load_holdout():
    path = Path(__file__).with_name("e4_300_launch_holdout.py")
    name = "e4_300_launch_holdout_v12_base"
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

    def anchor_to_live_v12(item, *args, **kwargs):
        event = previous_anchor_to_live(item, *args, **kwargs)
        if event is None:
            return None
        v12.v8.observe_context(event.mint, item)
        context = v12.v6._CONTEXT_BY_MINT.setdefault(event.mint, {})
        for key in ("creator", "name", "symbol", "uri", "token_program", "is_mayhem_mode"):
            value = item.get(key) if isinstance(item, dict) else None
            if value not in (None, ""):
                context[key] = value
        if event.kind == base.core.EventKind.CREATE.value:
            v12.PIPELINES.observe_launch_event(
                mint=event.mint,
                creator=event.creator or str(context.get("creator") or ""),
                received_ns=event.received_ns,
                price_sol=event.price_sol or 0.0,
            )
        elif event.kind in {base.core.EventKind.BUY.value, base.core.EventKind.SELL.value}:
            v12.PIPELINES.observe_trade_event(
                mint=event.mint,
                received_ns=event.received_ns,
                price_sol=event.price_sol or 0.0,
                is_buy=event.kind == base.core.EventKind.BUY.value,
            )
        return event

    base.anchor_to_live = anchor_to_live_v12
    holdout.load_base = lambda: base
    raise SystemExit(asyncio.run(holdout.main()))

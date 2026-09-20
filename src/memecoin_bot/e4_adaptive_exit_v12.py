"""Post-certification adaptive winner extension for V12.

Failure exits are never relaxed. Only non-adverse maximum-hold style exits can
be extended when price/flow evidence remains strong.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import e4_role_model_v12 as role_model
from .v12_creator_library import causal_100_passed

core = role_model.core
_PREVIOUS_EXIT = core.E4Policy.exit


def _enabled() -> bool:
    if os.getenv("V12_ADAPTIVE_EXIT_ENABLED", "").lower() not in {"1", "true", "yes", "on"}:
        return False
    path = Path(
        os.getenv(
            "V12_CAUSAL_CERTIFICATION_STATE_PATH",
            "research/v12-pre-armed-100-causal-paper-live.json",
        )
    )
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return causal_100_passed(state)


def _adverse(reason: str) -> bool:
    text = reason.lower()
    return any(
        marker in text
        for marker in (
            "adverse",
            "failure",
            "flow-break",
            "flow break",
            "rug",
            "stop",
            "emergency",
            "stale",
            "watchdog",
            "drawdown",
        )
    )


def _exit_adaptive(self: Any, position: Any, state: Any):
    action, fraction, reason = _PREVIOUS_EXIT(self, position, state)
    if not _enabled():
        return action, fraction, reason
    if action.startswith("SELL") and _adverse(str(reason)):
        return action, fraction, reason

    age_ms = int(getattr(position, "age_ms", 0))
    price = float(getattr(state, "price_sol", 0.0) or getattr(position, "last_price", 0.0) or 0.0)
    entry = float(getattr(position, "entry_price", 0.0) or 0.0)
    max_price = float(getattr(position, "max_price", price) or price)
    if price <= 0 or entry <= 0:
        return action, fraction, reason
    markout_bps = (price / entry - 1.0) * 10_000.0
    drawdown_bps = (1.0 - price / max(max_price, 1e-18)) * 10_000.0
    flow250 = state.flow(250)
    flow1000 = state.flow(1000)
    strong = (
        markout_bps > 0
        and flow250.net >= 0
        and flow1000.ratio >= 1.15
        and drawdown_bps < 800
    )

    if age_ms <= 2_000:
        if action.startswith("SELL") and strong:
            return "HOLD", 0.0, "V12 adaptive exit: strong survivor protected through 2s"
        return action, fraction, reason

    if age_ms <= 10_000 and strong:
        if not bool(getattr(position, "first_partial_done", False)) and markout_bps >= 900:
            return "SELL_PARTIAL", 0.30, "V12 adaptive exit: bank 30% strong survivor"
        return "HOLD", 0.0, "V12 adaptive exit: positive flow extension"

    if age_ms > 10_000:
        return "SELL_ALL", 1.0, "V12 adaptive exit: hard 10s post-cert ceiling"

    if bool(getattr(position, "first_partial_done", False)) and (
        drawdown_bps >= 800 or flow1000.ratio < 0.85
    ):
        return "SELL_ALL", 1.0, "V12 adaptive exit: survivor flow/drawdown break"

    return action, fraction, reason


core.E4Policy.exit = _exit_adaptive

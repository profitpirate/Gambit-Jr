from __future__ import annotations

import logging
import time
from typing import Any, Mapping

from . import e4_fast_execution_v10 as fast
from .e4_pipelines_v10 import E4_WALLET

core = fast.core
v6 = fast.v6
LOGGER = logging.getLogger("gambit.e4.copy_exit.v10")

_PREVIOUS_FROM_ROW = core.Event.from_row.__func__
_PREVIOUS_EXIT = core.E4Policy.exit


def _kind(value: Any) -> str:
    return str(getattr(value, "value", value) or "").upper()


def _observe_e4_event(event: Any) -> None:
    if str(getattr(event, "trader", "") or "") != E4_WALLET:
        return
    kind = _kind(getattr(event, "kind", None))
    if kind not in {"BUY", "SELL"}:
        return
    mint = str(getattr(event, "mint", "") or "")
    if not mint:
        return
    signature = str(getattr(event, "signature", "") or "")
    context = v6._CONTEXT_BY_MINT.setdefault(mint, {})
    seen = context.setdefault("e4_copy_event_keys", set())
    event_key = f"{kind}:{signature}:{getattr(event, 'event_index', '')}:{getattr(event, 'token_amount', '')}"
    if event_key in seen:
        return
    seen.add(event_key)
    if len(seen) > 256:
        context["e4_copy_event_keys"] = set(list(seen)[-128:])
    tokens = max(0.0, float(getattr(event, "token_amount", 0.0) or 0.0))
    observed_ns = int(getattr(event, "received_ns", 0) or time.time_ns())
    if kind == "BUY":
        context["e4_copy_bought_tokens"] = float(context.get("e4_copy_bought_tokens") or 0.0) + tokens
        context["e4_copy_last_buy_ns"] = observed_ns
        return
    bought = float(context.get("e4_copy_bought_tokens") or 0.0)
    sold = float(context.get("e4_copy_sold_tokens") or 0.0) + tokens
    context["e4_copy_sold_tokens"] = sold
    context["e4_copy_last_sell_tokens"] = tokens
    context["e4_copy_last_sell_ns"] = observed_ns
    context["e4_copy_sell_events"] = int(context.get("e4_copy_sell_events") or 0) + 1
    context["e4_copy_cumulative_sell_fraction"] = min(1.0, sold / bought) if bought > 0 else 0.0
    context["e4_copy_latest_sell_fraction"] = min(1.0, tokens / bought) if bought > 0 else 0.0


def _from_row_copy_v10(cls: type[core.Event], row: Mapping[str, Any]) -> core.Event:
    event = _PREVIOUS_FROM_ROW(cls, row)
    _observe_e4_event(event)
    return event


core.Event.from_row = classmethod(_from_row_copy_v10)


def _exit_copy_v10(self: core.E4Policy, position: core.Position, state: core.TokenState):
    profile = v6._PROFILE_BY_MINT.get(position.mint)
    family = str(getattr(profile, "family", "") or "")
    if family != "e4_teacher_confirmed_copy_safe":
        return _PREVIOUS_EXIT(self, position, state)
    context = v6._CONTEXT_BY_MINT.get(position.mint, {})
    cumulative = float(context.get("e4_copy_cumulative_sell_fraction") or 0.0)
    latest = float(context.get("e4_copy_latest_sell_fraction") or 0.0)
    sell_events = int(context.get("e4_copy_sell_events") or 0)
    if cumulative >= 0.90 or latest >= 0.50:
        return "SELL_ALL", 1.0, f"E4 copy exit mirror: cumulative={cumulative:.2%} latest={latest:.2%}"
    if sell_events > 0 and not position.first_partial_done:
        fraction = float(getattr(profile, "first_partial_fraction", 0.30) or 0.30)
        return "SELL_PARTIAL", fraction, f"E4 copy first-sell mirror: fraction={fraction:.2%}"
    return _PREVIOUS_EXIT(self, position, state)


core.E4Policy.exit = _exit_copy_v10

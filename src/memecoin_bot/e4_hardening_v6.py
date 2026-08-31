from __future__ import annotations

import asyncio

from . import e4_hardening_v5

core = e4_hardening_v5.core


# A new TokenState represents a new in-memory lifecycle (including restart and
# isolated stress fixtures). Clear the older module-level economic-event dedupe
# bucket so an identical mint/signature from a previous state cannot suppress a
# legitimate event in the new state.
_previous_token_state_init = core.TokenState.__init__


def _token_state_init_v6(self, *args, **kwargs) -> None:
    _previous_token_state_init(self, *args, **kwargs)
    e4_hardening_v5.e4_hardening_v4.hardening._STATE_EVENT_KEYS.pop(self.mint, None)


core.TokenState.__init__ = _token_state_init_v6


# Some deterministic execution harnesses construct Engine instances without
# calling the patched __init__. Production always calls it, but execution
# safety should not depend on construction style. Lazily provision the lock map
# before entering the v5 per-mint serialization boundary.
_previous_execute_sell = core.Engine.execute_sell


async def _execute_sell_v6(self, position, fraction: float, reason: str) -> None:
    if getattr(self, "position_locks", None) is None:
        self.position_locks = {}
    await _previous_execute_sell(self, position, fraction, reason)


core.Engine.execute_sell = _execute_sell_v6

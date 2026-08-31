#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


def main() -> int:
    path = Path("src/memecoin_bot/e4_hardening_v10.py")
    text = path.read_text(encoding="utf-8")
    marker = '''core.Event.from_row = classmethod(_from_row_v10)


def _entry_v10(
'''
    if marker not in text:
        if "def _apply_v10" in text:
            print("V10 state observer already applied")
            return 0
        raise RuntimeError("V10 state observer insertion marker not found")
    insertion = '''core.Event.from_row = classmethod(_from_row_v10)


_previous_state_apply = core.TokenState.apply


def _apply_v10(self: core.TokenState, event: core.Event, wallet: str | None):
    result = _previous_state_apply(self, event, wallet)
    # This is the lowest-latency production/capture common point. A Pump trade
    # from E4 is visible here before the policy evaluates the updated state, so
    # the guarded copy-confirmation path does not wait for an RPC history call.
    if event.trader == E4_WALLET:
        if event.kind == core.EventKind.BUY.value:
            PIPELINES.observe_e4_entry(
                {
                    "mint": event.mint,
                    "creator": self.creator or "",
                    "observed_ns": event.received_ns,
                    "entry_price_sol": event.price_sol or self.price_sol or 0.0,
                    "entry_sol": event.sol_amount,
                    "signature": event.signature,
                }
            )
        elif event.kind == core.EventKind.SELL.value:
            PIPELINES.observe_e4_exit(event.mint)
    return result


core.TokenState.apply = _apply_v10


def _entry_v10(
'''
    path.write_text(text.replace(marker, insertion, 1), encoding="utf-8")
    print("patched TokenState.apply with direct E4 trade observation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

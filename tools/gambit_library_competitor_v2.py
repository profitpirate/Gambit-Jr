#!/usr/bin/env python3
"""Causal horizon fix for the Phase-3 developer-library paper competitor.

The v1 runner required a market event at/after the 3s horizon. Quiet coins therefore
became UNKNOWN even when the last observed bonding-curve state was still the valid
state at 3s. This wrapper preserves the tiny frozen policy and paper-only execution,
but records event states while each 3s observation is active and selects the latest
valid state at or before the horizon. It never uses a post-horizon state. Quiet paper
positions are also advanced by the maintenance clock when their market state has not
changed, so a lack of a new swap event is not mistaken for a failed fill or exit.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

import gambit_library_competitor as base

VERSION = "gambit-library-forward-paper-v2-causal-horizon"


class CausalLatest(dict[str, dict[str, Any]]):
    """Normal latest-state map plus short-lived per-mint causal history."""

    def __init__(self) -> None:
        super().__init__()
        self.tracking: set[str] = set()
        self.history: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def __setitem__(self, mint: str, state: dict[str, Any]) -> None:
        super().__setitem__(mint, state)
        if mint in self.tracking:
            self.history[mint].append(dict(state))

    def begin(self, mint: str, initial: dict[str, Any]) -> None:
        self.tracking.add(mint)
        self.history[mint] = [dict(initial)]

    def state_at_or_before(self, mint: str, horizon_ns: int) -> dict[str, Any] | None:
        candidates = [s for s in self.history.get(mint, ()) if int(s.get("ns", 0)) <= horizon_ns]
        if not candidates:
            return None
        return max(candidates, key=lambda s: int(s.get("ns", 0)))

    def end(self, mint: str) -> None:
        self.tracking.discard(mint)
        self.history.pop(mint, None)


def usable_horizon_state(s0: dict[str, Any] | None, s1: dict[str, Any] | None,
                         horizon_ns: int) -> bool:
    """Return True only for a valid, causal state no later than the horizon."""
    return bool(base.valid(s0) and base.valid(s1)
                and int(s1.get("ns", 0)) >= int(s0.get("ns", 0))
                and int(s1.get("ns", 0)) <= horizon_ns)


class Runner(base.Runner):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.latest = CausalLatest()

    async def resolve_observation(self, mint: str) -> None:
        launch = self.launches[mint]
        due = launch["create_ns"] + self.library.c.horizon_ms * 1_000_000
        self.latest.begin(mint, launch["state"])
        try:
            delay = max(0, (due - base.time.time_ns()) / 1e9)
            if delay:
                await base.asyncio.sleep(delay)
            s0 = launch["state"]
            s1 = self.latest.state_at_or_before(mint, due)
            if not usable_horizon_state(s0, s1, due):
                self.counts["observation_unknown"] += 1
                self.emit("OBSERVATION_UNKNOWN", mint, {
                    "creator": launch["creator"],
                    "reason": "NO_VALID_CAUSAL_3S_STATE",
                    "horizon_ns": due,
                })
                return
            try:
                stake = self.library.c.observation_stake_sol
                tok = base.buy_quote(stake, s0)
                proceeds = base.sell_quote(tok, s1, stake, -tok)
                ret = proceeds / stake - 1
            except ValueError:
                self.counts["observation_unknown"] += 1
                self.emit("OBSERVATION_UNKNOWN", mint, {
                    "creator": launch["creator"],
                    "reason": "QUOTE_UNAVAILABLE",
                    "horizon_ns": due,
                    "hold_state_ns": int(s1.get("ns", 0)),
                })
                return
            updated = self.library.update(mint=mint, creator=launch["creator"], return_fraction=ret)
            if updated:
                self.counts["observation_resolved"] += 1
            self.emit("OBSERVATION_RESOLVED", mint, {
                "creator": launch["creator"],
                "return_fraction": ret,
                "outcome": "WIN" if ret > 0 else "LOSS" if ret < 0 else "BREAKEVEN",
                "horizon_ns": due,
                "hold_state_ns": int(s1.get("ns", 0)),
                "hold_state_age_ms": (due - int(s1.get("ns", 0))) / 1e6,
                "developer_stats_after": self.library.stats(launch["creator"]),
            })
        finally:
            self.latest.end(mint)

    def maintenance(self) -> None:
        """Advance timers on unchanged states, then apply v1 stale-path safeguards."""
        now = base.time.time_ns()
        for mint, p in list(self.positions.items()):
            if p.get("status") not in ("PENDING", "OPEN", "EXIT_PENDING"):
                continue
            state = self.latest.get(mint)
            if not base.valid(state):
                continue
            try:
                self.tick_position(mint, state, now)
            except ValueError:
                # A curve that can no longer quote remains subject to the v1
                # unknown/unresolved safeguards below; never fabricate a fill.
                pass
        super().maintenance()


def main() -> int:
    base.Runner = Runner
    base.VERSION = VERSION
    return base.main()


if __name__ == "__main__":
    raise SystemExit(main())

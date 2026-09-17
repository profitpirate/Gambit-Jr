#!/usr/bin/env python3
"""Regression checks for causal Phase-3 horizon resolution."""
from gambit_library_competitor_v2 import CausalLatest, usable_horizon_state


def state(ns: int, vsol: float = 30.0, vtok: float = 1_000_000.0):
    return {"ns": ns, "signature": "s", "slot": 1, "complete": False,
            "vsol": vsol, "vtok": vtok, "rtok": 800_000.0}


def main() -> int:
    create_ns = 1_000_000_000
    due = create_ns + 3_000_000_000
    initial = state(create_ns)
    latest = CausalLatest()
    latest.begin("quiet", initial)

    # No event at/after 3s: the last causal state remains the market state at 3s.
    quiet = latest.state_at_or_before("quiet", due)
    assert quiet == initial
    assert usable_horizon_state(initial, quiet, due)

    # A pre-horizon update supersedes create state.
    before = state(due - 1, 31.0)
    latest["quiet"] = before
    assert latest.state_at_or_before("quiet", due) == before

    # A post-horizon update must never leak into the 3s label.
    after = state(due + 1, 40.0)
    latest["quiet"] = after
    assert latest.state_at_or_before("quiet", due) == before
    assert not usable_horizon_state(initial, after, due)

    latest.end("quiet")
    assert latest.state_at_or_before("quiet", due) is None
    print('{"status":"PASS","causal_horizon":true,"post_horizon_lookahead_blocked":true}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

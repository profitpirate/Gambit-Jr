#!/usr/bin/env python3
from __future__ import annotations

from scripts import e4_v12_market_golden as market

_ORIGINAL_POLICIES = market.policy_candidates
_ORIGINAL_MODELS = market.model_specs


def bounded_policies():
    rows = _ORIGINAL_POLICIES()
    # The first 14 are hand-designed E4-inspired partial/stop/trail policies;
    # add a deterministic spread of generated policies without creating a
    # multi-hour combinatorial search on each CI runner.
    hand = rows[:14]
    generated = rows[14:]
    spread = [generated[index] for index in range(0, len(generated), max(1, len(generated) // 10))][:10]
    return hand + spread


def bounded_models():
    rows = _ORIGINAL_MODELS()
    return [row for row in rows if row[0] in {"logit", "extra4", "extra6", "hist"}]


market.policy_candidates = bounded_policies
market.model_specs = bounded_models


if __name__ == "__main__":
    raise SystemExit(market.main())

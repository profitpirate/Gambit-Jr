#!/usr/bin/env python3
from __future__ import annotations

from typing import Any, Mapping, Sequence

from scripts import e4_v12_independent_exit_replay as replay

_ORIGINAL = replay.simulate_independent


def simulate_independent(
    prediction: replay.base.Prediction,
    rows: Sequence[Mapping[str, Any]],
    **kwargs: Any,
):
    position, status = _ORIGINAL(prediction, rows, **kwargs)
    if position is None:
        return position, status
    metadata = prediction.metadata or {}
    mode = str(prediction.mode or "").lower()
    source_sol = max(0.0, replay.finite(metadata.get("source_sol")))
    source_tokens = max(0.0, replay.finite(metadata.get("source_tokens")))
    if ("reactive" in mode or bool(metadata.get("e4_direct_copy"))) and source_sol > 0 and source_tokens > 0:
        expected = source_tokens * position.entry_curve_sol / source_sol
        deterioration = replay.base.quote_deterioration_bps(expected, position.quoted_tokens_at_fill)
        guard_bps = int(kwargs.get("max_output_shortfall_bps", 800))
        if deterioration > guard_bps + 1e-9:
            return None, "scaled_e4_output_guard_rejected"
        position.expected_tokens_at_decision = expected
        position.output_deterioration_bps = deterioration
    return position, status


replay.simulate_independent = simulate_independent


if __name__ == "__main__":
    raise SystemExit(replay.main())

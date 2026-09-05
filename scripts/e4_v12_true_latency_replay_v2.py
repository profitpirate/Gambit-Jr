#!/usr/bin/env python3
from __future__ import annotations

from typing import Any, Mapping, Sequence

from scripts import e4_v12_true_latency_replay as base

_ORIGINAL_SIMULATE_POSITION = base.simulate_position


def _finite(value: Any, default: float = 0.0) -> float:
    return base.finite(value, default)


def simulate_position(
    prediction: base.Prediction,
    rows: Sequence[Mapping[str, Any]],
    e4_position: Mapping[str, Any] | None,
    **kwargs: Any,
):
    position, status = _ORIGINAL_SIMULATE_POSITION(
        prediction,
        rows,
        e4_position,
        **kwargs,
    )
    if position is None:
        return position, status

    mode = str(prediction.mode or "").lower()
    metadata = prediction.metadata or {}
    source_sol = max(0.0, _finite(metadata.get("source_sol")))
    source_tokens = max(0.0, _finite(metadata.get("source_tokens")))
    is_reactive = "reactive" in mode or bool(metadata.get("e4_direct_copy"))
    if not is_reactive or source_sol <= 0 or source_tokens <= 0:
        return position, status

    expected = source_tokens * position.entry_curve_sol / source_sol
    deterioration = base.quote_deterioration_bps(expected, position.quoted_tokens_at_fill)
    guard_bps = int(kwargs.get("max_output_shortfall_bps", 800))
    if deterioration > guard_bps + 1e-9:
        return None, "scaled_e4_output_guard_rejected"
    position.expected_tokens_at_decision = expected
    position.output_deterioration_bps = deterioration
    return position, status


# All callers imported by the thesis search hold the same module object. Patch
# the economic primitive once so historical search, holdout and CLI execution
# share identical reactive output semantics.
base.simulate_position = simulate_position


def main() -> int:
    return base.main()


if __name__ == "__main__":
    raise SystemExit(main())

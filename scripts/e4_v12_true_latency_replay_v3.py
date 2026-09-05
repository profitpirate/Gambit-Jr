#!/usr/bin/env python3
from __future__ import annotations

from typing import Any, Mapping, Sequence

from scripts import e4_v12_true_latency_replay as base
from scripts import e4_v12_true_latency_replay_v2 as v2

_TOTAL_KEYS = (
    "total_fee_bps",
    "total_fee_basis_points",
    "fee_bps",
    "fee_basis_points",
    "feeBasisPoints",
)
_COMPONENT_KEYS = (
    "protocol_fee_bps",
    "protocol_fee_basis_points",
    "protocolFeeBps",
    "creator_fee_bps",
    "creator_fee_basis_points",
    "creatorFeeBps",
    "lp_fee_bps",
    "lp_fee_basis_points",
    "lpFeeBps",
)


def _finite(value: Any, default: float = 0.0) -> float:
    return base.finite(value, default)


def _mapping_values(value: Any):
    if isinstance(value, Mapping):
        yield value
        for item in value.values():
            if isinstance(item, Mapping):
                yield from _mapping_values(item)


def fee_bps_from_rows(
    rows: Sequence[Mapping[str, Any]],
    timestamp_ns: int,
    fallback: int,
) -> int:
    selected: int | None = None
    for row in rows:
        received = base.integer(row.get("received_ns"))
        if received > timestamp_ns:
            break
        candidates = [row]
        raw = row.get("raw")
        if isinstance(raw, Mapping):
            candidates.extend(_mapping_values(raw))
        for mapping in candidates:
            explicit = next(
                (
                    _finite(mapping.get(key), -1.0)
                    for key in _TOTAL_KEYS
                    if mapping.get(key) is not None
                ),
                -1.0,
            )
            if explicit >= 0:
                selected = int(round(explicit))
                continue
            components = [
                _finite(mapping.get(key), 0.0)
                for key in _COMPONENT_KEYS
                if mapping.get(key) is not None
            ]
            if components:
                selected = int(round(sum(components)))
    if selected is None:
        selected = int(fallback)
    return max(0, min(2_500, selected))


def simulate_position(
    prediction: base.Prediction,
    rows: Sequence[Mapping[str, Any]],
    e4_position: Mapping[str, Any] | None,
    **kwargs: Any,
):
    adjusted = dict(kwargs)
    adjusted["fee_bps"] = fee_bps_from_rows(
        rows,
        prediction.decision_ns,
        int(kwargs.get("fee_bps", 125)),
    )
    position, status = v2._ORIGINAL_SIMULATE_POSITION(
        prediction,
        rows,
        e4_position,
        **adjusted,
    )
    if position is None:
        return position, status

    mode = str(prediction.mode or "").lower()
    metadata = prediction.metadata or {}
    source_sol = max(0.0, _finite(metadata.get("source_sol")))
    source_tokens = max(0.0, _finite(metadata.get("source_tokens")))
    is_reactive = "reactive" in mode or bool(metadata.get("e4_direct_copy"))
    if is_reactive and source_sol > 0 and source_tokens > 0:
        expected = source_tokens * position.entry_curve_sol / source_sol
        deterioration = base.quote_deterioration_bps(expected, position.quoted_tokens_at_fill)
        guard_bps = int(kwargs.get("max_output_shortfall_bps", 800))
        if deterioration > guard_bps + 1e-9:
            return None, "scaled_e4_output_guard_rejected"
        position.expected_tokens_at_decision = expected
        position.output_deterioration_bps = deterioration
    return position, status


base.simulate_position = simulate_position


def main() -> int:
    return base.main()


if __name__ == "__main__":
    raise SystemExit(main())

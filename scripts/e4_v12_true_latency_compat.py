#!/usr/bin/env python3
from __future__ import annotations

"""Compatibility bridge for V12 research modules migrated in different epochs.

The authoritative reserve replay now exposes ``ReserveState``, ``simulate_one``
and ``portfolio``. Several retained research/exit modules still consume the
older ``CurveState``/``Prediction`` helpers. This bridge restores that public
surface without reverting the authoritative implementation or recycling fixed
36 ms economics.
"""

import bisect
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts import e4_v12_true_latency_replay as base

_ORIGINAL_LOAD_PREDICTIONS = base.load_predictions


@dataclass(frozen=True)
class CurveState:
    timestamp_ns: int
    virtual_sol: float
    virtual_tokens: float
    real_tokens: float
    fdv_usd: float
    sequence: int = 0
    price_sol: float = 0.0

    @property
    def received_ns(self) -> int:
        return self.timestamp_ns


@dataclass(frozen=True)
class Prediction:
    mint: str
    decision_ns: int
    requested_fraction: float
    score: float
    mode: str
    metadata: Mapping[str, Any]


@dataclass
class PositionResult:
    mint: str
    decision_ns: int
    fill_ns: int
    exit_ns: int
    mode: str
    confirmed_by_e4: bool
    source_buy_ns: int | None
    source_lead_ms: float | None
    requested_fraction: float
    entry_curve_sol: float
    entry_cost_sol: float
    expected_tokens_at_decision: float
    quoted_tokens_at_fill: float
    output_deterioration_bps: float
    token_amount: float
    proceeds_sol: float
    pnl_sol: float
    sell_count: int
    exit_reason: str
    e4_pnl_sol: float | None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def event_order(row: Mapping[str, Any]) -> tuple[int, int, int, int, str]:
    return base.event_sort_key(row)


def reserve_states(rows: Sequence[Mapping[str, Any]]) -> list[CurveState]:
    ordered = sorted((dict(row) for row in rows), key=base.event_sort_key)
    output: list[CurveState] = []
    for sequence, row in enumerate(ordered):
        state = base.reserve_from_row(row, sequence)
        if state is None:
            continue
        output.append(
            CurveState(
                timestamp_ns=state.received_ns,
                virtual_sol=state.virtual_sol,
                virtual_tokens=state.virtual_tokens,
                real_tokens=state.real_tokens,
                fdv_usd=state.fdv_usd,
                sequence=state.sequence,
                price_sol=state.price_sol,
            )
        )
    return output


def route_bid(amount_sol: float, score: float, urgent: bool = False) -> float:
    return base.fee_bid(amount_sol, score, urgent)


def affordable_curve_input(total_budget_sol: float, fee_rate: float, score: float) -> float:
    return base.curve_input_for_budget(total_budget_sol, fee_rate, score)


def quote_deterioration_bps(expected_tokens: float, current_tokens: float) -> float:
    if expected_tokens <= 0:
        return 0.0
    value = (1.0 - current_tokens / expected_tokens) * 10_000.0
    return max(-100_000.0, min(100_000.0, value))


def parse_latencies(value: str) -> list[float]:
    output: list[float] = []
    for item in str(value).split(","):
        if not item.strip():
            continue
        latency = max(0.0, base.finite(item.strip()))
        if latency not in output:
            output.append(latency)
    return output or [0.0, 1.0, 2.0, 5.0, 10.0]


def load_events(path: Path) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            mint = str(row.get("mint") or "")
            if mint:
                grouped.setdefault(mint, []).append(row)
    for rows in grouped.values():
        rows.sort(key=base.event_sort_key)
        for sequence, row in enumerate(rows):
            row["__sequence"] = sequence
    return grouped


def _legacy_predictions(path: Path) -> list[Prediction]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, Mapping):
        rows = (
            payload.get("predictions")
            or payload.get("decisions")
            or (payload.get("live_holdout") or {}).get("decisions")
            or []
        )
    else:
        rows = []
    output: list[Prediction] = []
    seen: set[tuple[str, int]] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        mint = str(raw.get("mint") or "")
        decision_ns = base.integer(raw.get("decision_ns") or raw.get("timestamp_ns"))
        key = (mint, decision_ns)
        if not mint or decision_ns <= 0 or key in seen:
            continue
        seen.add(key)
        output.append(
            Prediction(
                mint=mint,
                decision_ns=decision_ns,
                requested_fraction=min(
                    1.0,
                    max(
                        0.0,
                        base.finite(
                            raw.get("requested_fraction")
                            or raw.get("fraction")
                            or raw.get("entry_fraction"),
                            0.0185,
                        ),
                    ),
                ),
                score=min(
                    1.0,
                    max(0.0, base.finite(raw.get("score") or raw.get("probability"), 0.96)),
                ),
                mode=str(raw.get("mode") or raw.get("family") or "golden_thesis"),
                metadata=dict(raw),
            )
        )
    output.sort(key=lambda item: (item.decision_ns, item.mint))
    return output


def load_predictions(
    path: Path | None,
    runs: Mapping[str, base.RunData] | None = None,
    mode: str = "predictions",
):
    if runs is not None:
        return _ORIGINAL_LOAD_PREDICTIONS(path, runs, mode)
    if path is None:
        raise ValueError("prediction path is required")
    return _legacy_predictions(path)


def same_window_e4_positions(batch: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("mint") or ""): dict(row)
        for row in base.same_window_e4(batch)
        if str(row.get("mint") or "")
    }


def _state_before(states: Sequence[CurveState], timestamp_ns: int) -> CurveState | None:
    if not states:
        return None
    keys = [(state.timestamp_ns, state.sequence) for state in states]
    index = bisect.bisect_right(keys, (timestamp_ns, 2**62)) - 1
    return states[index] if index >= 0 else None


def _state_after(states: Sequence[CurveState], timestamp_ns: int) -> CurveState | None:
    if not states:
        return None
    keys = [state.timestamp_ns for state in states]
    index = bisect.bisect_left(keys, timestamp_ns)
    return states[index] if index < len(states) else states[-1]


def _first_e4_buy_after(
    rows: Sequence[Mapping[str, Any]],
    decision_ns: int,
    confirmation_ns: int,
) -> Mapping[str, Any] | None:
    for row in rows:
        if str(row.get("trader") or "") != base.E4_WALLET:
            continue
        if str(row.get("kind") or "").upper() not in base.BUY_KINDS:
            continue
        timestamp_ns = base.integer(row.get("received_ns"))
        if decision_ns <= timestamp_ns <= decision_ns + confirmation_ns:
            return row
    return None


def _e4_sells_after(
    rows: Sequence[Mapping[str, Any]],
    source_buy_ns: int,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in rows
        if str(row.get("trader") or "") == base.E4_WALLET
        and str(row.get("kind") or "").upper() in base.SELL_KINDS
        and base.integer(row.get("received_ns")) >= source_buy_ns
    ]


def simulate_position(
    prediction: Prediction,
    rows: Sequence[Mapping[str, Any]],
    e4_position: Mapping[str, Any] | None,
    *,
    liquid_sol: float,
    latency_ms: float,
    entry_fraction_default: float = 0.0185,
    reserve_sol: float,
    fee_bps: int,
    max_output_shortfall_bps: int,
    confirmation_ms: float,
) -> tuple[PositionResult | None, str]:
    states = reserve_states(rows)
    if not states:
        return None, "missing_curve_states"
    decision_state = _state_before(states, prediction.decision_ns) or _state_after(
        states, prediction.decision_ns
    )
    fill_ns = prediction.decision_ns + int(max(0.0, latency_ms) * 1_000_000)
    fill_state = _state_before(states, fill_ns) or _state_after(states, fill_ns)
    if decision_state is None or fill_state is None:
        return None, "missing_decision_or_fill_state"

    fraction = prediction.requested_fraction or entry_fraction_default
    total_budget = min(
        max(0.0, liquid_sol - reserve_sol),
        max(0.0, liquid_sol * min(1.0, fraction)),
    )
    if total_budget <= 0:
        return None, "insufficient_balance"
    fee_rate = max(0, fee_bps) / 10_000.0
    curve_input = affordable_curve_input(total_budget, fee_rate, prediction.score)
    if curve_input <= 0:
        return None, "zero_curve_input"

    expected_tokens = base.buy_tokens(curve_input, decision_state)
    fill_tokens = base.buy_tokens(curve_input, fill_state)
    deterioration = quote_deterioration_bps(expected_tokens, fill_tokens)
    if fill_tokens <= 0:
        return None, "zero_fill_tokens"
    if deterioration > max_output_shortfall_bps + 1e-9:
        return None, "strict_output_guard_rejected"

    entry_cost = curve_input * (1.0 + fee_rate) + route_bid(curve_input, prediction.score)
    if entry_cost > max(0.0, liquid_sol - reserve_sol) + 1e-9:
        return None, "entry_cost_unfundable"

    ordered = sorted((dict(row) for row in rows), key=base.event_sort_key)
    confirmation_ns = int(max(0.0, confirmation_ms) * 1_000_000)
    source_buy = _first_e4_buy_after(ordered, prediction.decision_ns, confirmation_ns)
    source_buy_ns = base.integer(source_buy.get("received_ns")) if source_buy else None
    source_lead_ms = (
        (source_buy_ns - prediction.decision_ns) / 1_000_000.0
        if source_buy_ns is not None
        else None
    )
    confirmed = source_buy is not None
    remaining = fill_tokens
    proceeds = 0.0
    sell_count = 0
    exit_ns = fill_ns
    exit_reason = "unconfirmed_timeout"

    if confirmed and source_buy is not None:
        source_tokens = max(1e-12, base.finite(source_buy.get("token_amount")))
        sells = _e4_sells_after(ordered, source_buy_ns or 0)
        cumulative = 0.0
        for index, sell in enumerate(sells):
            sold_source = max(0.0, base.finite(sell.get("token_amount")))
            fraction_to_sell = min(max(0.0, 1.0 - cumulative), sold_source / source_tokens)
            if fraction_to_sell <= 0:
                continue
            cumulative = min(1.0, cumulative + fraction_to_sell)
            amount = min(remaining, fill_tokens * fraction_to_sell)
            due_ns = base.integer(sell.get("received_ns")) + int(
                max(0.0, latency_ms) * 1_000_000
            )
            state = _state_before(states, due_ns) or _state_after(states, due_ns)
            if state is None or amount <= 0:
                continue
            gross = base.sell_sol(amount, state)
            proceeds += max(
                0.0,
                gross * (1.0 - fee_rate)
                - route_bid(curve_input * fraction_to_sell, 1.0, index == len(sells) - 1),
            )
            remaining = max(0.0, remaining - amount)
            sell_count += 1
            exit_ns = max(exit_ns, due_ns)
        exit_reason = "e4_multileg_mirror"
    else:
        due_ns = prediction.decision_ns + confirmation_ns
        state = _state_before(states, due_ns) or _state_after(states, due_ns)
        if state is None:
            return None, "missing_timeout_exit_state"
        gross = base.sell_sol(remaining, state)
        proceeds = max(
            0.0,
            gross * (1.0 - fee_rate) - route_bid(curve_input, 1.0, True),
        )
        remaining = 0.0
        sell_count = 1
        exit_ns = due_ns

    if remaining > fill_tokens * 1e-6:
        state = states[-1]
        gross = base.sell_sol(remaining, state)
        proceeds += max(
            0.0,
            gross * (1.0 - fee_rate) - route_bid(curve_input, 1.0, True),
        )
        remaining = 0.0
        sell_count += 1
        exit_ns = max(exit_ns, state.timestamp_ns)
        exit_reason += "+tail_flatten"

    return (
        PositionResult(
            mint=prediction.mint,
            decision_ns=prediction.decision_ns,
            fill_ns=fill_ns,
            exit_ns=exit_ns,
            mode=prediction.mode,
            confirmed_by_e4=confirmed,
            source_buy_ns=source_buy_ns,
            source_lead_ms=source_lead_ms,
            requested_fraction=fraction,
            entry_curve_sol=curve_input,
            entry_cost_sol=entry_cost,
            expected_tokens_at_decision=expected_tokens,
            quoted_tokens_at_fill=fill_tokens,
            output_deterioration_bps=deterioration,
            token_amount=fill_tokens,
            proceeds_sol=proceeds,
            pnl_sol=proceeds - entry_cost,
            sell_count=sell_count,
            exit_reason=exit_reason,
            e4_pnl_sol=base.finite(e4_position.get("pnl_sol")) if e4_position else None,
        ),
        "filled",
    )


def _profit_factor(rows: Sequence[PositionResult]) -> float | None:
    gains = sum(row.pnl_sol for row in rows if row.pnl_sol > 0)
    losses = sum(row.pnl_sol for row in rows if row.pnl_sol < 0)
    if losses < 0:
        return gains / abs(losses)
    return 999.0 if gains > 0 else None


def replay_latency(
    predictions: Sequence[Prediction],
    grouped: Mapping[str, Sequence[Mapping[str, Any]]],
    e4_positions: Mapping[str, Mapping[str, Any]],
    *,
    starting_balance_sol: float,
    latency_ms: float,
    entry_fraction_default: float,
    reserve_sol: float,
    fee_bps: int,
    max_output_shortfall_bps: int,
    confirmation_ms: float,
    max_concurrent: int,
) -> dict[str, Any]:
    liquid = max(0.0, starting_balance_sol)
    active: list[PositionResult] = []
    completed: list[PositionResult] = []
    rejected: dict[str, int] = {}
    skipped = 0

    def settle(timestamp_ns: int) -> None:
        nonlocal liquid, active
        remaining: list[PositionResult] = []
        for position in active:
            if position.exit_ns <= timestamp_ns:
                liquid += position.proceeds_sol
                completed.append(position)
            else:
                remaining.append(position)
        active = remaining

    for prediction in sorted(predictions, key=lambda item: (item.decision_ns, item.mint)):
        settle(prediction.decision_ns)
        if len(active) >= max(1, max_concurrent):
            skipped += 1
            continue
        position, status = simulate_position(
            prediction,
            grouped.get(prediction.mint, ()),
            e4_positions.get(prediction.mint),
            liquid_sol=liquid,
            latency_ms=latency_ms,
            entry_fraction_default=entry_fraction_default,
            reserve_sol=reserve_sol,
            fee_bps=fee_bps,
            max_output_shortfall_bps=max_output_shortfall_bps,
            confirmation_ms=confirmation_ms,
        )
        if position is None:
            rejected[status] = rejected.get(status, 0) + 1
            continue
        liquid -= position.entry_cost_sol
        active.append(position)
    settle(2**63 - 1)
    wins = sum(position.pnl_sol > 0 for position in completed)
    return {
        "latency_ms": latency_ms,
        "starting_balance_sol": starting_balance_sol,
        "ending_balance_sol": liquid,
        "return_fraction": (
            (liquid - starting_balance_sol) / starting_balance_sol
            if starting_balance_sol > 0
            else None
        ),
        "all_predictions": {
            "closed": len(completed),
            "wins": wins,
            "losses": len(completed) - wins,
            "win_rate": wins / len(completed) if completed else None,
            "net_pnl_sol": sum(position.pnl_sol for position in completed),
            "profit_factor": _profit_factor(completed),
            "positions": [position.as_dict() for position in completed],
        },
        "rejected": dict(sorted(rejected.items())),
        "skipped_for_concurrency": skipped,
    }


def install() -> None:
    # Surface only the legacy names. The authoritative current functions remain
    # intact, and current ``load_predictions(path, runs, mode)`` is preserved by
    # the dispatcher above.
    base.CurveState = CurveState  # type: ignore[attr-defined]
    base.Prediction = Prediction  # type: ignore[attr-defined]
    base.PositionResult = PositionResult  # type: ignore[attr-defined]
    base.event_order = event_order  # type: ignore[attr-defined]
    base.reserve_states = reserve_states  # type: ignore[attr-defined]
    base.route_bid = route_bid  # type: ignore[attr-defined]
    base.affordable_curve_input = affordable_curve_input  # type: ignore[attr-defined]
    base.quote_deterioration_bps = quote_deterioration_bps  # type: ignore[attr-defined]
    base.parse_latencies = parse_latencies  # type: ignore[attr-defined]
    base.load_events = load_events  # type: ignore[attr-defined]
    base.load_predictions = load_predictions  # type: ignore[assignment]
    base.same_window_e4_positions = same_window_e4_positions  # type: ignore[attr-defined]
    base.simulate_position = simulate_position  # type: ignore[attr-defined]
    base.replay_latency = replay_latency  # type: ignore[attr-defined]


install()

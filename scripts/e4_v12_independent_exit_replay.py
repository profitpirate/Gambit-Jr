#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts import e4_v12_true_latency_replay as base
from scripts import e4_v12_true_latency_replay_v3  # noqa: F401 - dynamic live fees


def finite(value: Any, default: float = 0.0) -> float:
    return base.finite(value, default)


def integer(value: Any, default: int = 0) -> int:
    return base.integer(value, default)


@dataclass(frozen=True)
class ExitPolicy:
    stop_loss_fraction: float
    first_take_profit_fraction: float
    first_partial_fraction: float
    final_take_profit_fraction: float
    trailing_drawdown_fraction: float
    post_partial_floor_fraction: float
    maximum_hold_ms: float
    minimum_hold_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExitPolicy":
        return cls(**{key: float(value[key]) for key in cls.__dataclass_fields__})


@dataclass
class IndependentResult:
    mint: str
    decision_ns: int
    fill_ns: int
    exit_ns: int
    mode: str
    entry_curve_sol: float
    entry_cost_sol: float
    expected_tokens_at_decision: float
    quoted_tokens_at_fill: float
    output_deterioration_bps: float
    token_amount: float
    proceeds_sol: float
    pnl_sol: float
    return_fraction: float
    sell_count: int
    first_partial_done: bool
    exit_reason: str
    maximum_mark_return_fraction: float
    minimum_mark_return_fraction: float
    hold_ms: float

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def exit_cost(curve_input: float, gross: float, fee_rate: float, *, urgent: bool) -> float:
    return max(
        0.0,
        gross * (1.0 - fee_rate)
        - base.route_bid(curve_input, 1.0, urgent=urgent),
    )


def simulate_independent(
    prediction: base.Prediction,
    rows: Sequence[Mapping[str, Any]],
    *,
    liquid_sol: float,
    latency_ms: float,
    reserve_sol: float,
    fee_bps: int,
    max_output_shortfall_bps: int,
    policy: ExitPolicy,
) -> tuple[IndependentResult | None, str]:
    states = base.reserve_states(rows)
    if not states:
        return None, "missing_curve_states"
    decision_state = base.state_at_or_before(states, prediction.decision_ns) or base.state_at_or_after(states, prediction.decision_ns)
    fill_ns = prediction.decision_ns + int(max(0.0, latency_ms) * 1_000_000)
    fill_state = base.state_at_or_before(states, fill_ns) or base.state_at_or_after(states, fill_ns)
    if decision_state is None or fill_state is None:
        return None, "missing_decision_or_fill_state"

    total_budget = min(
        max(0.0, liquid_sol - reserve_sol),
        max(0.0, liquid_sol * min(1.0, prediction.requested_fraction or 0.0185)),
    )
    if total_budget <= 0:
        return None, "insufficient_balance"
    dynamic_fee_bps = e4_v12_true_latency_replay_v3.fee_bps_from_rows(rows, prediction.decision_ns, fee_bps)
    fee_rate = max(0, dynamic_fee_bps) / 10_000.0
    curve_input = base.affordable_curve_input(total_budget, fee_rate, prediction.score)
    if curve_input <= 0:
        return None, "zero_curve_input"

    expected_tokens = base.buy_tokens(curve_input, decision_state)
    fill_tokens = base.buy_tokens(curve_input, fill_state)
    deterioration = base.quote_deterioration_bps(expected_tokens, fill_tokens)
    if fill_tokens <= 0:
        return None, "zero_fill_tokens"
    if deterioration > max_output_shortfall_bps + 1e-9:
        return None, "strict_output_guard_rejected"
    entry_cost = curve_input * (1.0 + fee_rate) + base.route_bid(curve_input, prediction.score)
    if entry_cost > max(0.0, liquid_sol - reserve_sol) + 1e-9:
        return None, "entry_cost_unfundable"

    subsequent = [state for state in states if state.timestamp_ns >= fill_ns]
    if not subsequent:
        subsequent = [fill_state]
    deadline_ns = fill_ns + int(max(0.0, policy.maximum_hold_ms) * 1_000_000)
    minimum_exit_ns = fill_ns + int(max(0.0, policy.minimum_hold_ms) * 1_000_000)
    remaining = fill_tokens
    realised = 0.0
    sell_count = 0
    partial_done = False
    best_mark_return = -float("inf")
    worst_mark_return = float("inf")
    exit_ns = fill_ns
    exit_reason = "tail_flatten"

    def total_mark(state: base.CurveState) -> tuple[float, float]:
        gross = base.sell_sol(remaining, state)
        net = exit_cost(curve_input, gross, fee_rate, urgent=True)
        total = realised + net
        return total, (total - entry_cost) / max(entry_cost, 1e-12)

    for state in subsequent:
        timestamp_ns = state.timestamp_ns
        total_if_flat, mark_return = total_mark(state)
        best_mark_return = max(best_mark_return, mark_return)
        worst_mark_return = min(worst_mark_return, mark_return)

        if timestamp_ns < minimum_exit_ns:
            continue

        if not partial_done:
            if mark_return <= -abs(policy.stop_loss_fraction):
                realised = total_if_flat
                remaining = 0.0
                sell_count += 1
                exit_ns = timestamp_ns
                exit_reason = "hard_stop"
                break
            if mark_return >= policy.first_take_profit_fraction:
                fraction = min(0.95, max(0.01, policy.first_partial_fraction))
                tokens_to_sell = min(remaining, fill_tokens * fraction)
                gross = base.sell_sol(tokens_to_sell, state)
                realised += exit_cost(curve_input * fraction, gross, fee_rate, urgent=False)
                remaining = max(0.0, remaining - tokens_to_sell)
                partial_done = True
                sell_count += 1
                exit_ns = timestamp_ns
                _, after_partial_mark = total_mark(state)
                best_mark_return = max(best_mark_return, after_partial_mark)
                if remaining <= fill_tokens * 1e-6:
                    exit_reason = "first_take_profit_full"
                    break
        else:
            total_if_flat, mark_return = total_mark(state)
            best_mark_return = max(best_mark_return, mark_return)
            worst_mark_return = min(worst_mark_return, mark_return)
            if mark_return >= policy.final_take_profit_fraction:
                realised = total_if_flat
                remaining = 0.0
                sell_count += 1
                exit_ns = timestamp_ns
                exit_reason = "final_take_profit"
                break
            if mark_return <= policy.post_partial_floor_fraction:
                realised = total_if_flat
                remaining = 0.0
                sell_count += 1
                exit_ns = timestamp_ns
                exit_reason = "post_partial_floor"
                break
            if best_mark_return - mark_return >= policy.trailing_drawdown_fraction:
                realised = total_if_flat
                remaining = 0.0
                sell_count += 1
                exit_ns = timestamp_ns
                exit_reason = "trailing_stop"
                break

        if timestamp_ns >= deadline_ns:
            total_if_flat, _ = total_mark(state)
            realised = total_if_flat
            remaining = 0.0
            sell_count += 1
            exit_ns = timestamp_ns
            exit_reason = "maximum_hold"
            break

    if remaining > fill_tokens * 1e-6:
        eligible = [state for state in states if state.timestamp_ns <= deadline_ns]
        final_state = eligible[-1] if eligible else states[-1]
        gross = base.sell_sol(remaining, final_state)
        realised += exit_cost(curve_input, gross, fee_rate, urgent=True)
        remaining = 0.0
        sell_count += 1
        exit_ns = max(exit_ns, final_state.timestamp_ns)
        exit_reason = "tail_flatten" if exit_reason == "tail_flatten" else f"{exit_reason}_tail"

    pnl = realised - entry_cost
    return (
        IndependentResult(
            mint=prediction.mint,
            decision_ns=prediction.decision_ns,
            fill_ns=fill_ns,
            exit_ns=exit_ns,
            mode=prediction.mode,
            entry_curve_sol=curve_input,
            entry_cost_sol=entry_cost,
            expected_tokens_at_decision=expected_tokens,
            quoted_tokens_at_fill=fill_tokens,
            output_deterioration_bps=deterioration,
            token_amount=fill_tokens,
            proceeds_sol=realised,
            pnl_sol=pnl,
            return_fraction=pnl / max(entry_cost, 1e-12),
            sell_count=sell_count,
            first_partial_done=partial_done,
            exit_reason=exit_reason,
            maximum_mark_return_fraction=best_mark_return if math.isfinite(best_mark_return) else 0.0,
            minimum_mark_return_fraction=worst_mark_return if math.isfinite(worst_mark_return) else 0.0,
            hold_ms=max(0.0, (exit_ns - fill_ns) / 1_000_000.0),
        ),
        "filled",
    )


def metrics(rows: Sequence[IndependentResult | Mapping[str, Any]]) -> dict[str, Any]:
    pnl_values = [
        row.pnl_sol if isinstance(row, IndependentResult) else finite(row.get("pnl_sol"))
        for row in rows
    ]
    wins = sum(value > 0 for value in pnl_values)
    gains = sum(value for value in pnl_values if value > 0)
    losses = sum(value for value in pnl_values if value < 0)
    return {
        "trades": len(pnl_values),
        "wins": wins,
        "losses": len(pnl_values) - wins,
        "win_rate": wins / len(pnl_values) if pnl_values else 0.0,
        "net_pnl_sol": sum(pnl_values),
        "profit_factor": gains / abs(losses) if losses < 0 else (999.0 if gains > 0 else 0.0),
    }


def replay(
    predictions: Sequence[base.Prediction],
    grouped: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    starting_balance_sol: float,
    latency_ms: float,
    reserve_sol: float,
    fee_bps: int,
    max_output_shortfall_bps: int,
    policy: ExitPolicy,
    max_concurrent: int,
) -> dict[str, Any]:
    liquid = max(0.0, starting_balance_sol)
    active: list[IndependentResult] = []
    completed: list[IndependentResult] = []
    rejected: dict[str, int] = {}
    skipped_concurrency = 0

    def settle(timestamp_ns: int) -> None:
        nonlocal liquid, active
        still_open = []
        for position in active:
            if position.exit_ns <= timestamp_ns:
                liquid += position.proceeds_sol
                completed.append(position)
            else:
                still_open.append(position)
        active = still_open

    for prediction in sorted(predictions, key=lambda item: (item.decision_ns, item.mint)):
        settle(prediction.decision_ns)
        if len(active) >= max(1, max_concurrent):
            skipped_concurrency += 1
            continue
        position, status = simulate_independent(
            prediction,
            grouped.get(prediction.mint, ()),
            liquid_sol=liquid,
            latency_ms=latency_ms,
            reserve_sol=reserve_sol,
            fee_bps=fee_bps,
            max_output_shortfall_bps=max_output_shortfall_bps,
            policy=policy,
        )
        if position is None:
            rejected[status] = rejected.get(status, 0) + 1
            continue
        liquid -= position.entry_cost_sol
        active.append(position)
    settle(2**63 - 1)
    return {
        "latency_ms": latency_ms,
        "starting_balance_sol": starting_balance_sol,
        "ending_balance_sol": liquid,
        "return_fraction": (liquid - starting_balance_sol) / starting_balance_sol if starting_balance_sol > 0 else None,
        "metrics": metrics(completed),
        "positions": [position.as_dict() for position in completed],
        "rejected": dict(sorted(rejected.items())),
        "skipped_for_concurrency": skipped_concurrency,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay V12 entries with an independent partial/trailing exit")
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--latencies", default="0,1,2,5,10")
    parser.add_argument("--starting-balance-sol", type=float, default=3.0)
    parser.add_argument("--reserve-sol", type=float, default=0.03)
    parser.add_argument("--fee-bps", type=int, default=125)
    parser.add_argument("--max-output-shortfall-bps", type=int, default=800)
    parser.add_argument("--max-concurrent", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    grouped = base.load_events(args.events)
    predictions = base.load_predictions(args.predictions)
    policy = ExitPolicy.from_dict(json.loads(args.policy.read_text(encoding="utf-8")))
    results = {
        str(int(latency) if float(latency).is_integer() else latency): replay(
            predictions,
            grouped,
            starting_balance_sol=args.starting_balance_sol,
            latency_ms=latency,
            reserve_sol=args.reserve_sol,
            fee_bps=args.fee_bps,
            max_output_shortfall_bps=args.max_output_shortfall_bps,
            policy=policy,
            max_concurrent=args.max_concurrent,
        )
        for latency in base.parse_latencies(args.latencies)
    }
    payload = {
        "version": "e4-v12-independent-exit-replay-v1",
        "policy": policy.as_dict(),
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({latency: block["metrics"] for latency, block in results.items()}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

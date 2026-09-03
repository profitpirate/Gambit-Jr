#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bisect
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

E4_WALLET = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"
LAMPORTS = 1_000_000_000
TOKEN_SCALE = 1_000_000


def finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def profit_factor(rows: Sequence[Mapping[str, Any]]) -> float | None:
    wins = sum(finite(row.get("pnl_sol")) for row in rows if finite(row.get("pnl_sol")) > 0)
    losses = sum(finite(row.get("pnl_sol")) for row in rows if finite(row.get("pnl_sol")) < 0)
    return wins / abs(losses) if losses < 0 else None


def metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    wins = sum(finite(row.get("pnl_sol")) > 0 for row in rows)
    return {
        "closed": len(rows),
        "wins": wins,
        "losses": len(rows) - wins,
        "win_rate": wins / len(rows) if rows else None,
        "net_pnl_sol": sum(finite(row.get("pnl_sol")) for row in rows),
        "profit_factor": profit_factor(rows),
    }


def same_window_e4(batch: Mapping[str, Any]) -> list[dict[str, Any]]:
    positions = list((batch.get("actual_e4_fresh_sample") or {}).get("positions") or [])
    cohort = list((batch.get("capture") or {}).get("cohort") or [])
    starts = [int(row.get("received_ns") or 0) for row in cohort if int(row.get("received_ns") or 0) > 0]
    if not starts:
        return []
    start = min(starts) / 1e9 - 5.0
    tail = finite((batch.get("capture") or {}).get("tail_seconds_observed"))
    end = max(starts) / 1e9 + max(5.0, tail + 5.0)
    return [dict(row) for row in positions if start <= finite(row.get("entry_time")) <= end]


def load_events(path: Path, mints: set[str]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            mint = str(row.get("mint") or "")
            if mint in mints:
                grouped[mint].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda row: (int(row.get("received_ns") or 0), int(row.get("event_index") or 0)))
    return grouped


def e4_events(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in rows
        if str(row.get("trader") or "") == E4_WALLET
        and str(row.get("kind") or "").upper() in {"BUY", "SELL"}
    ]


def reserve_states(rows: Sequence[Mapping[str, Any]]) -> list[tuple[int, float, float, float]]:
    output = []
    for row in rows:
        raw = row.get("raw") or {}
        virtual_sol = finite(raw.get("virtual_sol_reserves"))
        virtual_tokens = finite(raw.get("virtual_token_reserves"))
        real_tokens = finite(raw.get("real_token_reserves"))
        if virtual_sol <= 0 or virtual_tokens <= 0:
            continue
        output.append(
            (
                int(row.get("received_ns") or 0),
                virtual_sol / LAMPORTS,
                virtual_tokens / TOKEN_SCALE,
                real_tokens / TOKEN_SCALE if real_tokens > 0 else float("inf"),
            )
        )
    return output


def state_at_or_before(states: Sequence[tuple[int, float, float, float]], timestamp_ns: int):
    if not states:
        return None
    times = [row[0] for row in states]
    index = bisect.bisect_right(times, timestamp_ns) - 1
    return states[index] if index >= 0 else None


def fee_bid(amount: float, score: float, *, urgent: bool = False) -> float:
    total = min(
        max(0.0, amount) * max(0.0, min(score, 1.0)) * (0.03 if urgent else 0.015),
        0.15,
    )
    priority = min(0.05, total * 0.60)
    tip = min(0.05, max(0.0, total - priority))
    return priority + tip


def affordable_curve_input(source_curve_sol: float, wallet_balance_sol: float | None, fee_rate: float) -> float:
    source = max(0.0, source_curve_sol)
    if wallet_balance_sol is None:
        return source
    available = max(0.0, wallet_balance_sol - 0.03)
    lo, hi = 0.0, min(source, available)
    for _ in range(64):
        mid = (lo + hi) / 2.0
        total_cost = mid * (1.0 + fee_rate) + fee_bid(mid, 0.96)
        if total_cost <= available:
            lo = mid
        else:
            hi = mid
    return lo


def buy_against_curve(curve_input_sol: float, virtual_sol: float, virtual_tokens: float, real_tokens: float) -> float:
    if curve_input_sol <= 0 or virtual_sol <= 0 or virtual_tokens <= 0:
        return 0.0
    tokens = curve_input_sol * virtual_tokens / (virtual_sol + curve_input_sol)
    return min(max(0.0, tokens), max(0.0, real_tokens))


def sell_against_curve(tokens: float, virtual_sol: float, virtual_tokens: float) -> float:
    if tokens <= 0 or virtual_sol <= 0 or virtual_tokens <= 0:
        return 0.0
    return tokens * virtual_sol / (virtual_tokens + tokens)


def e4_pre_buy_state(buy: Mapping[str, Any]) -> tuple[float, float, float] | None:
    raw = buy.get("raw") or {}
    post_sol = finite(raw.get("virtual_sol_reserves")) / LAMPORTS
    post_tokens = finite(raw.get("virtual_token_reserves")) / TOKEN_SCALE
    real_tokens_post = finite(raw.get("real_token_reserves")) / TOKEN_SCALE
    curve_sol = finite(buy.get("sol_amount"))
    token_out = finite(buy.get("token_amount"))
    if min(post_sol, post_tokens, curve_sol, token_out) <= 0:
        return None
    # Pump TradeEvent reserve fields are post-trade. Reverse the observed E4
    # buy to recover the immediately-pre-E4 curve state.
    return post_sol - curve_sol, post_tokens + token_out, real_tokens_post + token_out


def simulate_copy(
    *,
    mint: str,
    rows: Sequence[Mapping[str, Any]],
    latency_ms: float,
    fee_bps: int,
    wallet_balance_sol: float | None,
    ordering: str,
) -> dict[str, Any] | None:
    source = e4_events(rows)
    if not source or str(source[0].get("kind") or "").upper() != "BUY":
        return None
    buy = source[0]
    sells = [row for row in source[1:] if str(row.get("kind") or "").upper() == "SELL"]
    if not sells:
        return None

    fee_rate = max(0, fee_bps) / 10_000.0
    source_curve_sol = finite(buy.get("sol_amount"))
    curve_input = affordable_curve_input(source_curve_sol, wallet_balance_sol, fee_rate)
    states = reserve_states(rows)
    due = int(buy.get("received_ns") or 0) + int(latency_ms * 1_000_000)

    if ordering == "before_e4":
        pre = e4_pre_buy_state(buy)
        if pre is None:
            return None
        virtual_sol, virtual_tokens, real_tokens = pre
    elif ordering == "after_e4":
        state = state_at_or_before(states, due)
        if state is None:
            return None
        _, virtual_sol, virtual_tokens, real_tokens = state
    else:
        raise ValueError(ordering)

    received_tokens = buy_against_curve(curve_input, virtual_sol, virtual_tokens, real_tokens)
    if received_tokens <= 0:
        return None
    buy_route_cost = fee_bid(curve_input, 0.96)
    entry_cost = curve_input * (1.0 + fee_rate) + buy_route_cost

    e4_entry_tokens = finite(buy.get("token_amount"))
    remaining = received_tokens
    proceeds = 0.0
    sell_legs: list[dict[str, Any]] = []
    for index, sell in enumerate(sells):
        original_fraction = min(
            1.0,
            max(0.0, finite(sell.get("token_amount")) / max(e4_entry_tokens, 1e-12)),
        )
        tokens_to_sell = min(remaining, received_tokens * original_fraction)
        sell_due = int(sell.get("received_ns") or 0) + int(latency_ms * 1_000_000)
        state = state_at_or_before(states, sell_due)
        if state is None or tokens_to_sell <= 0:
            continue
        _, sell_virtual_sol, sell_virtual_tokens, _ = state
        gross = sell_against_curve(tokens_to_sell, sell_virtual_sol, sell_virtual_tokens)
        urgent = index == len(sells) - 1
        route_cost = fee_bid(curve_input * original_fraction, 1.0, urgent=urgent)
        net = max(0.0, gross * (1.0 - fee_rate) - route_cost)
        proceeds += net
        remaining = max(0.0, remaining - tokens_to_sell)
        sell_legs.append(
            {
                "original_fraction": original_fraction,
                "tokens": tokens_to_sell,
                "gross_sol": gross,
                "net_sol": net,
                "due_ns": sell_due,
            }
        )

    return {
        "mint": mint,
        "ordering": ordering,
        "latency_ms": latency_ms,
        "source_curve_input_sol": source_curve_sol,
        "copy_curve_input_sol": curve_input,
        "exact_source_size": abs(curve_input - source_curve_sol) <= max(1e-9, source_curve_sol * 1e-9),
        "entry_cost_sol": entry_cost,
        "tokens": received_tokens,
        "sell_count": len(sell_legs),
        "first_partial_fraction": sell_legs[0]["original_fraction"] if sell_legs else None,
        "proceeds_sol": proceeds,
        "pnl_sol": proceeds - entry_cost,
        "sell_legs": sell_legs,
    }


def same_exchange_reference(
    *,
    position: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    fee_bps: int,
    wallet_balance_sol: float | None,
) -> dict[str, Any] | None:
    """Apply V12 costs to E4's exact observed curve exchange ratios.

    This is not executable copy performance. It is a harness sanity reference:
    if both entry and exit exchange economics are held at E4's actual values,
    the replay must stop using post-trade spot as an imaginary average fill.
    """
    source = e4_events(rows)
    if not source or len(source) < 2:
        return None
    buy = source[0]
    sells = source[1:]
    fee_rate = max(0, fee_bps) / 10_000.0
    source_curve_sol = finite(buy.get("sol_amount"))
    curve_input = affordable_curve_input(source_curve_sol, wallet_balance_sol, fee_rate)
    scale = curve_input / source_curve_sol if source_curve_sol > 0 else 0.0
    entry_cost = curve_input * (1.0 + fee_rate) + fee_bid(curve_input, 0.96)
    e4_entry_tokens = finite(buy.get("token_amount"))
    proceeds = 0.0
    legs = []
    for index, sell in enumerate(sells):
        fraction = finite(sell.get("token_amount")) / max(e4_entry_tokens, 1e-12)
        gross = finite(sell.get("sol_amount")) * scale
        route_cost = fee_bid(curve_input * fraction, 1.0, urgent=index == len(sells) - 1)
        net = max(0.0, gross * (1.0 - fee_rate) - route_cost)
        proceeds += net
        legs.append({"original_fraction": fraction, "gross_sol": gross, "net_sol": net})
    return {
        "mint": str(position.get("mint") or ""),
        "pnl_sol": proceeds - entry_cost,
        "entry_cost_sol": entry_cost,
        "proceeds_sol": proceeds,
        "sell_count": len(legs),
        "first_partial_fraction": legs[0]["original_fraction"] if legs else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Reserve-aware V12/E4 curve-fidelity replay")
    parser.add_argument("--batch", required=True)
    parser.add_argument("--events", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--fee-bps", type=int, default=125)
    parser.add_argument("--wallet-balance-sol", type=float, default=1.2)
    args = parser.parse_args()

    batch = json.loads(Path(args.batch).read_text(encoding="utf-8"))
    e4_positions = same_window_e4(batch)
    mints = {str(row.get("mint") or "") for row in e4_positions}
    grouped = load_events(Path(args.events), mints)

    actual_e4 = metrics(e4_positions)
    actual_e4["positions"] = e4_positions

    exact_exchange = [
        row
        for position in e4_positions
        if (row := same_exchange_reference(
            position=position,
            rows=grouped[str(position.get("mint") or "")],
            fee_bps=args.fee_bps,
            wallet_balance_sol=args.wallet_balance_sol,
        )) is not None
    ]

    reactive: dict[str, Any] = {}
    for latency in (0.0, 10.0, 36.0):
        rows = [
            row
            for position in e4_positions
            if (row := simulate_copy(
                mint=str(position.get("mint") or ""),
                rows=grouped[str(position.get("mint") or "")],
                latency_ms=latency,
                fee_bps=args.fee_bps,
                wallet_balance_sol=args.wallet_balance_sol,
                ordering="after_e4",
            )) is not None
        ]
        reactive[str(int(latency))] = {**metrics(rows), "positions": rows}

    before_rows = [
        row
        for position in e4_positions
        if (row := simulate_copy(
            mint=str(position.get("mint") or ""),
            rows=grouped[str(position.get("mint") or "")],
            latency_ms=0.0,
            fee_bps=args.fee_bps,
            wallet_balance_sol=args.wallet_balance_sol,
            ordering="before_e4",
        )) is not None
    ]

    result = {
        "version": "e4-v12-curve-fidelity-v1",
        "methodology": {
            "event_reserves": "latest observed Pump post-trade reserves at-or-before intended copy time",
            "buy_model": "constant-product curve input against observed reserves",
            "sell_model": "constant-product token sell against observed reserves",
            "pump_fee_bps": args.fee_bps,
            "v12_route_cost_model": "current fee_bid caps",
            "wallet_balance_sol": args.wallet_balance_sol,
            "important": "after_e4 is executable-ordering reference; before_e4 is diagnostic only and cannot be achieved by waiting to observe E4's BUY",
        },
        "actual_e4": actual_e4,
        "same_e4_exchange_with_v12_costs": {**metrics(exact_exchange), "positions": exact_exchange},
        "reactive_after_e4": reactive,
        "before_e4_ordering_reference": {**metrics(before_rows), "positions": before_rows},
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")

    print(json.dumps({
        "actual_e4": actual_e4,
        "same_e4_exchange_with_v12_costs": metrics(exact_exchange),
        "reactive_after_e4": {key: metrics(value["positions"]) for key, value in reactive.items()},
        "before_e4_ordering_reference": metrics(before_rows),
    }, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

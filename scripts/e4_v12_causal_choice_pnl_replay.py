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


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def profit_factor(rows: Sequence[Mapping[str, Any]]) -> float | None:
    positive = sum(finite(row.get("pnl_sol")) for row in rows if finite(row.get("pnl_sol")) > 0)
    negative = sum(finite(row.get("pnl_sol")) for row in rows if finite(row.get("pnl_sol")) < 0)
    return positive / abs(negative) if negative < 0 else None


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


def fee_bid(amount_sol: float, score: float, urgent: bool = False) -> float:
    total = min(max(0.0, amount_sol) * max(0.0, min(1.0, score)) * (0.03 if urgent else 0.015), 0.15)
    priority = min(0.05, total * 0.60)
    tip = min(0.05, max(0.0, total - priority))
    return priority + tip


def load_events(paths: Sequence[Path]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                mint = str(row.get("mint") or "")
                if mint:
                    grouped[mint].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda row: (integer(row.get("received_ns")), integer(row.get("event_index"))))
    return grouped


def reserve_states(rows: Sequence[Mapping[str, Any]]) -> list[tuple[int, float, float, float]]:
    output: list[tuple[int, float, float, float]] = []
    for row in rows:
        raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
        virtual_sol = finite(raw.get("virtual_sol_reserves"))
        virtual_tokens = finite(raw.get("virtual_token_reserves"))
        real_tokens = finite(raw.get("real_token_reserves"))
        if virtual_sol <= 0 or virtual_tokens <= 0:
            continue
        output.append(
            (
                integer(row.get("received_ns")),
                virtual_sol / LAMPORTS,
                virtual_tokens / TOKEN_SCALE,
                real_tokens / TOKEN_SCALE if real_tokens > 0 else float("inf"),
            )
        )
    return output


def state_at(states: Sequence[tuple[int, float, float, float]], timestamp_ns: int):
    if not states:
        return None
    timestamps = [row[0] for row in states]
    index = bisect.bisect_right(timestamps, timestamp_ns) - 1
    if index >= 0:
        return states[index]
    return states[0]


def buy_tokens(curve_sol: float, virtual_sol: float, virtual_tokens: float, real_tokens: float) -> float:
    if curve_sol <= 0 or virtual_sol <= 0 or virtual_tokens <= 0:
        return 0.0
    output = curve_sol * virtual_tokens / (virtual_sol + curve_sol)
    return min(max(0.0, output), max(0.0, real_tokens))


def sell_sol(tokens: float, virtual_sol: float, virtual_tokens: float) -> float:
    if tokens <= 0 or virtual_sol <= 0 or virtual_tokens <= 0:
        return 0.0
    return tokens * virtual_sol / (virtual_tokens + tokens)


def e4_events(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in rows
        if str(row.get("trader") or "") == E4_WALLET
        and str(row.get("kind") or "").upper() in {"BUY", "SELL", "PUMPSWAP_BUY", "PUMPSWAP_SELL"}
    ]


def first_after(rows: Sequence[Mapping[str, Any]], timestamp_ns: int) -> Mapping[str, Any] | None:
    for row in rows:
        if integer(row.get("received_ns")) >= timestamp_ns:
            return row
    return rows[-1] if rows else None


def simulate_one(
    prediction: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    *,
    entry_fraction: float,
    starting_balance_sol: float,
    latency_ms: float,
    pump_fee_bps: int,
    confirmation_ms: float,
) -> dict[str, Any] | None:
    mint = str(prediction.get("mint") or "")
    decision_ns = integer(prediction.get("decision_ns"))
    fill_ns = decision_ns + int(latency_ms * 1e6)
    states = reserve_states(rows)
    entry_state = state_at(states, fill_ns)
    if entry_state is None:
        return None
    _, virtual_sol, virtual_tokens, real_tokens = entry_state
    available = max(0.0, starting_balance_sol - 0.03)
    requested_curve_sol = min(available, max(0.0, starting_balance_sol * entry_fraction))
    fee_rate = max(0, pump_fee_bps) / 10_000.0
    # Ensure curve input + protocol fee + route cost remains fundable.
    lo, hi = 0.0, requested_curve_sol
    for _ in range(64):
        middle = (lo + hi) / 2.0
        cost = middle * (1.0 + fee_rate) + fee_bid(middle, 0.96)
        if cost <= available:
            lo = middle
        else:
            hi = middle
    curve_input = lo
    tokens = buy_tokens(curve_input, virtual_sol, virtual_tokens, real_tokens)
    if tokens <= 0:
        return None
    entry_cost = curve_input * (1.0 + fee_rate) + fee_bid(curve_input, 0.96)

    source = e4_events(rows)
    source_buy = next((row for row in source if str(row.get("kind") or "").upper() in {"BUY", "PUMPSWAP_BUY"}), None)
    source_sells = [row for row in source if str(row.get("kind") or "").upper() in {"SELL", "PUMPSWAP_SELL"}]
    confirmed = source_buy is not None
    remaining = tokens
    proceeds = 0.0
    legs: list[dict[str, Any]] = []

    if confirmed and source_sells:
        source_tokens = max(1e-12, finite(source_buy.get("token_amount")))
        cumulative_fraction = 0.0
        for index, sell in enumerate(source_sells):
            fraction = min(1.0 - cumulative_fraction, max(0.0, finite(sell.get("token_amount")) / source_tokens))
            if fraction <= 0:
                continue
            cumulative_fraction += fraction
            amount = min(remaining, tokens * fraction)
            due = integer(sell.get("received_ns")) + int(latency_ms * 1e6)
            state = state_at(states, due)
            if state is None:
                continue
            _, x, y, _ = state
            gross = sell_sol(amount, x, y)
            urgent = index == len(source_sells) - 1
            net = max(0.0, gross * (1.0 - fee_rate) - fee_bid(curve_input * fraction, 1.0, urgent))
            proceeds += net
            remaining = max(0.0, remaining - amount)
            legs.append({"kind": "E4_MIRROR", "fraction_of_original": fraction, "gross_sol": gross, "net_sol": net, "due_ns": due})
        if remaining > tokens * 1e-6:
            last_ns = integer(rows[-1].get("received_ns")) if rows else fill_ns
            state = state_at(states, last_ns)
            if state is not None:
                _, x, y, _ = state
                gross = sell_sol(remaining, x, y)
                net = max(0.0, gross * (1.0 - fee_rate) - fee_bid(curve_input, 1.0, True))
                proceeds += net
                legs.append({"kind": "TAIL_FLATTEN", "fraction_of_original": remaining / tokens, "gross_sol": gross, "net_sol": net, "due_ns": last_ns})
                remaining = 0.0
    else:
        # No successful E4 confirmation: flatten at the production confirmation
        # timeout rather than pretending an unavailable source exit exists.
        due = decision_ns + int(confirmation_ms * 1e6)
        state = state_at(states, due)
        if state is None:
            return None
        _, x, y, _ = state
        gross = sell_sol(remaining, x, y)
        net = max(0.0, gross * (1.0 - fee_rate) - fee_bid(curve_input, 1.0, True))
        proceeds += net
        legs.append({"kind": "UNCONFIRMED_TIMEOUT", "fraction_of_original": 1.0, "gross_sol": gross, "net_sol": net, "due_ns": due})
        remaining = 0.0

    return {
        "mint": mint,
        "label": prediction.get("label"),
        "mode": prediction.get("mode"),
        "decision_ns": decision_ns,
        "fill_ns": fill_ns,
        "lead_ms": prediction.get("lead_ms"),
        "confirmed_by_e4": confirmed,
        "entry_curve_sol": curve_input,
        "entry_cost_sol": entry_cost,
        "tokens": tokens,
        "proceeds_sol": proceeds,
        "pnl_sol": proceeds - entry_cost,
        "sell_count": len(legs),
        "legs": legs,
        "e4_pnl_sol": prediction.get("e4_pnl_sol"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Reserve-aware WR replay for causal pre-impact V12 entries")
    parser.add_argument("--validation", type=Path, required=True)
    parser.add_argument("--events", action="append", default=[], type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--latency-ms", type=float, default=36.0)
    parser.add_argument("--entry-fraction", type=float, default=0.0185)
    parser.add_argument("--starting-balance-sol", type=float, default=1.2)
    parser.add_argument("--pump-fee-bps", type=int, default=125)
    parser.add_argument("--confirmation-ms", type=float, default=1500.0)
    args = parser.parse_args()

    validation = json.loads(args.validation.read_text(encoding="utf-8"))
    grouped = load_events(args.events)
    positions = []
    for prediction in validation.get("predictions") or []:
        mint = str(prediction.get("mint") or "")
        trade = simulate_one(
            prediction,
            grouped.get(mint, ()),
            entry_fraction=args.entry_fraction,
            starting_balance_sol=args.starting_balance_sol,
            latency_ms=args.latency_ms,
            pump_fee_bps=args.pump_fee_bps,
            confirmation_ms=args.confirmation_ms,
        )
        if trade is not None:
            positions.append(trade)

    confirmed = [row for row in positions if row["confirmed_by_e4"]]
    unconfirmed = [row for row in positions if not row["confirmed_by_e4"]]
    result = {
        "version": "e4-v12-causal-choice-pnl-replay-v1",
        "methodology": {
            "entry": "constant-product buy against latest captured Pump reserves at model decision + latency",
            "confirmed_exit": "mirror E4's observed cumulative sell fractions at source sell + latency",
            "unconfirmed_exit": "flatten at causal confirmation timeout",
            "pump_fee_bps": args.pump_fee_bps,
            "latency_ms": args.latency_ms,
            "entry_fraction": args.entry_fraction,
            "starting_balance_sol": args.starting_balance_sol,
            "confirmation_ms": args.confirmation_ms,
        },
        "all_predictions": {**metrics(positions), "positions": positions},
        "e4_confirmed_predictions": {**metrics(confirmed), "positions": confirmed},
        "unconfirmed_predictions": {**metrics(unconfirmed), "positions": unconfirmed},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "all": metrics(positions),
        "confirmed": metrics(confirmed),
        "unconfirmed": metrics(unconfirmed),
    }, indent=2, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

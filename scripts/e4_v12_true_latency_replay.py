#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bisect
import json
import math
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

E4_WALLET = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"
BUY_KINDS = {"BUY", "PUMPSWAP_BUY"}
SELL_KINDS = {"SELL", "PUMPSWAP_SELL"}
LAMPORTS = 1_000_000_000.0
TOKEN_SCALE = 1_000_000.0


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


def tx_index(row: Mapping[str, Any]) -> int:
    raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
    for key in ("transaction_index", "transactionIndex", "tx_index", "txIndex"):
        value = row.get(key)
        if value is None:
            value = raw.get(key)
        if value is not None:
            return integer(value, -1)
    return -1


def event_sort_key(row: Mapping[str, Any]) -> tuple[int, int, int, int, str]:
    """Arrival time is authoritative; transaction/event indexes break ties only."""
    index = tx_index(row)
    return (
        integer(row.get("received_ns")),
        integer(row.get("slot"), -1),
        index if index >= 0 else 1_000_000,
        integer(row.get("event_index")),
        str(row.get("signature") or ""),
    )


def normalize_sol_reserve(value: Any) -> float:
    number = finite(value)
    if number <= 0:
        return 0.0
    return number / LAMPORTS if number >= 1_000_000 else number


def normalize_token_reserve(value: Any) -> float:
    number = finite(value)
    if number <= 0:
        return 0.0
    return number / TOKEN_SCALE if number >= 10_000_000_000 else number


@dataclass(frozen=True)
class ReserveState:
    received_ns: int
    sequence: int
    virtual_sol: float
    virtual_tokens: float
    real_tokens: float
    price_sol: float
    fdv_usd: float


@dataclass
class RunData:
    run_id: str
    batch: dict[str, Any]
    events_by_mint: dict[str, list[dict[str, Any]]]
    reserves_by_mint: dict[str, list[ReserveState]]
    e4_positions: dict[str, dict[str, Any]]


@dataclass
class SimulatedTrade:
    run_id: str
    mint: str
    latency_ms: float
    decision_ns: int
    fill_ns: int
    exit_ns: int
    confirmed_by_e4: bool
    source_buy_ns: int | None
    source_lead_ms: float | None
    output_shortfall_bps: float
    allowed_shortfall_bps: int
    entry_budget_sol: float
    curve_input_sol: float
    entry_cost_sol: float
    expected_tokens: float
    received_tokens: float
    proceeds_sol: float
    pnl_sol: float
    first_partial_fraction: float | None
    sell_count: int
    exit_mode: str
    score: float
    family: str


@dataclass
class Rejection:
    run_id: str
    mint: str
    latency_ms: float
    decision_ns: int
    reason: str
    output_shortfall_bps: float | None
    fee_sol: float


def parse_pair(value: str) -> tuple[str, Path, Path]:
    run_id = ""
    body = value
    if "=" in value and value.index("=") < value.index(":"):
        run_id, body = value.split("=", 1)
    batch, events = body.split(":", 1)
    batch_path = Path(batch)
    if not run_id:
        run_id = batch_path.parent.parent.name if batch_path.parent.name == "artifacts" else batch_path.stem
    return run_id, batch_path, Path(events)


def same_window_e4(batch: Mapping[str, Any]) -> list[dict[str, Any]]:
    positions = list((batch.get("actual_e4_fresh_sample") or {}).get("positions") or [])
    cohort = list((batch.get("capture") or {}).get("cohort") or [])
    starts = [integer(row.get("received_ns")) for row in cohort if integer(row.get("received_ns")) > 0]
    if not starts:
        return [dict(row) for row in positions]
    start = min(starts) / 1e9 - 5.0
    tail = finite((batch.get("capture") or {}).get("tail_seconds_observed"))
    end = max(starts) / 1e9 + max(5.0, tail + 5.0)
    return [
        dict(row)
        for row in positions
        if start <= finite(row.get("entry_time")) <= end
    ]


def reserve_from_row(row: Mapping[str, Any], sequence: int) -> ReserveState | None:
    raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
    virtual_sol = normalize_sol_reserve(
        raw.get("virtual_sol_reserves") or row.get("virtual_sol_reserves")
    )
    virtual_tokens = normalize_token_reserve(
        raw.get("virtual_token_reserves") or row.get("virtual_token_reserves")
    )
    real_tokens = normalize_token_reserve(
        raw.get("real_token_reserves") or row.get("real_token_reserves")
    )
    if virtual_sol <= 0 or virtual_tokens <= 0:
        return None
    return ReserveState(
        received_ns=integer(row.get("received_ns")),
        sequence=sequence,
        virtual_sol=virtual_sol,
        virtual_tokens=virtual_tokens,
        real_tokens=real_tokens if real_tokens > 0 else float("inf"),
        price_sol=finite(row.get("price_sol")),
        fdv_usd=finite(row.get("fdv_usd")),
    )


def load_run(run_id: str, batch_path: Path, events_path: Path) -> RunData:
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    grouped: dict[str, list[dict[str, Any]]] = {}
    with events_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            mint = str(row.get("mint") or "")
            if mint:
                grouped.setdefault(mint, []).append(row)
    reserves: dict[str, list[ReserveState]] = {}
    for mint, rows in grouped.items():
        rows.sort(key=event_sort_key)
        for sequence, row in enumerate(rows):
            row["__sequence"] = sequence
        states = [
            state
            for sequence, row in enumerate(rows)
            if (state := reserve_from_row(row, sequence)) is not None
        ]
        reserves[mint] = states
    e4_positions = {
        str(row.get("mint") or ""): dict(row)
        for row in same_window_e4(batch)
        if str(row.get("mint") or "")
    }
    return RunData(run_id, batch, grouped, reserves, e4_positions)


def state_at_or_before(
    states: Sequence[ReserveState],
    timestamp_ns: int,
    sequence: int | None = None,
) -> ReserveState | None:
    if not states:
        return None
    keys = [(item.received_ns, item.sequence) for item in states]
    target = (timestamp_ns, sequence if sequence is not None else 2**62)
    index = bisect.bisect_right(keys, target) - 1
    return states[index] if index >= 0 else None


def state_at_or_after(states: Sequence[ReserveState], timestamp_ns: int) -> ReserveState | None:
    if not states:
        return None
    keys = [item.received_ns for item in states]
    index = bisect.bisect_left(keys, timestamp_ns)
    if index < len(states):
        return states[index]
    return states[-1]


def buy_tokens(curve_input_sol: float, state: ReserveState) -> float:
    if curve_input_sol <= 0 or state.virtual_sol <= 0 or state.virtual_tokens <= 0:
        return 0.0
    tokens = curve_input_sol * state.virtual_tokens / (state.virtual_sol + curve_input_sol)
    return min(max(0.0, tokens), max(0.0, state.real_tokens))


def sell_sol(tokens: float, state: ReserveState) -> float:
    if tokens <= 0 or state.virtual_sol <= 0 or state.virtual_tokens <= 0:
        return 0.0
    return tokens * state.virtual_sol / (state.virtual_tokens + tokens)


def fee_bid(amount_sol: float, score: float, urgent: bool = False) -> float:
    total = min(
        max(0.0, amount_sol)
        * max(0.0, min(1.0, score))
        * (0.03 if urgent else 0.015),
        0.15,
    )
    priority = min(0.05, total * 0.60)
    tip = min(0.05, max(0.0, total - priority))
    return priority + tip


def priority_failure_cost(amount_sol: float, score: float) -> float:
    # A failed Solana instruction can still consume base/priority fees, while
    # the atomic Jito-tip transfer does not execute.  Charge the priority share.
    return max(0.000005, min(0.05, fee_bid(amount_sol, score) * 0.60))


def curve_input_for_budget(total_budget: float, fee_rate: float, score: float) -> float:
    if total_budget <= 0:
        return 0.0
    lo, hi = 0.0, total_budget
    for _ in range(72):
        middle = (lo + hi) / 2.0
        total = middle * (1.0 + fee_rate) + fee_bid(middle, score)
        if total <= total_budget:
            lo = middle
        else:
            hi = middle
    return lo


def source_events(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    buy = None
    sells: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("trader") or "") != E4_WALLET:
            continue
        kind = str(row.get("kind") or "").upper()
        if kind in BUY_KINDS and buy is None:
            buy = dict(row)
        elif kind in SELL_KINDS and buy is not None:
            sells.append(dict(row))
    return buy, sells


def resolve_decision_sequence(prediction: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> int:
    explicit = prediction.get("decision_sequence")
    if explicit is not None:
        return integer(explicit, -1)
    event_id = prediction.get("decision_event_id")
    signature = str(prediction.get("decision_signature") or "")
    event_index = prediction.get("decision_event_index")
    for row in rows:
        if event_id is not None and str(row.get("event_id")) == str(event_id):
            return integer(row.get("__sequence"), -1)
        if signature and str(row.get("signature") or "") == signature:
            if event_index is None or integer(row.get("event_index")) == integer(event_index):
                return integer(row.get("__sequence"), -1)
    decision_ns = integer(prediction.get("decision_ns"))
    eligible = [
        integer(row.get("__sequence"), -1)
        for row in rows
        if integer(row.get("received_ns")) <= decision_ns
    ]
    return max(eligible, default=-1)


def prediction_precedes_source(
    prediction: Mapping[str, Any],
    source_buy: Mapping[str, Any] | None,
    decision_sequence: int,
) -> bool:
    if source_buy is None:
        return False
    decision_key = (integer(prediction.get("decision_ns")), decision_sequence)
    source_key = (
        integer(source_buy.get("received_ns")),
        integer(source_buy.get("__sequence"), -1),
    )
    return decision_key < source_key


def simulate_one(
    run: RunData,
    prediction: Mapping[str, Any],
    *,
    liquid_sol: float,
    latency_ms: float,
    output_shortfall_bps: int,
    entry_fraction: float,
    maximum_position_sol: float,
    reserve_sol: float,
    pump_fee_bps: int,
    confirmation_ms: float,
    unconfirmed_timeout_ms: float,
) -> tuple[SimulatedTrade | None, Rejection | None]:
    mint = str(prediction.get("mint") or "")
    rows = run.events_by_mint.get(mint, [])
    states = run.reserves_by_mint.get(mint, [])
    decision_ns = integer(prediction.get("decision_ns"))
    decision_sequence = resolve_decision_sequence(prediction, rows)
    if not rows or not states or decision_ns <= 0:
        return None, Rejection(run.run_id, mint, latency_ms, decision_ns, "missing captured event/reserve state", None, 0.0)

    decision_state = state_at_or_before(states, decision_ns, decision_sequence)
    if decision_state is None:
        return None, Rejection(run.run_id, mint, latency_ms, decision_ns, "no causal curve quote at decision", None, 0.0)

    fill_ns = decision_ns + int(latency_ms * 1_000_000)
    fill_sequence = decision_sequence if latency_ms <= 0 else None
    fill_state = state_at_or_before(states, fill_ns, fill_sequence)
    if fill_state is None:
        return None, Rejection(run.run_id, mint, latency_ms, decision_ns, "no curve quote at fill", None, 0.0)

    score = min(1.0, max(0.0, finite(prediction.get("score"), 0.96)))
    requested_fraction = min(1.0, max(0.0, finite(prediction.get("entry_fraction"), entry_fraction)))
    available = max(0.0, liquid_sol - reserve_sol)
    budget = min(available, liquid_sol * requested_fraction, maximum_position_sol)
    fee_rate = max(0, pump_fee_bps) / 10_000.0
    curve_input = curve_input_for_budget(budget, fee_rate, score)
    if curve_input <= 0:
        return None, Rejection(run.run_id, mint, latency_ms, decision_ns, "insufficient deployable balance", None, 0.0)

    expected_tokens = buy_tokens(curve_input, decision_state)
    received_tokens = buy_tokens(curve_input, fill_state)
    if expected_tokens <= 0 or received_tokens <= 0:
        return None, Rejection(run.run_id, mint, latency_ms, decision_ns, "zero-token quote", None, 0.0)
    shortfall = max(0.0, (1.0 - received_tokens / expected_tokens) * 10_000.0)
    if shortfall > output_shortfall_bps + 1e-9:
        failed_fee = min(liquid_sol, priority_failure_cost(curve_input, score))
        return None, Rejection(
            run.run_id,
            mint,
            latency_ms,
            decision_ns,
            "BuyExactSolIn output floor rejected deteriorated fill",
            shortfall,
            failed_fee,
        )

    entry_route = fee_bid(curve_input, score)
    entry_cost = curve_input * (1.0 + fee_rate) + entry_route
    if entry_cost > available + 1e-9:
        return None, Rejection(run.run_id, mint, latency_ms, decision_ns, "entry cost exceeds deployable balance", shortfall, 0.0)

    source_buy, source_sells = source_events(rows)
    pre_source = prediction_precedes_source(prediction, source_buy, decision_sequence)
    source_buy_ns = integer(source_buy.get("received_ns")) if source_buy else None
    source_lead_ms = (
        (source_buy_ns - decision_ns) / 1e6 if source_buy_ns is not None else None
    )
    confirmation_limit_ns = decision_ns + int(confirmation_ms * 1_000_000)
    confirmed = bool(
        source_buy is not None
        and pre_source
        and integer(source_buy.get("received_ns")) <= confirmation_limit_ns
    )

    remaining = received_tokens
    proceeds = 0.0
    first_partial: float | None = None
    sell_count = 0
    exit_ns = fill_ns
    exit_mode = "UNCONFIRMED_TIMEOUT"

    if confirmed and source_sells:
        source_tokens = max(1e-12, finite(source_buy.get("token_amount")))
        cumulative = 0.0
        for index, sell in enumerate(source_sells):
            original_fraction = min(
                1.0 - cumulative,
                max(0.0, finite(sell.get("token_amount")) / source_tokens),
            )
            if original_fraction <= 0:
                continue
            cumulative += original_fraction
            if first_partial is None:
                first_partial = original_fraction
            amount = min(remaining, received_tokens * original_fraction)
            due = integer(sell.get("received_ns")) + int(latency_ms * 1_000_000)
            sell_sequence = integer(sell.get("__sequence"), -1) if latency_ms <= 0 else None
            state = state_at_or_before(states, due, sell_sequence)
            if state is None:
                state = state_at_or_after(states, due)
            if state is None or amount <= 0:
                continue
            gross = sell_sol(amount, state)
            urgent = index == len(source_sells) - 1
            net = max(
                0.0,
                gross * (1.0 - fee_rate)
                - fee_bid(curve_input * original_fraction, 1.0, urgent),
            )
            proceeds += net
            remaining = max(0.0, remaining - amount)
            sell_count += 1
            exit_ns = max(exit_ns, due)
        exit_mode = "E4_MULTI_LEG_MIRROR"
    else:
        due = decision_ns + int(unconfirmed_timeout_ms * 1_000_000)
        state = state_at_or_before(states, due)
        if state is None:
            state = state_at_or_after(states, due)
        if state is None:
            return None, Rejection(run.run_id, mint, latency_ms, decision_ns, "no exit curve state", shortfall, 0.0)
        gross = sell_sol(remaining, state)
        proceeds = max(0.0, gross * (1.0 - fee_rate) - fee_bid(curve_input, 1.0, True))
        remaining = 0.0
        sell_count = 1
        exit_ns = due

    if remaining > received_tokens * 1e-6:
        tail_ns = max(integer(row.get("received_ns")) for row in rows)
        state = state_at_or_before(states, tail_ns)
        if state is not None:
            gross = sell_sol(remaining, state)
            proceeds += max(0.0, gross * (1.0 - fee_rate) - fee_bid(curve_input, 1.0, True))
            remaining = 0.0
            sell_count += 1
            exit_ns = max(exit_ns, tail_ns)
            exit_mode += "+TAIL_FLATTEN"

    trade = SimulatedTrade(
        run_id=run.run_id,
        mint=mint,
        latency_ms=latency_ms,
        decision_ns=decision_ns,
        fill_ns=fill_ns,
        exit_ns=exit_ns,
        confirmed_by_e4=confirmed,
        source_buy_ns=source_buy_ns,
        source_lead_ms=source_lead_ms,
        output_shortfall_bps=shortfall,
        allowed_shortfall_bps=output_shortfall_bps,
        entry_budget_sol=budget,
        curve_input_sol=curve_input,
        entry_cost_sol=entry_cost,
        expected_tokens=expected_tokens,
        received_tokens=received_tokens,
        proceeds_sol=proceeds,
        pnl_sol=proceeds - entry_cost,
        first_partial_fraction=first_partial,
        sell_count=sell_count,
        exit_mode=exit_mode,
        score=score,
        family=str(prediction.get("family") or prediction.get("mode") or "golden_thesis"),
    )
    return trade, None


def profit_factor(rows: Sequence[SimulatedTrade]) -> float | None:
    positive = sum(row.pnl_sol for row in rows if row.pnl_sol > 0)
    negative = sum(row.pnl_sol for row in rows if row.pnl_sol < 0)
    if negative < 0:
        return positive / abs(negative)
    return 999.0 if positive > 0 else None


def metric_summary(values: Iterable[float]) -> dict[str, float | int | None]:
    rows = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not rows:
        return {"count": 0, "median": None, "p95": None, "max": None}
    p95_index = min(len(rows) - 1, max(0, math.ceil(0.95 * len(rows)) - 1))
    return {
        "count": len(rows),
        "median": statistics.median(rows),
        "p95": rows[p95_index],
        "max": rows[-1],
    }


def portfolio(
    runs: Mapping[str, RunData],
    predictions: Sequence[Mapping[str, Any]],
    *,
    latency_ms: float,
    output_shortfall_bps: int,
    starting_balance_sol: float,
    entry_fraction: float,
    maximum_position_sol: float,
    reserve_sol: float,
    pump_fee_bps: int,
    confirmation_ms: float,
    unconfirmed_timeout_ms: float,
    max_concurrency: int,
) -> dict[str, Any]:
    liquid = starting_balance_sol
    active: list[SimulatedTrade] = []
    closed: list[SimulatedTrade] = []
    rejections: list[Rejection] = []
    touched: set[tuple[str, str]] = set()
    skipped_concurrency = 0
    skipped_reentry = 0
    equity_peak = liquid
    maximum_drawdown = 0.0

    def settle(until_ns: int) -> None:
        nonlocal liquid, active, equity_peak, maximum_drawdown
        remaining: list[SimulatedTrade] = []
        for trade in active:
            if trade.exit_ns <= until_ns:
                liquid += trade.proceeds_sol
                closed.append(trade)
                equity_peak = max(equity_peak, liquid)
                maximum_drawdown = max(maximum_drawdown, equity_peak - liquid)
            else:
                remaining.append(trade)
        active = remaining

    ordered = sorted(
        predictions,
        key=lambda row: (
            integer(row.get("decision_ns")),
            str(row.get("run_id") or ""),
            str(row.get("mint") or ""),
        ),
    )
    for prediction in ordered:
        run_id = str(prediction.get("run_id") or "")
        run = runs.get(run_id)
        if run is None:
            continue
        decision_ns = integer(prediction.get("decision_ns"))
        settle(decision_ns)
        key = (run_id, str(prediction.get("mint") or ""))
        if key in touched:
            skipped_reentry += 1
            continue
        touched.add(key)
        if len(active) >= max_concurrency:
            skipped_concurrency += 1
            continue
        trade, rejection = simulate_one(
            run,
            prediction,
            liquid_sol=liquid,
            latency_ms=latency_ms,
            output_shortfall_bps=output_shortfall_bps,
            entry_fraction=entry_fraction,
            maximum_position_sol=maximum_position_sol,
            reserve_sol=reserve_sol,
            pump_fee_bps=pump_fee_bps,
            confirmation_ms=confirmation_ms,
            unconfirmed_timeout_ms=unconfirmed_timeout_ms,
        )
        if rejection is not None:
            if rejection.fee_sol > 0:
                liquid = max(0.0, liquid - rejection.fee_sol)
                equity_peak = max(equity_peak, liquid)
                maximum_drawdown = max(maximum_drawdown, equity_peak - liquid)
            rejections.append(rejection)
            continue
        if trade is None or liquid - trade.entry_cost_sol < reserve_sol - 1e-9:
            continue
        liquid -= trade.entry_cost_sol
        active.append(trade)
    settle(2**63 - 1)

    wins = [row for row in closed if row.pnl_sol > 0]
    losses = [row for row in closed if row.pnl_sol <= 0]
    confirmed = [row for row in closed if row.confirmed_by_e4]
    unconfirmed = [row for row in closed if not row.confirmed_by_e4]
    return {
        "latency_ms": latency_ms,
        "output_shortfall_bps": output_shortfall_bps,
        "starting_balance_sol": starting_balance_sol,
        "ending_balance_sol": liquid,
        "net_pnl_sol": liquid - starting_balance_sol,
        "return_fraction": liquid / starting_balance_sol - 1.0 if starting_balance_sol > 0 else None,
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(closed) if closed else None,
        "profit_factor": profit_factor(closed),
        "confirmed_closed": len(confirmed),
        "confirmed_wins": sum(row.pnl_sol > 0 for row in confirmed),
        "confirmed_win_rate": (
            sum(row.pnl_sol > 0 for row in confirmed) / len(confirmed)
            if confirmed else None
        ),
        "unconfirmed_closed": len(unconfirmed),
        "rejected_output_guard": sum(
            row.reason.startswith("BuyExactSolIn") for row in rejections
        ),
        "rejection_fees_sol": sum(row.fee_sol for row in rejections),
        "skipped_concurrency": skipped_concurrency,
        "skipped_reentry": skipped_reentry,
        "max_drawdown_sol": maximum_drawdown,
        "lead_ms": metric_summary(
            row.source_lead_ms for row in closed if row.source_lead_ms is not None
        ),
        "output_shortfall_bps_observed": metric_summary(
            row.output_shortfall_bps for row in closed
        ),
        "positions": [asdict(row) for row in closed],
        "rejections": [asdict(row) for row in rejections],
    }


def load_predictions(path: Path | None, runs: Mapping[str, RunData], mode: str) -> list[dict[str, Any]]:
    if mode == "reactive-e4":
        output: list[dict[str, Any]] = []
        for run_id, run in runs.items():
            for mint, rows in run.events_by_mint.items():
                source_buy, _ = source_events(rows)
                if source_buy is None:
                    continue
                output.append(
                    {
                        "run_id": run_id,
                        "mint": mint,
                        "decision_ns": integer(source_buy.get("received_ns")),
                        "decision_sequence": integer(source_buy.get("__sequence"), -1),
                        "score": 0.96,
                        "family": "reactive_e4_copy",
                        "reactive_source": True,
                    }
                )
        return output
    if path is None:
        raise ValueError("--predictions is required unless --mode reactive-e4")
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("predictions") if isinstance(payload, Mapping) else payload
    if not isinstance(rows, list):
        raise ValueError("prediction payload must be a list or contain predictions[]")
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild V12 entry/exit economics independently at every latency"
    )
    parser.add_argument("--pair", action="append", default=[], help="[run_id=]batch.json:events.jsonl")
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--mode", choices=("predictions", "reactive-e4"), default="predictions")
    parser.add_argument("--latencies-ms", default="0,1,2,5,10")
    parser.add_argument("--output-shortfall-bps", default="200,400,600,800,1000")
    parser.add_argument("--starting-balance-sol", type=float, default=3.0)
    parser.add_argument("--entry-fraction", type=float, default=0.0185)
    parser.add_argument("--maximum-position-sol", type=float, default=0.30)
    parser.add_argument("--reserve-sol", type=float, default=0.03)
    parser.add_argument("--pump-fee-bps", type=int, default=125)
    parser.add_argument("--confirmation-ms", type=float, default=1_500.0)
    parser.add_argument("--unconfirmed-timeout-ms", type=float, default=1_500.0)
    parser.add_argument("--max-concurrency", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.pair:
        parser.error("at least one --pair is required")

    runs = {
        run_id: load_run(run_id, batch, events)
        for run_id, batch, events in (parse_pair(value) for value in args.pair)
    }
    predictions = load_predictions(args.predictions, runs, args.mode)
    latencies = [finite(value) for value in args.latencies_ms.split(",") if value.strip()]
    floors = [integer(value) for value in args.output_shortfall_bps.split(",") if value.strip()]
    matrix: dict[str, Any] = {}
    for floor in floors:
        matrix[str(floor)] = {}
        for latency in latencies:
            result = portfolio(
                runs,
                predictions,
                latency_ms=latency,
                output_shortfall_bps=floor,
                starting_balance_sol=args.starting_balance_sol,
                entry_fraction=args.entry_fraction,
                maximum_position_sol=args.maximum_position_sol,
                reserve_sol=args.reserve_sol,
                pump_fee_bps=args.pump_fee_bps,
                confirmation_ms=args.confirmation_ms,
                unconfirmed_timeout_ms=args.unconfirmed_timeout_ms,
                max_concurrency=args.max_concurrency,
            )
            matrix[str(floor)][str(latency)] = result
            print(json.dumps({
                "floor_bps": floor,
                "latency_ms": latency,
                "closed": result["closed"],
                "wins": result["wins"],
                "win_rate": result["win_rate"],
                "net_pnl_sol": result["net_pnl_sol"],
                "profit_factor": result["profit_factor"],
                "output_rejections": result["rejected_output_guard"],
            }, sort_keys=True), flush=True)

    payload = {
        "version": "e4-v12-true-latency-reserve-replay-v1",
        "methodology": {
            "chronology": "received_ns first; transaction/event indexes are tie-breakers only",
            "entry": "constant-product quote at decision and independently at decision+latency",
            "protection": "BuyExactSolIn minimum-token floor; deteriorated fills reject and pay conservative priority fee",
            "confirmed_exit": "mirror every observed E4 sell fraction at source sell+latency",
            "unconfirmed_exit": "flatten at configured confirmation timeout",
            "portfolio": "chronological 3 SOL bankroll, no re-entry, max two concurrent positions",
        },
        "run_ids": list(runs),
        "prediction_count": len(predictions),
        "matrix": matrix,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

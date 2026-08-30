#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import heapq
import importlib.util
import json
import math
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence


def load_base():
    path = Path(__file__).with_name("e4_live_market_stress.py")
    name = "e4_live_market_stress_concurrency_base"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


base = load_base()


@dataclass(slots=True)
class Leg:
    decided_ns: int
    filled_ns: int
    fraction: float
    price: float
    reason: str
    stale_quote_fill: bool = False


@dataclass(slots=True)
class PathResult:
    mint: str
    entry_ns: int
    exit_ns: int
    entry_price: float
    gross_multiple: float
    hold_ms: float
    first_partial_fraction: float | None
    sell_legs: list[Leg]
    invalid_fraction_count: int
    oversell_count: int
    escalation_count: int
    stale_quote_fills: int
    rapid_market: bool
    failure_exit: bool


@dataclass(slots=True)
class Active:
    result: PathResult
    entry_cost: float
    exit_value: float


def flow(events: Sequence[Any], index: int, window_ms: int) -> tuple[float, float]:
    now = events[index].received_ns
    cutoff = now - window_ms * 1_000_000
    buy = 0.0
    sell = 0.0
    for event in reversed(events[: index + 1]):
        if event.received_ns < cutoff:
            break
        if event.kind == base.core.EventKind.BUY.value:
            buy += max(0.0, event.sol_amount)
        elif event.kind == base.core.EventKind.SELL.value:
            sell += max(0.0, event.sol_amount)
    return buy, sell


def next_price_event(events: Sequence[Any], due_ns: int, last_index: int) -> tuple[int, Any | None]:
    for index in range(max(0, last_index), len(events)):
        event = events[index]
        if event.received_ns >= due_ns and event.price_sol and event.price_sol > 0:
            return index, event
    return len(events) - 1, None


def is_rapid(events: Sequence[Any], entry_index: int) -> bool:
    start = events[entry_index].received_ns
    end = start + 2_000_000_000
    observed = [
        event for event in events[entry_index:]
        if event.received_ns <= end and event.price_sol and event.price_sol > 0
    ]
    for previous, current in zip(observed, observed[1:]):
        delta_ms = (current.received_ns - previous.received_ns) / 1_000_000
        if delta_ms <= 250 and abs(current.price_sol / previous.price_sol - 1) >= 0.10:
            return True
    prices = [event.price_sol for event in observed]
    return bool(prices and max(prices) / min(prices) >= 1.5)


def simulate_path(
    events: Sequence[Any],
    *,
    execution_delay_ms: float,
    decision_delay_ms: float,
    failure_window_ms: int = 5_000,
    max_hold_ms: int = 60_000,
    quiet_ms: int = 750,
) -> PathResult | None:
    creates = [event for event in events if event.kind == base.core.EventKind.CREATE.value]
    if not creates:
        return None
    created = creates[0]
    entry_ready = created.received_ns + int(decision_delay_ms * 1_000_000)
    entry_due = entry_ready + int(execution_delay_ms * 1_000_000)
    entry_index = None
    for index, event in enumerate(events):
        if (
            event.received_ns >= entry_due
            and event.kind in {base.core.EventKind.BUY.value, base.core.EventKind.SELL.value}
            and event.price_sol
            and event.price_sol > 0
        ):
            entry_index = index
            break
    if entry_index is None:
        return None
    entry = events[entry_index]
    if not entry.fdv_usd or entry.fdv_usd > 10_000:
        return None

    entry_price = float(entry.price_sol)
    max_price = entry_price
    remaining = 1.0
    realized = 0.0
    first_partial: float | None = None
    legs: list[Leg] = []
    invalid = 0
    oversell = 0
    escalations = 0
    stale = 0
    pending: tuple[int, float, str, int, bool] | None = None
    last_price = entry_price
    last_trade_ns = entry.received_ns
    failure_exit = False
    rapid = is_rapid(events, entry_index)

    # Real market events plus independent 5s and 60s watchdog ticks.
    timeline: list[tuple[int, int, int | None]] = []
    for index in range(entry_index + 1, len(events)):
        heapq.heappush(timeline, (events[index].received_ns, 0, index))
    heapq.heappush(timeline, (entry.received_ns + failure_window_ms * 1_000_000, 1, None))
    heapq.heappush(timeline, (entry.received_ns + max_hold_ms * 1_000_000, 2, None))

    def decide(now_ns: int, event_index: int | None, timer: int) -> tuple[float, str] | None:
        nonlocal max_price, last_price, last_trade_ns, failure_exit
        age_ms = (now_ns - entry.received_ns) / 1_000_000
        if event_index is not None:
            event = events[event_index]
            if event.price_sol and event.price_sol > 0:
                last_price = float(event.price_sol)
                max_price = max(max_price, last_price)
            if event.kind in {base.core.EventKind.BUY.value, base.core.EventKind.SELL.value}:
                last_trade_ns = event.received_ns
        markout_bps = (last_price / entry_price - 1) * 10_000
        drawdown_bps = (1 - last_price / max_price) * 10_000 if max_price else 0
        if event_index is not None:
            buy250, sell250 = flow(events, event_index, 250)
            buy1s, sell1s = flow(events, event_index, 1000)
        else:
            buy250 = sell250 = buy1s = sell1s = 0.0
        ratio1s = buy1s / sell1s if sell1s > 0 else (math.inf if buy1s > 0 else 0.0)
        broken = (sell250 > buy250) or ratio1s < 0.85

        if timer == 2 or age_ms >= max_hold_ms:
            return 1.0, "E4 absolute hold horizon"
        if age_ms <= failure_window_ms:
            if markout_bps <= -350:
                failure_exit = True
                return 1.0, "E4 fast adverse-markout failure"
            if broken and markout_bps <= -100:
                failure_exit = True
                return 1.0, "E4 fast flow-break failure"
        if first_partial is None:
            if markout_bps >= 1500 and buy250 > sell250 and (buy250 / sell250 if sell250 else math.inf) >= 2:
                return 0.20, "E4 acceleration first partial"
            if markout_bps >= 900:
                return 0.30, "E4 normal first partial"
            if timer == 1 or age_ms >= failure_window_ms:
                quiet_for = (now_ns - last_trade_ns) / 1_000_000
                if quiet_for >= quiet_ms or broken:
                    failure_exit = True
                    return 1.0, "E4 confirmation window expired"
            return None
        if broken and drawdown_bps >= 350:
            return 1.0, "E4 runner flow broke"
        if drawdown_bps >= 1200:
            return 1.0, "E4 runner peak drawdown"
        if markout_bps >= 3000 and buy250 <= sell250:
            return 0.25, "E4 runner distribution"
        return None

    while timeline and remaining > 1e-12:
        now_ns, timer, event_index = heapq.heappop(timeline)
        if pending and now_ns >= pending[0]:
            due, fraction, reason, requested_index, escalated = pending
            fill_index, fill_event = next_price_event(events, due, requested_index)
            fill_price = float(fill_event.price_sol) if fill_event is not None else last_price
            stale_fill = fill_event is None
            if stale_fill:
                stale += 1
            if not 0 < fraction <= 1:
                invalid += 1
                fraction = min(1.0, max(0.0, fraction))
            sold = remaining * fraction
            if sold > remaining + 1e-12:
                oversell += 1
                sold = remaining
            realized += sold * (fill_price / entry_price)
            remaining = max(0.0, remaining - sold)
            legs.append(
                Leg(
                    decided_ns=due - int(execution_delay_ms * 1_000_000),
                    filled_ns=(fill_event.received_ns if fill_event is not None else due),
                    fraction=fraction,
                    price=fill_price,
                    reason=reason,
                    stale_quote_fill=stale_fill,
                )
            )
            if first_partial is None and fraction < 0.999:
                first_partial = fraction
            pending = None
            if escalated and remaining > 1e-12:
                escalations += 1
                pending = (
                    (fill_event.received_ns if fill_event is not None else due)
                    + int(execution_delay_ms * 1_000_000),
                    1.0,
                    "Escalated residual liquidation",
                    fill_index,
                    False,
                )
            if remaining <= 1e-12:
                break

        decision = decide(now_ns, event_index, timer)
        if decision is None:
            continue
        fraction, reason = decision
        if pending is not None:
            if fraction >= 0.999 and pending[1] < 0.999:
                due, existing_fraction, existing_reason, requested_index, _ = pending
                pending = (due, existing_fraction, existing_reason, requested_index, True)
            continue
        pending = (
            now_ns + int(execution_delay_ms * 1_000_000),
            fraction,
            reason,
            event_index if event_index is not None else entry_index,
            False,
        )
        # Ensure a pending order after the final market event still settles.
        heapq.heappush(timeline, (pending[0], 3, None))

    if remaining > 1e-12:
        # Defensive residual close at the last real curve price.
        realized += remaining * (last_price / entry_price)
        legs.append(
            Leg(
                decided_ns=entry.received_ns + max_hold_ms * 1_000_000,
                filled_ns=entry.received_ns + max_hold_ms * 1_000_000 + int(execution_delay_ms * 1_000_000),
                fraction=1.0,
                price=last_price,
                reason="Defensive residual close",
                stale_quote_fill=True,
            )
        )
        stale += 1
        remaining = 0.0

    exit_ns = max(leg.filled_ns for leg in legs)
    gross_multiple = realized
    return PathResult(
        mint=entry.mint,
        entry_ns=entry.received_ns,
        exit_ns=exit_ns,
        entry_price=entry_price,
        gross_multiple=gross_multiple,
        hold_ms=(exit_ns - entry.received_ns) / 1_000_000,
        first_partial_fraction=first_partial,
        sell_legs=legs,
        invalid_fraction_count=invalid,
        oversell_count=oversell,
        escalation_count=escalations,
        stale_quote_fills=stale,
        rapid_market=rapid,
        failure_exit=failure_exit,
    )


def portfolio(paths: Sequence[PathResult], starting_balance: float, reserve: float = 0.03) -> dict[str, Any]:
    liquid = starting_balance
    active: list[Active] = []
    completed: list[dict[str, Any]] = []
    peak = 0
    skipped = 0
    balance_floor = starting_balance

    def settle(before_ns: int) -> None:
        nonlocal liquid, active, balance_floor
        remaining = []
        for item in active:
            if item.result.exit_ns <= before_ns:
                liquid += item.exit_value
                completed.append(
                    {
                        "mint": item.result.mint,
                        "entry_sol": item.entry_cost,
                        "exit_sol": item.exit_value,
                        "pnl_sol": item.exit_value - item.entry_cost,
                        "hold_ms": item.result.hold_ms,
                        "rapid_market": item.result.rapid_market,
                        "first_partial_fraction": item.result.first_partial_fraction,
                    }
                )
            else:
                remaining.append(item)
        active = remaining
        balance_floor = min(balance_floor, liquid)

    for result in sorted(paths, key=lambda row: row.entry_ns):
        settle(result.entry_ns)
        if len(active) >= 2:
            skipped += 1
            continue
        deployable = max(0.0, liquid - reserve)
        size = min(deployable * 0.20, 5.0)
        if size < 0.01:
            skipped += 1
            continue
        liquid -= size
        # Cost model uses the actual wallet-relative position size rather than
        # embedding a fixed 0.05 SOL trade into every path. Four percent covers
        # conservative round-trip protocol/service/impact costs and 0.0008 SOL
        # represents fixed priority/tip/network overhead.
        exit_value = max(0.0, size * result.gross_multiple - size * 0.04 - 0.0008)
        active.append(Active(result, size, exit_value))
        peak = max(peak, len(active))
        balance_floor = min(balance_floor, liquid)
    settle(2**63 - 1)
    wins = [row for row in completed if row["pnl_sol"] > 0]
    rapid = [row for row in completed if row["rapid_market"]]
    return {
        "starting_balance_sol": starting_balance,
        "ending_balance_sol": liquid,
        "net_pnl_sol": liquid - starting_balance,
        "closed_positions": len(completed),
        "net_win_rate": len(wins) / len(completed) if completed else None,
        "max_concurrent_positions": peak,
        "skipped_for_capacity_or_size": skipped,
        "minimum_liquid_balance_sol": balance_floor,
        "reserve_breached": balance_floor < reserve - 1e-9,
        "rapid_positions": len(rapid),
        "rapid_net_pnl_sol": sum(row["pnl_sol"] for row in rapid),
        "positions": completed,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    events, diagnostics = await base.capture_native_pump(args.capture_seconds, base.DEFAULT_WS_RPCS)
    grouped: dict[str, list[Any]] = {}
    by_mint: dict[str, list[Any]] = {}
    for event in events:
        by_mint.setdefault(event.mint, []).append(event)
    for mint, values in by_mint.items():
        values.sort(key=lambda event: (event.received_ns, event.event_index))
        if any(event.kind == base.core.EventKind.CREATE.value for event in values):
            grouped[mint] = values

    scenarios = {}
    for delay in args.delays:
        paths = [
            result
            for values in grouped.values()
            if (result := simulate_path(
                values,
                execution_delay_ms=delay,
                decision_delay_ms=args.decision_delay_ms,
            ))
            is not None
        ]
        invalid = sum(result.invalid_fraction_count for result in paths)
        oversells = sum(result.oversell_count for result in paths)
        open_count = sum(not result.sell_legs for result in paths)
        scenarios[f"{delay:g}ms"] = {
            "real_token_paths": len(paths),
            "rapid_market_paths": sum(result.rapid_market for result in paths),
            "all_closed": open_count == 0,
            "invalid_fraction_count": invalid,
            "oversell_count": oversells,
            "escalations": sum(result.escalation_count for result in paths),
            "stale_quote_fills": sum(result.stale_quote_fills for result in paths),
            "hold_ms": base.metric_summary([result.hold_ms for result in paths]),
            "fully_exited_within_5s_fraction": (
                sum(result.hold_ms <= 5_000 for result in paths) / len(paths)
                if paths else None
            ),
            "fully_exited_within_10s_fraction": (
                sum(result.hold_ms <= 10_000 for result in paths) / len(paths)
                if paths else None
            ),
            "portfolio_0_3": portfolio(paths, 0.3),
            "portfolio_1_2": portfolio(paths, 1.2),
            "sample_paths": [
                {
                    **{key: value for key, value in asdict(result).items() if key != "sell_legs"},
                    "sell_legs": [asdict(leg) for leg in result.sell_legs],
                }
                for result in paths[:25]
            ],
        }

    async with base.RpcPool(base.DEFAULT_HTTP_RPCS, timeout=10) as rpc:
        actual = await base.fetch_e4_wallet_sample(rpc, args.wallet_signatures)
        rpc_errors = rpc.errors[-50:]

    primary = scenarios.get("50ms") or next(iter(scenarios.values()), {})
    portfolio_primary = primary.get("portfolio/1_2") or {}
    gates = {
        "real_launches": len(grouped) >= args.minimum_launches,
        "real_paths": (primary.get("real_token_paths") or 0) >= 25,
        "all_positions_closed": bool(primary.get("all_closed")),
        "no_invalid_fractions": primary.get("invalid_fraction_count") == 0,
        "no_oversells": primary.get("oversell_count") == 0,
        "two_position_limit": (portfolio_primary.get("max_concurrent_positions") or 0) <= 2,
        "reserve_preserved": not bool(portfolio_primary.get("reserve_breached")),
        "rapid_market_sample": (primary.get("rapid_market_paths") or 0) >= 5,
    }
    return {
        "generated_at": int(time.time()),
        "hypothesis_only": True,
        "real_market_data_only": True,
        "mainnet_transactions_sent": 0,
        "capture": {
            **diagnostics,
            "launches": len(grouped),
            "events": len(events),
            "trade_events": sum(
                event.kind in {base.core.EventKind.BUY.value, base.core.EventKind.SELL.value}
                for event in events
            ),
        },
        "scenarios": scenarios,
        "actual_e4_sample": actual,
        "rpc_errors": rpc_errors,
        "mechanical_gates": gates,
        "mechanically_passed": all(gates.values()),
        "live_certified": False,
        "live_certification_reason": (
            "Mechanical real-data replay is not a substitute for a funded authenticated "
            "route-landing canary or a calibrated E4 selected-vs-ignored entry model."
        ),
    }


def parse_delays(value: str) -> list[float]:
    return [float(item.strip()) for item in value.split(",") if item.strip()]


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--capture-seconds", type=float, default=600)
    value.add_argument("--minimum-launches", type=int, default=80)
    value.add_argument("--wallet-signatures", type=int, default=250)
    value.add_argument("--decision-delay-ms", type=float, default=5)
    value.add_argument("--delays", type=parse_delays, default=parse_delays("0,25,50,100,250,500,1000"))
    value.add_argument("--output", default="outputs/e4-v4-live-concurrency-stress.json")
    return value


def main() -> None:
    args = parser().parse_args()
    report = asyncio.run(run(args))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps({key: value for key, value in report.items() if key != "scenarios"}, indent=2, default=str))


if __name__ == "__main__":
    main()

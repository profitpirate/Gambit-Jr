#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from memecoin_bot import e4_hardening_v5 as v5

core = v5.core
base_exit = v5.e4_hardening_v4._previous_exit


@dataclass(slots=True)
class PathEvent:
    relative_ms: float
    event: core.Event


@dataclass(slots=True)
class TokenPath:
    mint: str
    entry_price: float
    events: list[PathEvent]
    rapid_1s: float
    rapid_5s: float


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lo = math.floor(position)
    hi = math.ceil(position)
    if lo == hi:
        return ordered[lo]
    weight = position - lo
    return ordered[lo] * (1 - weight) + ordered[hi] * weight


def summary(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "min": min(values) if values else None,
        "median": statistics.median(values) if values else None,
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": max(values) if values else None,
    }


def load_paths(path: Path) -> list[TokenPath]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                grouped[str(row["mint"])].append(row)

    result: list[TokenPath] = []
    for mint, rows in grouped.items():
        rows.sort(
            key=lambda row: (
                int(row["received_ns"]),
                int(row.get("slot") or 0),
                int(row.get("event_index") or 0),
            )
        )
        trades = [
            row
            for row in rows
            if row.get("kind") in {"BUY", "SELL"} and row.get("price_sol")
        ]
        if len(trades) < 3:
            continue
        entry = trades[0]
        entry_ns = int(entry["received_ns"])
        entry_price = float(entry["price_sol"])
        if entry_price <= 0:
            continue
        converted: list[PathEvent] = []
        for index, row in enumerate(trades):
            rel_ms = max(0.0, (int(row["received_ns"]) - entry_ns) / 1_000_000)
            if rel_ms > 61_000:
                break
            kind = core.EventKind.BUY if row["kind"] == "BUY" else core.EventKind.SELL
            converted.append(
                PathEvent(
                    rel_ms,
                    core.Event(
                        event_id=index + 1,
                        kind=kind,
                        mint=mint,
                        source_ns=int(row["received_ns"]),
                        received_ns=int(row["received_ns"]),
                        slot=int(row.get("slot") or 0),
                        signature=str(row.get("signature") or ""),
                        trader=row.get("trader"),
                        sol_amount=float(row.get("sol_amount") or 0.0),
                        token_amount=float(row.get("token_amount") or 0.0),
                        price_sol=float(row["price_sol"]),
                        fdv_usd=float(row.get("fdv_usd") or 0.0),
                    ),
                )
            )
        if len(converted) < 3:
            continue
        within_1s = [
            item.event.price_sol
            for item in converted
            if item.relative_ms <= 1_000 and item.event.price_sol
        ]
        within_5s = [
            item.event.price_sol
            for item in converted
            if item.relative_ms <= 5_000 and item.event.price_sol
        ]
        rapid_1s = max(
            (abs(price / entry_price - 1.0) for price in within_1s), default=0.0
        )
        rapid_5s = max(
            (abs(price / entry_price - 1.0) for price in within_5s), default=0.0
        )
        result.append(TokenPath(mint, entry_price, converted, rapid_1s, rapid_5s))
    return result


def fill_price(path: TokenPath, due_ms: float, last_price: float) -> float:
    for item in path.events:
        if item.relative_ms >= due_ms and item.event.price_sol:
            return float(item.event.price_sol)
    return last_price


def simulate_pair(
    left: TokenPath,
    right: TokenPath,
    delay_ms: float,
    settings: core.Settings,
) -> dict[str, Any]:
    states = {left.mint: core.TokenState(left.mint), right.mint: core.TokenState(right.mint)}
    positions = {}
    paths = {left.mint: left, right.mint: right}
    now = time.time_ns()
    for path in (left, right):
        positions[path.mint] = core.Position(
            position_id=f"stress-{path.mint}",
            mint=path.mint,
            status=core.PositionStatus.OPEN,
            opened_ns=now,
            entry_sol=0.05,
            tokens=1.0,
            remaining=1.0,
            entry_price=path.entry_price,
            max_price=path.entry_price,
            last_price=path.entry_price,
            entry_signature=f"forced-{path.mint}",
        )

    timeline = []
    for path in (left, right):
        timeline.extend((item.relative_ms, path.mint, item.event) for item in path.events)
    timeline.extend((5_000.0, mint, None) for mint in paths)
    timeline.extend((60_000.0, mint, None) for mint in paths)
    timeline.sort(key=lambda item: (item[0], item[1], 0 if item[2] is None else 1))

    pending: dict[str, tuple[float, float, str]] = {}
    closed_at: dict[str, float] = {}
    partials: dict[str, list[float]] = defaultdict(list)
    last_prices = {left.mint: left.entry_price, right.mint: right.entry_price}
    invalid_fractions = 0
    oversells = 0
    duplicate_first_partial = 0
    fills = 0
    pnl = 0.0

    def apply_due(until_ms: float) -> None:
        nonlocal invalid_fractions, oversells, duplicate_first_partial, fills, pnl
        for mint, (due, fraction, reason) in list(pending.items()):
            if due > until_ms or mint not in positions:
                continue
            position = positions[mint]
            if not 0 < fraction <= 1:
                invalid_fractions += 1
                pending.pop(mint, None)
                continue
            price = fill_price(paths[mint], due, last_prices[mint])
            amount = position.remaining * fraction
            if amount > position.remaining + 1e-12:
                oversells += 1
                amount = position.remaining
            proceeds = amount * price / position.entry_price * position.entry_sol
            pnl += proceeds - amount * position.entry_sol
            position.remaining = max(0.0, position.remaining - amount)
            fills += 1
            if fraction < 0.999 and position.remaining > 1e-9:
                if not position.first_partial_done:
                    position.first_partial_done = True
                    position.first_partial_fraction = fraction
                elif not partials[mint]:
                    duplicate_first_partial += 1
                partials[mint].append(fraction)
                position.status = core.PositionStatus.PARTIAL
            else:
                position.remaining = 0.0
                position.status = core.PositionStatus.CLOSED
                closed_at[mint] = due
                positions.pop(mint, None)
            pending.pop(mint, None)

    for relative_ms, mint, event in timeline:
        apply_due(relative_ms)
        if mint not in positions:
            continue
        position = positions[mint]
        state = states[mint]
        if event is not None:
            state.apply(event, None)
            if event.price_sol:
                last_prices[mint] = float(event.price_sol)
        position.opened_ns = time.time_ns() - int(relative_ms * 1_000_000)
        if mint in pending:
            continue

        if event is None:
            if relative_ms >= settings.max_hold_ms:
                action, fraction, reason = "SELL_ALL", 1.0, "stress absolute hold horizon"
            elif relative_ms >= settings.failure_window_ms and not position.first_partial_done:
                action, fraction, reason = "SELL_ALL", 1.0, "stress confirmation watchdog"
            else:
                continue
        else:
            action, fraction, reason = base_exit(core.E4Policy(settings), position, state)
        if action.startswith("SELL"):
            pending[mint] = (relative_ms + delay_ms, fraction, reason)

    apply_due(61_000 + delay_ms)
    for mint, position in list(positions.items()):
        if mint not in pending:
            pending[mint] = (60_000 + delay_ms, 1.0, "final bounded close")
    apply_due(61_000 + delay_ms)

    return {
        "closed": len(closed_at),
        "open": len(positions),
        "invalid_fractions": invalid_fractions,
        "oversells": oversells,
        "duplicate_first_partial": duplicate_first_partial,
        "fills": fills,
        "max_close_ms": max(closed_at.values(), default=None),
        "partials": sum(len(values) for values in partials.values()),
        "net_pnl_sol": pnl,
        "rapid_pair": (
            left.rapid_1s >= 0.20
            or right.rapid_1s >= 0.20
            or left.rapid_5s >= 0.50
            or right.rapid_5s >= 0.50
        ),
    }


def run(events: Path, pairs: int, seed: int, output: Path) -> dict[str, Any]:
    paths = load_paths(events)
    if len(paths) < 2:
        raise RuntimeError("real-market event file does not contain two usable token paths")
    rng = random.Random(seed)
    pair_indices = []
    for _ in range(pairs):
        a, b = rng.sample(range(len(paths)), 2)
        pair_indices.append((a, b))
    settings = core.Settings(model_path=Path("missing-model.json"))
    scenarios = {}
    for delay in (0.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1_000.0):
        rows = [simulate_pair(paths[a], paths[b], delay, settings) for a, b in pair_indices]
        scenarios[f"{int(delay)}ms"] = {
            "pairs": len(rows),
            "positions": len(rows) * 2,
            "fully_closed": sum(row["closed"] for row in rows),
            "open_after_horizon": sum(row["open"] for row in rows),
            "invalid_fractions": sum(row["invalid_fractions"] for row in rows),
            "oversells": sum(row["oversells"] for row in rows),
            "duplicate_first_partial": sum(
                row["duplicate_first_partial"] for row in rows
            ),
            "fills": sum(row["fills"] for row in rows),
            "rapid_pairs": sum(row["rapid_pair"] for row in rows),
            "max_close_ms": max(
                (row["max_close_ms"] or 0 for row in rows), default=None
            ),
            "close_ms": summary(
                [row["max_close_ms"] for row in rows if row["max_close_ms"] is not None]
            ),
            "net_pnl_sol": sum(row["net_pnl_sol"] for row in rows),
        }

    rapid = [
        path for path in paths if path.rapid_1s >= 0.20 or path.rapid_5s >= 0.50
    ]
    failures = []
    for name, scenario in scenarios.items():
        if scenario["fully_closed"] != scenario["positions"]:
            failures.append(f"{name}: not all positions closed")
        if scenario["oversells"]:
            failures.append(f"{name}: oversells={scenario['oversells']}")
        if scenario["invalid_fractions"]:
            failures.append(f"{name}: invalid_fractions={scenario['invalid_fractions']}")
        if scenario["duplicate_first_partial"]:
            failures.append(
                f"{name}: duplicate_first_partial={scenario['duplicate_first_partial']}"
            )
    report = {
        "report_version": "e4-real-market-concurrency-v1",
        "hypothesis_only": True,
        "synthetic_tokens": 0,
        "synthetic_prices": 0,
        "source": str(events),
        "real_token_paths": len(paths),
        "rapid_token_paths": len(rapid),
        "pairing_method": (
            "Two untouched real token trajectories are time-aligned only to stress "
            "two-position concurrency; every price and market event remains observed data."
        ),
        "pairs_per_latency": pairs,
        "scenarios": scenarios,
        "passed": not failures,
        "failures": failures,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Stress E4 with paired real-market token trajectories"
    )
    value.add_argument("--events", type=Path, required=True)
    value.add_argument("--pairs", type=int, default=2_000)
    value.add_argument("--seed", type=int, default=44)
    value.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/e4-real-market-concurrency.json"),
    )
    return value


if __name__ == "__main__":
    args = parser().parse_args()
    result = run(args.events, max(1, args.pairs), args.seed, args.output)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["passed"] else 1)

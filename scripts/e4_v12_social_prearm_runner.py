#!/usr/bin/env python3
"""Compatibility boundary for social research; never signs or sends transactions.

Both historical and forward evaluation use the canonical reserve replay engine.
Strategy rules and admission thresholds are not relaxed by this adapter.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts import e4_v12_true_latency_replay as economics


def load_events(events_path: Path) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    with Path(events_path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{events_path}:{line_number}: expected an event object")
            mint = str(row.get("mint") or "")
            if mint:
                grouped.setdefault(mint, []).append(row)
    for rows in grouped.values():
        rows.sort(key=economics.event_sort_key)
        for sequence, row in enumerate(rows):
            row["__sequence"] = sequence
    return grouped


def parse_latencies(value: str) -> list[float]:
    values = [float(item.strip()) for item in value.split(",") if item.strip()]
    if not values or any(not math.isfinite(item) or item < 0 for item in values):
        raise ValueError("latencies must be finite, nonnegative, and nonempty")
    return list(dict.fromkeys(values))


def economic_run(run: Any, mints: set[str]) -> Any:
    """Cache reserve indexing, not evaluation results, across the rule search."""
    cached = getattr(run, "_social_economic_run", None)
    if cached is None:
        positions = {str(row["mint"]): dict(row) for row in economics.same_window_e4(run.batch) if row.get("mint")}
        cached = economics.RunData(run.run_id, run.batch, {}, {}, positions)
        run._social_economic_run = cached
    for mint in mints:
        if mint in cached.events_by_mint:
            continue
        rows = [dict(row) for row in run.grouped.get(mint, [])]
        rows.sort(key=economics.event_sort_key)
        for sequence, row in enumerate(rows):
            row["__sequence"] = sequence
        cached.events_by_mint[mint] = rows
        cached.reserves_by_mint[mint] = [
            state for sequence, row in enumerate(rows)
            if (state := economics.reserve_from_row(row, sequence)) is not None
        ]
    return cached


def normalize_prediction(row: Mapping[str, Any], run: Any) -> dict[str, Any]:
    result = dict(row)
    result["run_id"] = run.run_id
    result["entry_fraction"] = float(row.get("requested_fraction", row.get("entry_fraction", 0.0185)))
    result["family"] = str(row.get("mode") or row.get("family") or "v12_social_prearm")
    if not 0 < result["entry_fraction"] <= 1:
        raise ValueError("invalid prediction entry fraction")
    return result


def aggregate_economics(
    runs: Sequence[Any], predictions: Sequence[Mapping[str, Any]], latencies: Sequence[float],
    *, starting_balance_sol: float, max_output_shortfall_bps: int,
) -> dict[str, Any]:
    """Use ONE continuous bankroll per latency, including rejected-fill fees."""
    from scripts import e4_v12_golden_thesis_search as golden

    if not math.isfinite(starting_balance_sol) or starting_balance_sol <= 0:
        raise ValueError("starting balance must be positive and finite")
    if not latencies or any(not math.isfinite(float(x)) or float(x) < 0 for x in latencies):
        raise ValueError("invalid latency scenarios")
    by_id = {run.run_id: run for run in runs}
    if len(by_id) != len(runs):
        raise ValueError("duplicate run IDs would overwrite economic evidence")
    converted: list[dict[str, Any]] = []
    selected: dict[str, set[str]] = {key: set() for key in by_id}
    for row in predictions:
        run_id = str(row.get("run_id") or "")
        mint = str(row.get("mint") or "")
        if run_id not in by_id or mint not in by_id[run_id].grouped:
            raise ValueError(f"prediction has no captured run/mint: {run_id}/{mint}")
        selected[run_id].add(mint)
        converted.append(normalize_prediction(row, by_id[run_id]))
    replay_runs = {key: economic_run(run, selected[key]) for key, run in by_id.items()}
    output: dict[str, Any] = {}
    for latency in latencies:
        result = economics.portfolio(
            replay_runs, converted, latency_ms=float(latency),
            output_shortfall_bps=max_output_shortfall_bps,
            starting_balance_sol=starting_balance_sol, entry_fraction=0.0185,
            maximum_position_sol=0.30, reserve_sol=0.03, pump_fee_bps=125,
            confirmation_ms=1500.0, unconfirmed_timeout_ms=1500.0, max_concurrency=2,
        )
        positions = result["positions"]
        fees = float(result["rejection_fees_sol"])
        reconciled = sum(float(row["pnl_sol"]) for row in positions) - fees
        if not math.isclose(reconciled, float(result["net_pnl_sol"]), abs_tol=1e-8):
            raise ValueError("portfolio P&L does not reconcile with trades and rejection fees")
        gains = sum(max(0.0, float(row["pnl_sol"])) for row in positions)
        losses = sum(max(0.0, -float(row["pnl_sol"])) for row in positions) + fees
        key = str(int(latency) if float(latency).is_integer() else latency)
        output[key] = {
            **result, "trades": result["closed"],
            "wilson_low": golden.wilson_lower(result["wins"], result["closed"]),
            "profit_factor": gains / losses if losses > 0 else (999.0 if gains > 0 else None),
            "rejected": dict(Counter(row["reason"] for row in result["rejections"])),
        }
    return output


def install_compatibility() -> None:
    required = ("event_sort_key", "reserve_from_row", "same_window_e4", "portfolio")
    missing = [name for name in required if not callable(getattr(economics, name, None))]
    if missing:
        raise RuntimeError(f"canonical replay API is incomplete: {missing}")
    economics.load_events = load_events
    economics.event_order = economics.event_sort_key
    economics.parse_latencies = parse_latencies
    from scripts import e4_v12_golden_thesis_search as golden
    # The old aggregate called retired Prediction/replay_latency APIs. Migrate
    # that entire boundary instead of adding unrelated compatibility stubs.
    golden.aggregate_economics = aggregate_economics


def main() -> int:
    install_compatibility()
    from scripts import e4_v12_social_prearm_search as social
    return int(social.main())


if __name__ == "__main__":
    raise SystemExit(main())

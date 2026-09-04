#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from memecoin_bot import e4_direct_copy_v12 as direct
from memecoin_bot import e4_hardening_v12 as v12
from memecoin_bot import e4_role_model_v12 as role_model

STARTING_BALANCE_SOL = 3.0
DIRECT_FAMILY = direct.DIRECT_COPY_FAMILY


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


def load_base() -> Any:
    path = Path(__file__).with_name("e4_live_market_stress.py")
    name = "e4_v12_live_3sol_replay_base"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def parse_pair(value: str) -> tuple[Path, Path, str]:
    parts = value.split(":", 2)
    if len(parts) == 2:
        batch, events = parts
        label = Path(batch).parent.parent.name or Path(batch).stem
    elif len(parts) == 3:
        label, batch, events = parts
    else:
        raise argparse.ArgumentTypeError("pair must be BATCH:EVENTS or LABEL:BATCH:EVENTS")
    return Path(batch), Path(events), label


def load_events(base: Any, path: Path) -> dict[str, list[Any]]:
    fields = {
        "event_id",
        "kind",
        "mint",
        "received_ns",
        "signature",
        "slot",
        "event_index",
        "trader",
        "sol_amount",
        "token_amount",
        "price_sol",
        "fdv_usd",
        "complete",
        "creator",
        "raw",
    }
    grouped: dict[str, list[Any]] = defaultdict(list)
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            payload = json.loads(line)
            event = base.LiveEvent(**{key: payload.get(key) for key in fields})
            grouped[event.mint].append(event)
    for rows in grouped.values():
        rows.sort(key=lambda item: (item.received_ns, item.slot, item.event_index))
    return grouped


def same_window_e4(batch: Mapping[str, Any]) -> list[dict[str, Any]]:
    positions = list((batch.get("actual_e4_fresh_sample") or {}).get("positions") or [])
    cohort = list((batch.get("capture") or {}).get("cohort") or [])
    starts = [integer(row.get("received_ns")) for row in cohort if integer(row.get("received_ns")) > 0]
    if not starts:
        return []
    start = min(starts) / 1e9 - 5.0
    tail = finite((batch.get("capture") or {}).get("tail_seconds_observed"))
    end = max(starts) / 1e9 + max(5.0, tail + 5.0)
    return [
        dict(row)
        for row in positions
        if start <= finite(row.get("entry_time")) <= end
    ]


def metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    pnl = [finite(row.get("pnl_sol")) for row in rows]
    wins = [value for value in pnl if value > 0]
    losses = [value for value in pnl if value <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(value for value in losses if value < 0))
    return {
        "closed": len(rows),
        "wins": len(wins),
        "losses": len(rows) - len(wins),
        "win_rate": len(wins) / len(rows) if rows else None,
        "net_pnl_sol": sum(pnl),
        "average_pnl_sol": statistics.fmean(pnl) if pnl else None,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else None),
        "gross_profit_sol": gross_profit,
        "gross_loss_sol": gross_loss,
    }


def drawdown(rows: Sequence[Mapping[str, Any]], starting_balance: float) -> dict[str, Any]:
    ordered = sorted(
        rows,
        key=lambda row: (
            integer(row.get("exit_ns"), integer(row.get("entry_ns"))),
            str(row.get("mint") or ""),
        ),
    )
    equity = starting_balance
    peak = starting_balance
    trough = starting_balance
    max_drawdown_sol = 0.0
    max_drawdown_pct = 0.0
    for row in ordered:
        equity += finite(row.get("pnl_sol"))
        peak = max(peak, equity)
        trough = min(trough, equity)
        current = peak - equity
        max_drawdown_sol = max(max_drawdown_sol, current)
        if peak > 0:
            max_drawdown_pct = max(max_drawdown_pct, current / peak)
    return {
        "starting_balance_sol": starting_balance,
        "ending_balance_sol": equity,
        "minimum_equity_sol": trough,
        "max_drawdown_sol": max_drawdown_sol,
        "max_drawdown_fraction": max_drawdown_pct,
    }


def clear_replay_state(mints: Sequence[str]) -> None:
    # Only erase mutable per-mint replay state. Static creator models and policy
    # configuration remain exactly as the checked-out V12 build defines them.
    for mint in mints:
        role_model.reset_role_model_replay(mint)
        for name in ("_PROFILE_BY_MINT", "_CONTEXT_BY_MINT"):
            mapping = getattr(v12.v6, name, None)
            if isinstance(mapping, dict):
                mapping.pop(mint, None)


def simulate_batch(
    base: Any,
    batch_path: Path,
    events_path: Path,
    label: str,
    latency_ms: float,
    starting_balance: float,
) -> dict[str, Any]:
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    grouped = load_events(base, events_path)
    ordered_mints = sorted(
        grouped,
        key=lambda mint: (
            integer(grouped[mint][0].received_ns),
            mint,
        ),
    )
    clear_replay_state(ordered_mints)

    settings = base.core.Settings(model_path=Path("missing-model.json"))
    # Direct E4 copies are allowed to match E4's absolute source stake up to the
    # physically available 3 SOL bankroll; ordinary V12 families retain their
    # policy-requested percentage size.
    settings.max_position_fraction = 1.0

    candidates: list[Any] = []
    family_by_mint: dict[str, str] = {}
    source_sizes: dict[str, float] = {}
    retry_count = 0

    for mint in ordered_mints:
        rows = grouped[mint]
        role_model.reset_role_model_replay(mint)
        trade = base.simulate_token(rows, settings, latency_ms)
        profile = v12.v6._PROFILE_BY_MINT.get(mint)
        family = str(getattr(profile, "family", "") or "") if profile is not None else ""
        is_direct = family == DIRECT_FAMILY

        if trade is None and is_direct:
            original_slippage = settings.buy_slippage_bps
            try:
                settings.buy_slippage_bps = direct.direct_copy_slippage_bps(settings)
                role_model.reset_role_model_replay(mint)
                trade = base.simulate_token(rows, settings, latency_ms)
                profile = v12.v6._PROFILE_BY_MINT.get(mint)
                family = str(getattr(profile, "family", "") or "") if profile is not None else ""
                is_direct = family == DIRECT_FAMILY
                retry_count += 1
            finally:
                settings.buy_slippage_bps = original_slippage

        if trade is None:
            continue

        family_by_mint[mint] = family or "unknown"
        if is_direct:
            source = role_model.PIPELINES.e4_signal(mint)
            source_sol = finite(getattr(source, "entry_sol", 0.0)) if source is not None else 0.0
            source_sizes[mint] = source_sol
            if source_sol > 0:
                trade.requested_fraction = min(1.0, source_sol / max(starting_balance, 1e-12))
        candidates.append(trade)

    portfolio = dict(base.evaluate_portfolio(candidates, starting_balance, settings))
    positions = []
    for raw in portfolio.get("positions") or []:
        row = dict(raw)
        row["family"] = family_by_mint.get(str(row.get("mint") or ""), "unknown")
        positions.append(row)
    portfolio["positions"] = positions

    direct_rows = [row for row in positions if row.get("family") == DIRECT_FAMILY]
    noncopy_rows = [row for row in positions if row.get("family") != DIRECT_FAMILY]
    e4_rows = same_window_e4(batch)
    e4_mints = {str(row.get("mint") or "") for row in e4_rows}
    v12_mints = {str(row.get("mint") or "") for row in positions}
    e4_win_mints = {
        str(row.get("mint") or "")
        for row in e4_rows
        if finite(row.get("pnl_sol")) > 0
    }

    return {
        "label": label,
        "batch_path": str(batch_path),
        "events_path": str(events_path),
        "fresh_launches": integer((batch.get("capture") or {}).get("unique_launches")),
        "trade_events": integer((batch.get("capture") or {}).get("trade_events"), integer((batch.get("capture") or {}).get("decoded_events"))),
        "latency_ms": latency_ms,
        "starting_balance_sol": starting_balance,
        "candidate_count": len(candidates),
        "direct_copy_retry_count": retry_count,
        "direct_copy_source_sizes_sol": source_sizes,
        "portfolio": portfolio,
        "all_v12": {**metrics(positions), **drawdown(positions, starting_balance)},
        "direct_copy": metrics(direct_rows),
        "non_copy": metrics(noncopy_rows),
        "e4": metrics(e4_rows),
        "comparison": {
            "e4_trade_capture": len(v12_mints & e4_mints) / len(e4_mints) if e4_mints else None,
            "e4_winner_capture": len(v12_mints & e4_win_mints) / len(e4_win_mints) if e4_win_mints else None,
            "missed_e4_mints": sorted(e4_mints - v12_mints),
            "extra_v12_mints": sorted(v12_mints - e4_mints),
        },
    }


def combine(results: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for result in results:
        if key == "all_v12":
            rows.extend(dict(row) for row in (result.get("portfolio") or {}).get("positions") or [])
        else:
            positions = list((result.get("portfolio") or {}).get("positions") or [])
            if key == "direct_copy":
                rows.extend(dict(row) for row in positions if row.get("family") == DIRECT_FAMILY)
            elif key == "non_copy":
                rows.extend(dict(row) for row in positions if row.get("family") != DIRECT_FAMILY)
    return metrics(rows)


def combine_e4(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    closed = sum(integer((result.get("e4") or {}).get("closed")) for result in results)
    wins = sum(integer((result.get("e4") or {}).get("wins")) for result in results)
    net = sum(finite((result.get("e4") or {}).get("net_pnl_sol")) for result in results)
    gross_profit = sum(finite((result.get("e4") or {}).get("gross_profit_sol")) for result in results)
    gross_loss = sum(finite((result.get("e4") or {}).get("gross_loss_sol")) for result in results)
    return {
        "closed": closed,
        "wins": wins,
        "losses": closed - wins,
        "win_rate": wins / closed if closed else None,
        "net_pnl_sol": net,
        "gross_profit_sol": gross_profit,
        "gross_loss_sol": gross_loss,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else None),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay frozen V12 over live-captured Pump launch windows with an exact 3 SOL bankroll")
    parser.add_argument("--pair", action="append", type=parse_pair, default=[], metavar="[LABEL:]BATCH:EVENTS")
    parser.add_argument("--latency-ms", type=float, default=36.0)
    parser.add_argument("--starting-balance-sol", type=float, default=STARTING_BALANCE_SOL)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.pair:
        parser.error("at least one --pair is required")
    if args.starting_balance_sol <= 0:
        parser.error("starting balance must be positive")

    base = load_base()
    previous_to_core = base.LiveEvent.to_core

    def to_core_v12(self: Any) -> Any:
        event = previous_to_core(self)
        role_model.observe_market_event(event)
        return event

    base.LiveEvent.to_core = to_core_v12

    results = [
        simulate_batch(
            base,
            batch_path,
            events_path,
            label,
            args.latency_ms,
            args.starting_balance_sol,
        )
        for batch_path, events_path, label in args.pair
    ]

    aggregate = {
        "windows": len(results),
        "fresh_launches": sum(integer(result.get("fresh_launches")) for result in results),
        "starting_balance_per_window_sol": args.starting_balance_sol,
        "all_v12": combine(results, "all_v12"),
        "direct_copy": combine(results, "direct_copy"),
        "non_copy": combine(results, "non_copy"),
        "e4": combine_e4(results),
        "mean_ending_balance_sol": statistics.fmean(
            finite((result.get("all_v12") or {}).get("ending_balance_sol"))
            for result in results
        ),
        "worst_window_max_drawdown_sol": max(
            (finite((result.get("all_v12") or {}).get("max_drawdown_sol")) for result in results),
            default=0.0,
        ),
        "worst_window_max_drawdown_fraction": max(
            (finite((result.get("all_v12") or {}).get("max_drawdown_fraction")) for result in results),
            default=0.0,
        ),
    }

    payload = {
        "version": "e4-v12-live-3sol-replay-v1",
        "methodology": {
            "source": "raw event streams captured live from Pump launch windows",
            "policy": "checked-out authoritative V12 selection, sizing and exit stack",
            "starting_balance_sol": args.starting_balance_sol,
            "balance_reset": "3 SOL at the beginning of each independent live window",
            "latency_ms": args.latency_ms,
            "costs": "existing V12 Pump fee, route cost, slippage and price-impact model",
            "direct_copy_sizing": "observed E4 SOL stake capped at the available 3 SOL bankroll",
            "mainnet_orders_sent": False,
        },
        "aggregate": aggregate,
        "windows": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False), encoding="utf-8")

    print(json.dumps({
        "windows": aggregate["windows"],
        "fresh_launches": aggregate["fresh_launches"],
        "starting_balance_per_window_sol": aggregate["starting_balance_per_window_sol"],
        "all_v12": aggregate["all_v12"],
        "direct_copy": aggregate["direct_copy"],
        "non_copy": aggregate["non_copy"],
        "e4": aggregate["e4"],
        "mean_ending_balance_sol": aggregate["mean_ending_balance_sol"],
        "worst_window_max_drawdown_fraction": aggregate["worst_window_max_drawdown_fraction"],
    }, indent=2, sort_keys=True, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

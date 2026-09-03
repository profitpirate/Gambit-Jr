#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

from memecoin_bot import e4_direct_copy_v12 as direct
from memecoin_bot import e4_hardening_v12 as v12
from memecoin_bot import e4_role_model_v12 as role_model
from memecoin_bot import e4_copy_fidelity_v12 as copy_fidelity

E4_V12_COPY_FIDELITY_POLICY_SHA256 = "ec998b88acab48e678e47be0f4a4c9776e5185ecf483a8232f5e63ca1cf706dc"
copy_fidelity.assert_policy_fingerprint(E4_V12_COPY_FIDELITY_POLICY_SHA256)


def load_base():
    path = Path(__file__).with_name("e4_live_market_stress.py")
    name = "e4_v12_direct_copy_replay_base"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def same_window_e4(batch: Mapping[str, Any]) -> list[dict[str, Any]]:
    positions = list((batch.get("actual_e4_fresh_sample") or {}).get("positions") or [])
    cohort = list((batch.get("capture") or {}).get("cohort") or [])
    starts = [int(row.get("received_ns") or 0) for row in cohort if int(row.get("received_ns") or 0) > 0]
    if not starts:
        return []
    start = min(starts) / 1e9 - 5.0
    tail = float((batch.get("capture") or {}).get("tail_seconds_observed") or 0.0)
    end = max(starts) / 1e9 + max(5.0, tail + 5.0)
    return [dict(row) for row in positions if start <= float(row.get("entry_time") or 0) <= end]


def profit_factor(rows: list[Mapping[str, Any]]) -> float | None:
    positive = sum(float(row.get("pnl_sol") or 0) for row in rows if float(row.get("pnl_sol") or 0) > 0)
    negative = sum(float(row.get("pnl_sol") or 0) for row in rows if float(row.get("pnl_sol") or 0) < 0)
    return positive / abs(negative) if negative < 0 else None


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100.0 * value:.2f}%"


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay V12 forced E4 direct-copy execution over a live artifact")
    parser.add_argument("--batch", required=True)
    parser.add_argument("--events", required=True)
    parser.add_argument("--source-run", type=int, required=True)
    parser.add_argument("--latency-ms", type=float, default=36.0)
    parser.add_argument("--starting-balance-sol", type=float, default=1.2)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    base = load_base()
    batch = json.loads(Path(args.batch).read_text(encoding="utf-8"))
    previous_to_core = base.LiveEvent.to_core

    def to_core_v12(self):
        event = previous_to_core(self)
        role_model.observe_market_event(event)
        return event

    base.LiveEvent.to_core = to_core_v12
    fields = {
        "event_id", "kind", "mint", "received_ns", "signature", "slot", "event_index",
        "trader", "sol_amount", "token_amount", "price_sol", "fdv_usd", "complete", "creator", "raw",
    }
    grouped: dict[str, list[Any]] = defaultdict(list)
    with Path(args.events).open("r", encoding="utf-8") as handle:
        for line in handle:
            payload = json.loads(line)
            event = base.LiveEvent(**{key: payload.get(key) for key in fields})
            grouped[event.mint].append(event)
    for rows in grouped.values():
        rows.sort(key=lambda item: (item.received_ns, item.slot, item.event_index))

    settings = base.core.Settings(model_path=Path("missing-model.json"))
    # Direct E4 copies use absolute E4 source sizing. The replay permits up to
    # 100% of the available paper balance so the balance/reserve ceiling, not
    # the old percentage ladder, is the limiting factor.
    settings.max_position_fraction = 1.0
    candidates = []
    source_sizes: dict[str, float] = {}

    for mint, rows in grouped.items():
        role_model.reset_role_model_replay(mint)
        trade = base.simulate_token(rows, settings, args.latency_ms)
        profile = v12.v6._PROFILE_BY_MINT.get(mint)
        is_direct = bool(profile and str(getattr(profile, "family", "")) == direct.DIRECT_COPY_FAMILY)

        if trade is None and is_direct:
            original = settings.buy_slippage_bps
            try:
                settings.buy_slippage_bps = direct.direct_copy_slippage_bps(settings)
                role_model.reset_role_model_replay(mint)
                trade = base.simulate_token(rows, settings, args.latency_ms)
                profile = v12.v6._PROFILE_BY_MINT.get(mint)
                is_direct = bool(profile and str(getattr(profile, "family", "")) == direct.DIRECT_COPY_FAMILY)
            finally:
                settings.buy_slippage_bps = original

        if trade is None or not is_direct:
            continue
        source = role_model.PIPELINES.e4_signal(mint)
        source_sol = float(getattr(source, "entry_sol", 0.0) or 0.0) if source else 0.0
        source_sizes[mint] = source_sol
        if source_sol > 0:
            trade.requested_fraction = min(1.0, source_sol / max(args.starting_balance_sol, 1e-12))
        candidates.append(trade)

    portfolio = base.evaluate_portfolio(candidates, args.starting_balance_sol, settings)
    v12_rows = list(portfolio.get("positions") or [])
    e4_rows = same_window_e4(batch)
    e4_mints = {str(row.get("mint") or "") for row in e4_rows}
    v12_mints = {str(row.get("mint") or "") for row in v12_rows}
    e4_wins = {str(row.get("mint") or "") for row in e4_rows if float(row.get("pnl_sol") or 0) > 0}
    v12_wins = {str(row.get("mint") or "") for row in v12_rows if float(row.get("pnl_sol") or 0) > 0}
    e4_wr = len(e4_wins) / len(e4_rows) if e4_rows else None

    result = {
        "source_run": args.source_run,
        "fresh_launches": int((batch.get("capture") or {}).get("unique_launches") or 0),
        "latency_ms": args.latency_ms,
        "copy_fidelity_policy_sha256": E4_V12_COPY_FIDELITY_POLICY_SHA256,
        "direct_copy_slippage_bps": direct.direct_copy_slippage_bps(settings),
        "e4": {
            "closed": len(e4_rows),
            "wins": len(e4_wins),
            "win_rate": e4_wr,
            "net_pnl_sol": sum(float(row.get("pnl_sol") or 0) for row in e4_rows),
            "profit_factor": profit_factor(e4_rows),
        },
        "v12_direct_copy": {
            "candidates": len(candidates),
            "closed": len(v12_rows),
            "wins": len(v12_wins),
            "win_rate": portfolio.get("net_win_rate"),
            "net_pnl_sol": portfolio.get("net_pnl_sol"),
            "profit_factor": portfolio.get("profit_factor"),
            "ending_balance_sol": portfolio.get("ending_balance_sol"),
            "source_sizes_sol": source_sizes,
            "positions": v12_rows,
        },
        "comparison": {
            "e4_trade_capture": len(v12_mints & e4_mints) / len(e4_mints) if e4_mints else None,
            "e4_winner_capture": len(v12_mints & e4_wins) / len(e4_wins) if e4_wins else None,
            "both_won": len(v12_wins & e4_wins),
            "missed_e4_mints": sorted(e4_mints - v12_mints),
            "extra_v12_mints": sorted(v12_mints - e4_mints),
        },
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "source_run": args.source_run,
        "e4_closed": len(e4_rows),
        "e4_wr": pct(e4_wr),
        "v12_closed": len(v12_rows),
        "v12_wr": pct(portfolio.get("net_win_rate")),
        "trade_capture": pct(result["comparison"]["e4_trade_capture"]),
        "winner_capture": pct(result["comparison"]["e4_winner_capture"]),
        "v12_pnl_sol": portfolio.get("net_pnl_sol"),
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

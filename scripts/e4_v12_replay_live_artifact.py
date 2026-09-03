#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from memecoin_bot import e4_hardening_v12 as v12
from memecoin_bot import e4_role_model_v12 as role_model
from memecoin_bot.e4_pipelines_v10 import E4_WALLET

E4_V12_ROLE_MODEL_POLICY_SHA256 = "f4d5959b25f607bc667073b672d66570bf29d8d2b2020811605808ce08e032df"
role_model.assert_policy_fingerprint(E4_V12_ROLE_MODEL_POLICY_SHA256)


def load_base():
    path = Path(__file__).with_name("e4_live_market_stress.py")
    name = "e4_v12_replay_live_base"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def profit_factor(rows: list[Mapping[str, Any]]) -> float | None:
    wins = sum(float(row.get("pnl_sol") or 0) for row in rows if float(row.get("pnl_sol") or 0) > 0)
    losses = sum(float(row.get("pnl_sol") or 0) for row in rows if float(row.get("pnl_sol") or 0) < 0)
    return wins / abs(losses) if losses < 0 else None


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100.0 * value:.2f}%"


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


def direct_e4_decision_mints(grouped: Mapping[str, list[Any]], e4_mints: set[str]) -> tuple[set[str], dict[str, str]]:
    eligible: set[str] = set()
    reasons: dict[str, str] = {}
    for mint in e4_mints:
        rows = grouped.get(mint) or []
        sell_seen = False
        found = False
        for row in rows:
            if row.kind in {"SELL", "PUMPSWAP_SELL"}:
                sell_seen = True
            if row.trader != E4_WALLET or row.kind not in {"BUY", "PUMPSWAP_BUY"}:
                continue
            found = True
            fdv = float(row.fdv_usd or 0.0)
            if sell_seen:
                reasons[mint] = "sell_seen_before_e4_buy"
            elif fdv <= 0.0 or fdv > 8500.0:
                reasons[mint] = f"entry_fdv_outside_v12:{fdv:.2f}"
            else:
                eligible.add(mint)
                reasons[mint] = "direct_e4_buy_eligible"
            break
        if not found:
            reasons[mint] = "e4_buy_not_in_captured_launch_events"
    return eligible, reasons


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay permanent V12 over a captured live Pump artifact and compare to same-window E4")
    parser.add_argument("--batch", required=True)
    parser.add_argument("--events", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--latency-ms", type=float, default=36.0)
    parser.add_argument("--starting-balance-sol", type=float, default=1.2)
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
    candidates = []
    families: dict[str, str] = {}
    for mint, rows in grouped.items():
        role_model.reset_role_model_replay(mint)
        create = next((row for row in rows if row.kind == base.core.EventKind.CREATE.value), None)
        if create is not None and isinstance(create.raw, Mapping):
            v12.v8.observe_context(mint, create.raw)
            context = v12.v6._CONTEXT_BY_MINT.setdefault(mint, {})
            for key in ("creator", "name", "symbol", "uri", "token_program", "is_mayhem_mode"):
                value = create.raw.get(key)
                if value not in (None, ""):
                    context[key] = value
        trade = base.simulate_token(rows, settings, args.latency_ms)
        if trade is None:
            continue
        candidates.append(trade)
        profile = v12.v6._PROFILE_BY_MINT.get(mint)
        families[mint] = str(getattr(profile, "family", "unknown") or "unknown")

    portfolio = base.evaluate_portfolio(candidates, args.starting_balance_sol, settings)
    v12_rows = list(portfolio.get("positions") or [])
    e4_rows = same_window_e4(batch)
    e4_mints = {str(row.get("mint") or "") for row in e4_rows}
    v12_mints = {str(row.get("mint") or "") for row in v12_rows}
    candidate_mints = {trade.mint for trade in candidates}
    direct_eligible, direct_reasons = direct_e4_decision_mints(grouped, e4_mints)

    e4_wins = {str(row.get("mint") or "") for row in e4_rows if float(row.get("pnl_sol") or 0) > 0}
    v12_wins = {str(row.get("mint") or "") for row in v12_rows if float(row.get("pnl_sol") or 0) > 0}
    e4_wr = sum(float(row.get("pnl_sol") or 0) > 0 for row in e4_rows) / len(e4_rows) if e4_rows else None

    comparison = {
        "source_batch_run": 33716649440,
        "source_commit": batch.get("commit"),
        "replayed_commit": "77e4f7ae96af93b39874933e07a33c0fe7257705",
        "latency_ms": args.latency_ms,
        "starting_balance_sol": args.starting_balance_sol,
        "fresh_launches": int((batch.get("capture") or {}).get("unique_launches") or 0),
        "e4": {
            "closed_positions": len(e4_rows),
            "wins": len(e4_wins),
            "losses": len(e4_rows) - len(e4_wins),
            "net_win_rate": e4_wr,
            "net_pnl_sol": sum(float(row.get("pnl_sol") or 0) for row in e4_rows),
            "profit_factor": profit_factor(e4_rows),
            "mints": sorted(e4_mints),
        },
        "v12": {
            "candidate_trades_before_portfolio_concurrency": len(candidates),
            "closed_positions": len(v12_rows),
            "wins": len(v12_wins),
            "losses": len(v12_rows) - len(v12_wins),
            "net_win_rate": portfolio.get("net_win_rate"),
            "net_pnl_sol": portfolio.get("net_pnl_sol"),
            "profit_factor": portfolio.get("profit_factor"),
            "ending_balance_sol": portfolio.get("ending_balance_sol"),
            "max_concurrent_positions": portfolio.get("max_concurrent_positions"),
            "skipped_for_concurrency": portfolio.get("skipped_for_concurrency"),
            "families": families,
            "positions": v12_rows,
        },
        "selection_comparison": {
            "e4_positions_with_direct_v12_decision_authority": len(direct_eligible),
            "direct_decision_recall": len(direct_eligible) / len(e4_mints) if e4_mints else None,
            "v12_executable_candidate_overlap_with_e4": len(candidate_mints & e4_mints),
            "v12_closed_overlap_with_e4": len(v12_mints & e4_mints),
            "v12_extra_closed_mints": sorted(v12_mints - e4_mints),
            "e4_missed_at_execution_or_other_gates": sorted(e4_mints - candidate_mints),
            "e4_winners_captured_by_v12_closed": len(e4_wins & v12_mints),
            "e4_winner_capture_rate": len(e4_wins & v12_mints) / len(e4_wins) if e4_wins else None,
            "v12_and_e4_both_won": len(v12_wins & e4_wins),
            "direct_decision_reasons": direct_reasons,
        },
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(comparison, indent=2, sort_keys=True), encoding="utf-8")
    summary = {
        "fresh_launches": comparison["fresh_launches"],
        "e4_closed": len(e4_rows),
        "e4_wr": pct(e4_wr),
        "e4_pnl_sol": comparison["e4"]["net_pnl_sol"],
        "e4_pf": comparison["e4"]["profit_factor"],
        "v12_candidates": len(candidates),
        "v12_closed": len(v12_rows),
        "v12_wr": pct(portfolio.get("net_win_rate")),
        "v12_pnl_sol": portfolio.get("net_pnl_sol"),
        "v12_pf": portfolio.get("profit_factor"),
        "direct_decision_recall": pct(comparison["selection_comparison"]["direct_decision_recall"]),
        "e4_winner_capture_rate": pct(comparison["selection_comparison"]["e4_winner_capture_rate"]),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

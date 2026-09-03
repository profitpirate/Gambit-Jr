#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping

from memecoin_bot import e4_direct_copy_v12 as direct
from memecoin_bot import e4_hardening_v12 as v12
from memecoin_bot import e4_role_model_v12 as role_model


def load_base():
    path = Path(__file__).with_name("e4_live_market_stress.py")
    name = "e4_v12_copy_timing_audit_base"
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


def cohort_metrics(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    wins = sum(float(row.get("pnl_sol") or 0) > 0 for row in rows)
    return {
        "closed": len(rows),
        "wins": wins,
        "losses": len(rows) - wins,
        "win_rate": wins / len(rows) if rows else None,
        "net_pnl_sol": sum(float(row.get("pnl_sol") or 0) for row in rows),
        "profit_factor": profit_factor(rows),
    }


def median_field(rows: list[Mapping[str, Any]], field: str) -> float | None:
    values = [float(row[field]) for row in rows if row.get(field) is not None]
    return statistics.median(values) if values else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit V12 direct E4-copy timing and isolate copy-only forward P&L")
    parser.add_argument("--batch", required=True)
    parser.add_argument("--events", required=True)
    parser.add_argument("--source-run", type=int, required=True)
    parser.add_argument("--latency-ms", type=float, default=36.0)
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
    direct_decisions: set[str] = set()
    direct_filled: dict[str, Any] = {}
    direct_trade_audits: list[dict[str, Any]] = []
    non_copy_families: Counter[str] = Counter()
    missed_direct: list[dict[str, Any]] = []

    for mint, rows in grouped.items():
        role_model.reset_role_model_replay(mint)
        trade = base.simulate_token(rows, settings, args.latency_ms)
        profile = v12.v6._PROFILE_BY_MINT.get(mint)
        family = str(getattr(profile, "family", "") or "") if profile else ""
        is_direct = family == direct.DIRECT_COPY_FAMILY

        if trade is None and is_direct:
            direct_decisions.add(mint)
            original_slippage = settings.buy_slippage_bps
            try:
                settings.buy_slippage_bps = direct.direct_copy_slippage_bps(settings)
                role_model.reset_role_model_replay(mint)
                trade = base.simulate_token(rows, settings, args.latency_ms)
                profile = v12.v6._PROFILE_BY_MINT.get(mint)
                family = str(getattr(profile, "family", "") or "") if profile else ""
                is_direct = family == direct.DIRECT_COPY_FAMILY
            finally:
                settings.buy_slippage_bps = original_slippage

        if is_direct:
            direct_decisions.add(mint)
            source = role_model.PIPELINES.e4_signal(mint)
            source_ns = int(getattr(source, "observed_ns", 0) or 0) if source else 0
            source_price = float(getattr(source, "entry_price_sol", 0.0) or 0.0) if source else 0.0
            source_sol = float(getattr(source, "entry_sol", 0.0) or 0.0) if source else 0.0
            source_signature = str(getattr(source, "signature", "") or "") if source else ""
            if trade is None:
                missed_direct.append({
                    "mint": mint,
                    "family": family,
                    "source_observed_ns": source_ns,
                    "source_entry_price_sol": source_price,
                    "source_entry_sol": source_sol,
                    "source_signature": source_signature,
                    "reason": "direct-copy decision produced no simulated fill even at direct-copy slippage ceiling",
                })
                continue
            direct_filled[mint] = trade
            source_to_decision_ms = (trade.entry_decision_ns - source_ns) / 1e6 if source_ns else None
            decision_to_fill_ms = (trade.entry_fill_ns - trade.entry_decision_ns) / 1e6
            source_to_fill_ms = (trade.entry_fill_ns - source_ns) / 1e6 if source_ns else None
            fill_drift_bps = (
                (float(trade.entry_price_sol) / source_price - 1.0) * 10_000.0
                if source_price > 0 and float(trade.entry_price_sol) > 0
                else None
            )
            direct_trade_audits.append({
                "mint": mint,
                "family": family,
                "source_signature": source_signature,
                "source_observed_ns": source_ns,
                "source_entry_price_sol": source_price,
                "source_entry_sol": source_sol,
                "decision_ns": int(trade.entry_decision_ns),
                "fill_ns": int(trade.entry_fill_ns),
                "fill_price_sol": float(trade.entry_price_sol),
                "source_to_decision_ms": source_to_decision_ms,
                "decision_to_fill_ms": decision_to_fill_ms,
                "source_to_fill_ms": source_to_fill_ms,
                "fill_drift_bps": fill_drift_bps,
            })
        elif trade is not None:
            non_copy_families[family or "unknown"] += 1

    primary = (((batch.get("hypothetical_scenarios") or {}).get("36ms") or {}).get("balances") or {}).get("1.2") or {}
    primary_positions = [dict(row) for row in primary.get("positions") or []]
    direct_position_mints = set(direct_filled)
    direct_positions = [row for row in primary_positions if str(row.get("mint") or "") in direct_position_mints]
    non_copy_positions = [row for row in primary_positions if str(row.get("mint") or "") not in direct_position_mints]

    e4_rows = same_window_e4(batch)
    e4_by_mint = {str(row.get("mint") or ""): row for row in e4_rows}
    v12_by_mint = {str(row.get("mint") or ""): row for row in direct_positions}
    for row in direct_trade_audits:
        mint = str(row["mint"])
        v12_row = v12_by_mint.get(mint)
        e4_row = e4_by_mint.get(mint)
        row["v12_closed"] = v12_row is not None
        row["v12_pnl_sol"] = float(v12_row.get("pnl_sol") or 0) if v12_row else None
        row["v12_won"] = bool(v12_row and float(v12_row.get("pnl_sol") or 0) > 0)
        row["e4_closed"] = e4_row is not None
        row["e4_pnl_sol"] = float(e4_row.get("pnl_sol") or 0) if e4_row else None
        row["e4_won"] = bool(e4_row and float(e4_row.get("pnl_sol") or 0) > 0)

    e4_mints = set(e4_by_mint)
    e4_winner_mints = {mint for mint, row in e4_by_mint.items() if float(row.get("pnl_sol") or 0) > 0}
    direct_closed_mints = set(v12_by_mint)
    direct_winner_mints = {mint for mint, row in v12_by_mint.items() if float(row.get("pnl_sol") or 0) > 0}

    result = {
        "source_run": args.source_run,
        "latency_ms": args.latency_ms,
        "fresh_launches": int((batch.get("capture") or {}).get("unique_launches") or (batch.get("capture") or {}).get("new_launches") or 0),
        "direct_copy_slippage_bps": direct.direct_copy_slippage_bps(settings),
        "direct_copy_positions": direct_positions,
        "non_copy_positions": non_copy_positions,
        "direct_copy_trades": sorted(direct_trade_audits, key=lambda row: (int(row.get("source_observed_ns") or 0), row["mint"])),
        "missed_direct_copy_fills": sorted(missed_direct, key=lambda row: row["mint"]),
        "non_copy_family_counts": dict(sorted(non_copy_families.items())),
        "direct_copy": cohort_metrics(direct_positions),
        "non_copy": cohort_metrics(non_copy_positions),
        "all_v12": cohort_metrics(primary_positions),
        "e4": cohort_metrics(e4_rows),
        "timing": {
            "median_source_to_decision_ms": median_field(direct_trade_audits, "source_to_decision_ms"),
            "median_decision_to_fill_ms": median_field(direct_trade_audits, "decision_to_fill_ms"),
            "median_source_to_fill_ms": median_field(direct_trade_audits, "source_to_fill_ms"),
            "median_fill_drift_bps": median_field(direct_trade_audits, "fill_drift_bps"),
        },
        "comparison": {
            "direct_copy_decisions": len(direct_decisions),
            "direct_copy_filled_candidates": len(direct_filled),
            "direct_copy_closed_positions": len(direct_positions),
            "non_copy_closed_positions": len(non_copy_positions),
            "e4_closed_positions": len(e4_rows),
            "decision_recall": len(direct_decisions & e4_mints) / len(e4_mints) if e4_mints else None,
            "filled_candidate_recall": len(set(direct_filled) & e4_mints) / len(e4_mints) if e4_mints else None,
            "closed_trade_capture": len(direct_closed_mints & e4_mints) / len(e4_mints) if e4_mints else None,
            "winner_mint_capture": len(direct_closed_mints & e4_winner_mints) / len(e4_winner_mints) if e4_winner_mints else None,
            "both_won": len(direct_winner_mints & e4_winner_mints),
            "missed_e4_mints": sorted(e4_mints - direct_closed_mints),
            "extra_direct_copy_mints": sorted(direct_closed_mints - e4_mints),
            "extra_non_copy_mints": sorted({str(row.get("mint") or "") for row in non_copy_positions} - e4_mints),
        },
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "source_run": args.source_run,
        "e4_closed": result["e4"]["closed"],
        "e4_wr": result["e4"]["win_rate"],
        "direct_copy_closed": result["direct_copy"]["closed"],
        "direct_copy_wr": result["direct_copy"]["win_rate"],
        "direct_copy_pnl_sol": result["direct_copy"]["net_pnl_sol"],
        "non_copy_closed": result["non_copy"]["closed"],
        "non_copy_wr": result["non_copy"]["win_rate"],
        "decision_recall": result["comparison"]["decision_recall"],
        "closed_trade_capture": result["comparison"]["closed_trade_capture"],
        "median_decision_to_fill_ms": result["timing"]["median_decision_to_fill_ms"],
        "median_fill_drift_bps": result["timing"]["median_fill_drift_bps"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

E4_HISTORICAL_NET_WR = 155 / 258
E4_HISTORICAL_NET_PF = 4.92
E4_HISTORICAL_NET_POSITIONS = 258

FINGERPRINT_PATHS = (
    "src/memecoin_bot/e4_hardening_v6.py",
    "src/memecoin_bot/e4_hardening_v9.py",
    "src/memecoin_bot/e4_hardening_v10.py",
    "src/memecoin_bot/e4_hardening_v12.py",
    "src/memecoin_bot/e4_pipeline_manager_v11.py",
    "src/memecoin_bot/e4_pipelines_v10.py",
    "src/memecoin_bot/e4_final.py",
    "src/memecoin_bot/e4_exec/__main__.py",
    "models/e4/e4-creator-expectancy.json",
    "models/e4/e4-discovered-creators.json",
    "models/e4/e4-v12-selection.json",
    "scripts/e4_live_market_stress.py",
    "scripts/e4_300_launch_holdout.py",
    "scripts/e4_300_launch_holdout_v12.py",
)


def load(path: Path, default: Any = None) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def fingerprint() -> str:
    digest = hashlib.sha256()
    for name in FINGERPRINT_PATHS:
        path = Path(name)
        if not path.exists():
            raise FileNotFoundError(name)
        digest.update(name.encode()); digest.update(b"\0"); digest.update(path.read_bytes()); digest.update(b"\0")
    return digest.hexdigest()


def wilson(wins: int, total: int, z: float = 1.959963984540054) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    p = wins / total
    d = 1 + z*z/total
    c = (p + z*z/(2*total)) / d
    m = z * math.sqrt(p*(1-p)/total + z*z/(4*total*total)) / d
    return max(0.0, c-m), min(1.0, c+m)


def profit_factor(rows: Iterable[Mapping[str, Any]]) -> float | None:
    rows = list(rows)
    positive = sum(float(r.get("pnl_sol") or 0) for r in rows if float(r.get("pnl_sol") or 0) > 0)
    negative = sum(float(r.get("pnl_sol") or 0) for r in rows if float(r.get("pnl_sol") or 0) < 0)
    return positive / abs(negative) if negative < 0 else None


def key(row: Mapping[str, Any]) -> str:
    return f"{row.get('mint')}|{int(row.get('entry_ns') or row.get('entry_time') or 0)}"


def same_window_e4(batch: Mapping[str, Any]) -> list[dict[str, Any]]:
    fresh = (batch.get("actual_e4_fresh_sample") or {}).get("positions") or []
    cohort = (batch.get("capture") or {}).get("cohort") or []
    starts = [int(r.get("received_ns") or 0) for r in cohort if int(r.get("received_ns") or 0) > 0]
    if not starts:
        return []
    start = min(starts)/1e9 - 5
    tail = float((batch.get("capture") or {}).get("tail_seconds_observed") or 0)
    end = max(starts)/1e9 + max(5.0, tail + 5.0)
    return [dict(r) for r in fresh if start <= float(r.get("entry_time") or 0) <= end]


def _row_metrics(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    wins = sum(float(r.get("pnl_sol") or 0) > 0 for r in rows)
    lo, hi = wilson(wins, len(rows))
    half = (hi-lo)/2 if lo is not None and hi is not None else None
    losers = [r for r in rows if float(r.get("pnl_sol") or 0) <= 0]
    return {
        "closed": len(rows),
        "wins": wins,
        "losses": len(rows)-wins,
        "win_rate": wins/len(rows) if rows else None,
        "wilson_low": lo,
        "wilson_high": hi,
        "wilson_half_width": half,
        "net_pnl_sol": sum(float(r.get("pnl_sol") or 0) for r in rows),
        "profit_factor": profit_factor(rows),
        "median_hold_ms": statistics.median([float(r.get("hold_ms") or 0) for r in rows]) if rows else None,
        "median_entry_fdv_usd": statistics.median([float(r.get("entry_fdv_usd") or 0) for r in rows]) if rows else None,
        "losers_within_5s": sum(float(r.get("hold_ms") or 0) <= 5000 for r in losers)/len(losers) if losers else None,
    }


def aggregate(evidence: Mapping[str, Any]) -> dict[str, Any]:
    gambit = list((evidence.get("gambit_positions") or {}).values())
    direct = list((evidence.get("direct_copy_positions") or {}).values())
    non_copy = list((evidence.get("non_copy_positions") or {}).values())
    e4 = list((evidence.get("same_window_e4_positions") or {}).values())
    batches = list(evidence.get("batches") or [])
    copy_audits = dict(evidence.get("copy_audits") or {})
    launches = sum(int(r.get("launches") or 0) for r in batches)
    audited_batch_ids = {str(batch_id) for batch_id in copy_audits}
    audited_batches = [r for r in batches if str(r.get("batch_id")) in audited_batch_ids]
    audited_launches = sum(int(r.get("launches") or 0) for r in audited_batches)

    gm = _row_metrics(gambit)
    dm = _row_metrics(direct)
    nm = _row_metrics(non_copy)
    em = _row_metrics(e4)

    e4_mints = {str(r.get("mint") or "") for r in e4 if str(r.get("mint") or "")}
    e4_winner_mints = {str(r.get("mint") or "") for r in e4 if str(r.get("mint") or "") and float(r.get("pnl_sol") or 0) > 0}
    direct_mints = {str(r.get("mint") or "") for r in direct if str(r.get("mint") or "")}
    direct_winner_mints = {str(r.get("mint") or "") for r in direct if str(r.get("mint") or "") and float(r.get("pnl_sol") or 0) > 0}

    timing_rows: list[Mapping[str, Any]] = []
    family_counts: Counter[str] = Counter()
    direct_decisions = 0
    direct_fills = 0
    for audit in copy_audits.values():
        direct_decisions += int((audit.get("comparison") or {}).get("direct_copy_decisions") or 0)
        direct_fills += int((audit.get("comparison") or {}).get("direct_copy_filled_candidates") or 0)
        timing_rows.extend(audit.get("direct_copy_trades") or [])
        family_counts.update({str(k): int(v) for k, v in (audit.get("non_copy_family_counts") or {}).items()})

    def median_field(name: str) -> float | None:
        values = [float(r[name]) for r in timing_rows if r.get(name) is not None]
        return statistics.median(values) if values else None

    invariants = all(int(r.get("reentries") or 0) == 0 and int(r.get("max_concurrent_positions") or 0) <= 2 for r in batches)
    copy_sufficient = (
        dm["closed"] >= 100
        and len(audited_batches) >= 3
        and dm["wilson_half_width"] is not None
        and float(dm["wilson_half_width"]) <= 0.10
        and invariants
    )

    summary = {
        "batch_count": len(batches),
        "copy_audited_batch_count": len(audited_batches),
        "unique_launches_observed": launches,
        "copy_audited_launches": audited_launches,
        "gambit_closed_positions": gm["closed"],
        "gambit_wins": gm["wins"],
        "gambit_losses": gm["losses"],
        "gambit_net_win_rate": gm["win_rate"],
        "gambit_wilson_95_low": gm["wilson_low"],
        "gambit_wilson_95_high": gm["wilson_high"],
        "gambit_wilson_half_width": gm["wilson_half_width"],
        "gambit_net_pnl_sol": gm["net_pnl_sol"],
        "gambit_profit_factor": gm["profit_factor"],
        "gambit_trade_rate_per_launch": gm["closed"]/launches if launches else None,
        "gambit_median_hold_ms": gm["median_hold_ms"],
        "gambit_median_entry_fdv_usd": gm["median_entry_fdv_usd"],
        "gambit_losers_within_5s": gm["losers_within_5s"],
        "direct_copy_closed_positions": dm["closed"],
        "direct_copy_wins": dm["wins"],
        "direct_copy_losses": dm["losses"],
        "direct_copy_net_win_rate": dm["win_rate"],
        "direct_copy_wilson_95_low": dm["wilson_low"],
        "direct_copy_wilson_95_high": dm["wilson_high"],
        "direct_copy_wilson_half_width": dm["wilson_half_width"],
        "direct_copy_net_pnl_sol": dm["net_pnl_sol"],
        "direct_copy_profit_factor": dm["profit_factor"],
        "direct_copy_trade_rate_per_launch": dm["closed"]/audited_launches if audited_launches else None,
        "direct_copy_median_hold_ms": dm["median_hold_ms"],
        "direct_copy_losers_within_5s": dm["losers_within_5s"],
        "non_copy_closed_positions": nm["closed"],
        "non_copy_wins": nm["wins"],
        "non_copy_losses": nm["losses"],
        "non_copy_net_win_rate": nm["win_rate"],
        "non_copy_net_pnl_sol": nm["net_pnl_sol"],
        "non_copy_profit_factor": nm["profit_factor"],
        "non_copy_family_counts": dict(sorted(family_counts.items())),
        "same_window_e4_closed_positions": em["closed"],
        "same_window_e4_wins": em["wins"],
        "same_window_e4_net_win_rate": em["win_rate"],
        "same_window_e4_wilson_95_low": em["wilson_low"],
        "same_window_e4_wilson_95_high": em["wilson_high"],
        "same_window_e4_net_pnl_sol": em["net_pnl_sol"],
        "same_window_e4_trade_rate_per_launch": em["closed"]/launches if launches else None,
        "direct_copy_trade_capture": len(direct_mints & e4_mints)/len(e4_mints) if e4_mints else None,
        "direct_copy_winner_mint_capture": len(direct_mints & e4_winner_mints)/len(e4_winner_mints) if e4_winner_mints else None,
        "direct_copy_both_won": len(direct_winner_mints & e4_winner_mints),
        "direct_copy_extra_mints": sorted(direct_mints - e4_mints),
        "direct_copy_missed_e4_mints": sorted(e4_mints - direct_mints),
        "direct_copy_decisions": direct_decisions,
        "direct_copy_filled_candidates": direct_fills,
        "direct_copy_median_source_to_decision_ms": median_field("source_to_decision_ms"),
        "direct_copy_median_decision_to_fill_ms": median_field("decision_to_fill_ms"),
        "direct_copy_median_source_to_fill_ms": median_field("source_to_fill_ms"),
        "direct_copy_median_fill_drift_bps": median_field("fill_drift_bps"),
        "e4_historical_net_win_rate": E4_HISTORICAL_NET_WR,
        "e4_historical_net_profit_factor": E4_HISTORICAL_NET_PF,
        "e4_historical_exact_positions": E4_HISTORICAL_NET_POSITIONS,
        "invariants_ok": invariants,
        "sufficient_evidence": copy_sufficient,
    }

    if not copy_audits:
        summary["classification"] = "COPY_AUDIT_MISSING"
    elif not copy_sufficient:
        summary["classification"] = "INSUFFICIENT_COPY_ONLY_EVIDENCE"
    else:
        wr = float(summary["direct_copy_net_win_rate"] or 0)
        pf = float(summary["direct_copy_profit_factor"] or 0)
        pnl = float(summary["direct_copy_net_pnl_sol"] or 0)
        if wr >= E4_HISTORICAL_NET_WR - 0.08 and pf >= 2.0 and pnl > 0:
            summary["classification"] = "E4_LIKE_FORWARD_EDGE"
        elif pf > 1.0 and pnl > 0:
            summary["classification"] = "POSITIVE_BUT_BELOW_E4"
        else:
            summary["classification"] = "FAILED_FORWARD_EDGE_CERTIFICATION"
    return summary


def markdown(path: Path, evidence: Mapping[str, Any]) -> None:
    s = evidence["summary"]
    pct = lambda x: "n/a" if x is None else f"{100*float(x):.2f}%"
    path.write_text("\n".join([
        "# Gambit E4 V12 forward evidence", "",
        f"**Classification:** {s['classification']}",
        f"**Frozen strategy fingerprint:** `{evidence['strategy_fingerprint']}`",
        f"**Independent batches:** {s['batch_count']}",
        f"**Copy-audited batches:** {s['copy_audited_batch_count']}",
        f"**Fresh launches:** {s['unique_launches_observed']}", "",
        "## Direct E4-copy cohort",
        f"**Closed:** {s['direct_copy_closed_positions']}",
        f"**WR:** {pct(s['direct_copy_net_win_rate'])}",
        f"**95% CI:** {pct(s['direct_copy_wilson_95_low'])} – {pct(s['direct_copy_wilson_95_high'])}",
        f"**P&L:** {s['direct_copy_net_pnl_sol']:.6f} SOL",
        f"**PF:** {s['direct_copy_profit_factor']}",
        f"**E4 trade capture:** {pct(s['direct_copy_trade_capture'])}",
        f"**E4 winner-mint capture:** {pct(s['direct_copy_winner_mint_capture'])}",
        f"**Median source→decision:** {s['direct_copy_median_source_to_decision_ms']} ms",
        f"**Median decision→fill:** {s['direct_copy_median_decision_to_fill_ms']} ms",
        f"**Median source→fill:** {s['direct_copy_median_source_to_fill_ms']} ms",
        f"**Median fill drift:** {s['direct_copy_median_fill_drift_bps']} bps", "",
        "## Non-copy V12 cohort",
        f"**Closed:** {s['non_copy_closed_positions']}",
        f"**WR:** {pct(s['non_copy_net_win_rate'])}",
        f"**P&L:** {s['non_copy_net_pnl_sol']:.6f} SOL",
        f"**PF:** {s['non_copy_profit_factor']}",
        f"**Families:** {json.dumps(s['non_copy_family_counts'], sort_keys=True)}", "",
        "## All V12 positions (diagnostic only)",
        f"**Closed:** {s['gambit_closed_positions']}",
        f"**WR:** {pct(s['gambit_net_win_rate'])}",
        f"**P&L:** {s['gambit_net_pnl_sol']:.6f} SOL",
        f"**PF:** {s['gambit_profit_factor']}", "",
        "## Same-window E4",
        f"**Closed:** {s['same_window_e4_closed_positions']}",
        f"**WR:** {pct(s['same_window_e4_net_win_rate'])}",
        f"**P&L:** {s['same_window_e4_net_pnl_sol']:.6f} SOL",
        f"**Historical E4 exact-net benchmark:** {pct(E4_HISTORICAL_NET_WR)}, PF ~{E4_HISTORICAL_NET_PF}",
        f"**Copy-only evidence sufficient:** {'YES' if s['sufficient_evidence'] else 'NO'}", "",
        "E4-parity classification uses only explicitly audited direct-copy positions. Creator, social, narrative and other V12 entries remain visible as diagnostics but cannot affect the copy-only WR/PF/P&L gate."
    ]) + "\n", encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser(); p.add_argument("--batch", required=True); p.add_argument("--batch-id", required=True)
    p.add_argument("--evidence", default="models/e4/e4-v12-forward-evidence.json"); p.add_argument("--markdown", default="models/e4/e4-v12-forward-evidence.md")
    p.add_argument("--copy-audit", default="")
    a = p.parse_args(); batch = load(Path(a.batch), {}); fp = fingerprint(); path = Path(a.evidence); evidence = load(path, None)
    if evidence is None:
        evidence = {"version":"e4-v12-forward-evidence-v2","strategy_fingerprint":fp,"fingerprint_paths":list(FINGERPRINT_PATHS),"batches":[],"gambit_positions":{},"direct_copy_positions":{},"non_copy_positions":{},"same_window_e4_positions":{},"copy_audits":{}}
    elif evidence.get("strategy_fingerprint") != fp:
        raise RuntimeError(f"V12 strategy fingerprint changed old={evidence.get('strategy_fingerprint')} new={fp}")

    evidence["version"] = "e4-v12-forward-evidence-v2"
    evidence.setdefault("gambit_positions", {})
    evidence.setdefault("direct_copy_positions", {})
    evidence.setdefault("non_copy_positions", {})
    evidence.setdefault("same_window_e4_positions", {})
    evidence.setdefault("copy_audits", {})
    evidence.setdefault("batches", [])

    batch_id = str(a.batch_id)
    existing_batch = next((r for r in evidence["batches"] if str(r.get("batch_id")) == batch_id), None)
    primary = (((batch.get("hypothetical_scenarios") or {}).get("36ms") or {}).get("balances") or {}).get("1.2") or {}
    if existing_batch is None:
        for r in primary.get("positions") or []:
            evidence["gambit_positions"].setdefault(key(r), dict(r))
        for r in same_window_e4(batch):
            evidence["same_window_e4_positions"].setdefault(key(r), dict(r))
        existing_batch = {
            "batch_id":batch_id,
            "commit":batch.get("commit"),
            "generated_at_epoch":batch.get("generated_at_epoch"),
            "launches":int((batch.get("capture") or {}).get("new_launches") or 0),
            "trade_events":int((batch.get("capture") or {}).get("trade_events") or 0),
            "closed_positions":int(primary.get("closed_positions") or 0),
            "net_pnl_sol":float(primary.get("net_pnl_sol") or 0),
            "reentries":int(primary.get("reentries") or 0),
            "max_concurrent_positions":int(primary.get("max_concurrent_positions") or 0),
        }
        evidence["batches"].append(existing_batch)

    if a.copy_audit:
        audit = load(Path(a.copy_audit), {})
        if str(audit.get("source_run") or batch_id) != batch_id:
            raise RuntimeError(f"copy audit source_run={audit.get('source_run')} does not match batch_id={batch_id}")
        evidence["copy_audits"][batch_id] = audit
        for r in audit.get("direct_copy_positions") or []:
            evidence["direct_copy_positions"].setdefault(key(r), dict(r))
        for r in audit.get("non_copy_positions") or []:
            evidence["non_copy_positions"].setdefault(key(r), dict(r))
        existing_batch["copy_audited"] = True
        existing_batch["direct_copy_closed_positions"] = len(audit.get("direct_copy_positions") or [])
        existing_batch["non_copy_closed_positions"] = len(audit.get("non_copy_positions") or [])
        existing_batch["direct_copy_decisions"] = int((audit.get("comparison") or {}).get("direct_copy_decisions") or 0)
        existing_batch["direct_copy_filled_candidates"] = int((audit.get("comparison") or {}).get("direct_copy_filled_candidates") or 0)

    evidence["summary"] = aggregate(evidence)
    path.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    markdown(Path(a.markdown), evidence)
    print(json.dumps(evidence["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__": raise SystemExit(main())

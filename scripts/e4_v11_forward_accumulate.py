#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any, Iterable, Mapping

E4_HISTORICAL_NET_WR = 155 / 258
E4_HISTORICAL_NET_PF = 4.92
E4_HISTORICAL_NET_POSITIONS = 258

FINGERPRINT_PATHS = (
    "src/memecoin_bot/e4_hardening_v6.py",
    "src/memecoin_bot/e4_hardening_v9.py",
    "src/memecoin_bot/e4_hardening_v10.py",
    "src/memecoin_bot/e4_pipeline_manager_v11.py",
    "src/memecoin_bot/e4_pipelines_v10.py",
    "src/memecoin_bot/e4_final.py",
    "models/e4/e4-creator-expectancy.json",
    "models/e4/e4-discovered-creators.json",
    "scripts/e4_live_market_stress.py",
    "scripts/e4_300_launch_holdout.py",
)


def load(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def fingerprint() -> str:
    digest = hashlib.sha256()
    for name in FINGERPRINT_PATHS:
        path = Path(name)
        if not path.exists():
            raise FileNotFoundError(name)
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def wilson(wins: int, total: int, z: float = 1.959963984540054) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    p = wins / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def profit_factor(rows: Iterable[Mapping[str, Any]]) -> float | None:
    rows = list(rows)
    positive = sum(float(row.get("pnl_sol") or 0.0) for row in rows if float(row.get("pnl_sol") or 0.0) > 0)
    negative = sum(float(row.get("pnl_sol") or 0.0) for row in rows if float(row.get("pnl_sol") or 0.0) < 0)
    if negative >= 0:
        return None
    return positive / abs(negative)


def same_window_e4(batch: Mapping[str, Any]) -> list[dict[str, Any]]:
    fresh = (batch.get("actual_e4_fresh_sample") or {}).get("positions") or []
    cohort = (batch.get("capture") or {}).get("cohort") or []
    if not cohort:
        return []
    starts = [int(row.get("received_ns") or 0) for row in cohort if int(row.get("received_ns") or 0) > 0]
    if not starts:
        return []
    start = min(starts) / 1e9 - 5
    tail = float((batch.get("capture") or {}).get("tail_seconds_observed") or 0.0)
    end = max(starts) / 1e9 + max(5.0, tail + 5.0)
    return [
        dict(row)
        for row in fresh
        if start <= float(row.get("entry_time") or 0.0) <= end
    ]


def position_key(row: Mapping[str, Any]) -> str:
    return f"{row.get('mint')}|{int(row.get('entry_ns') or row.get('entry_time') or 0)}"


def aggregate(evidence: dict[str, Any]) -> dict[str, Any]:
    gambit_rows = list((evidence.get("gambit_positions") or {}).values())
    e4_rows = list((evidence.get("same_window_e4_positions") or {}).values())
    wins = sum(float(row.get("pnl_sol") or 0.0) > 0 for row in gambit_rows)
    losses = len(gambit_rows) - wins
    lower, upper = wilson(wins, len(gambit_rows))
    half_width = (upper - lower) / 2 if lower is not None and upper is not None else None
    e4_wins = sum(float(row.get("pnl_sol") or 0.0) > 0 for row in e4_rows)
    e4_lower, e4_upper = wilson(e4_wins, len(e4_rows))
    loser_rows = [row for row in gambit_rows if float(row.get("pnl_sol") or 0.0) <= 0]
    batches = evidence.get("batches") or []
    invariants_ok = all(
        int(row.get("reentries") or 0) == 0 and int(row.get("max_concurrent_positions") or 0) <= 2
        for row in batches
    )
    summary = {
        "batch_count": len(batches),
        "unique_launches_observed": sum(int(row.get("launches") or 0) for row in batches),
        "gambit_closed_positions": len(gambit_rows),
        "gambit_wins": wins,
        "gambit_losses": losses,
        "gambit_net_win_rate": wins / len(gambit_rows) if gambit_rows else None,
        "gambit_wilson_95_low": lower,
        "gambit_wilson_95_high": upper,
        "gambit_wilson_half_width": half_width,
        "gambit_net_pnl_sol": sum(float(row.get("pnl_sol") or 0.0) for row in gambit_rows),
        "gambit_profit_factor": profit_factor(gambit_rows),
        "gambit_median_hold_ms": statistics.median([float(row.get("hold_ms") or 0.0) for row in gambit_rows]) if gambit_rows else None,
        "gambit_losers_within_5s": (sum(float(row.get("hold_ms") or 0.0) <= 5000 for row in loser_rows) / len(loser_rows)) if loser_rows else None,
        "gambit_entries_below_10k": (sum(float(row.get("entry_fdv_usd") or 0.0) < 10000 for row in gambit_rows) / len(gambit_rows)) if gambit_rows else None,
        "same_window_e4_closed_positions": len(e4_rows),
        "same_window_e4_wins": e4_wins,
        "same_window_e4_net_win_rate": e4_wins / len(e4_rows) if e4_rows else None,
        "same_window_e4_wilson_95_low": e4_lower,
        "same_window_e4_wilson_95_high": e4_upper,
        "same_window_e4_net_pnl_sol": sum(float(row.get("pnl_sol") or 0.0) for row in e4_rows),
        "e4_historical_net_win_rate": E4_HISTORICAL_NET_WR,
        "e4_historical_net_profit_factor": E4_HISTORICAL_NET_PF,
        "e4_historical_exact_positions": E4_HISTORICAL_NET_POSITIONS,
        "invariants_ok": invariants_ok,
    }
    sufficient = (
        len(gambit_rows) >= 100
        and len(batches) >= 3
        and half_width is not None
        and half_width <= 0.10
        and invariants_ok
    )
    summary["sufficient_evidence"] = sufficient
    if not sufficient:
        summary["classification"] = "INSUFFICIENT_EVIDENCE"
    else:
        wr = float(summary["gambit_net_win_rate"] or 0.0)
        pf = float(summary["gambit_profit_factor"] or 0.0)
        pnl = float(summary["gambit_net_pnl_sol"] or 0.0)
        if wr >= E4_HISTORICAL_NET_WR - 0.08 and pf >= 2.0 and pnl > 0:
            summary["classification"] = "E4_LIKE_FORWARD_EDGE"
        elif pf > 1.0 and pnl > 0:
            summary["classification"] = "POSITIVE_BUT_BELOW_E4"
        else:
            summary["classification"] = "FAILED_FORWARD_EDGE_CERTIFICATION"
    return summary


def write_markdown(path: Path, evidence: Mapping[str, Any]) -> None:
    s = evidence["summary"]
    pct = lambda value: "n/a" if value is None else f"{100*float(value):.2f}%"
    lines = [
        "# Gambit E4 V11 forward evidence",
        "",
        f"**Classification:** {s['classification']}",
        f"**Frozen strategy fingerprint:** `{evidence['strategy_fingerprint']}`",
        f"**Independent batches:** {s['batch_count']}",
        f"**Fresh launches observed:** {s['unique_launches_observed']}",
        f"**Gambit closed positions:** {s['gambit_closed_positions']}",
        f"**Gambit net WR:** {pct(s['gambit_net_win_rate'])}",
        f"**Gambit 95% Wilson CI:** {pct(s['gambit_wilson_95_low'])} – {pct(s['gambit_wilson_95_high'])}",
        f"**Gambit net P&L:** {s['gambit_net_pnl_sol']:.6f} SOL",
        f"**Gambit PF:** {s['gambit_profit_factor']}",
        f"**Same-window E4 closed positions:** {s['same_window_e4_closed_positions']}",
        f"**Same-window E4 net WR:** {pct(s['same_window_e4_net_win_rate'])}",
        f"**Same-window E4 net P&L:** {s['same_window_e4_net_pnl_sol']:.6f} SOL",
        f"**Historical exact E4 benchmark:** {pct(s['e4_historical_net_win_rate'])}, PF ~{s['e4_historical_net_profit_factor']}",
        f"**Evidence sufficient:** {'YES' if s['sufficient_evidence'] else 'NO'}",
        "",
        "The result is not considered complete until at least 100 closed Gambit positions have been observed across at least three independent live batches, the 95% Wilson half-width is <=10 percentage points, and reentry/concurrency invariants remain intact.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", required=True)
    parser.add_argument("--evidence", default="models/e4/e4-v11-forward-evidence.json")
    parser.add_argument("--markdown", default="models/e4/e4-v11-forward-evidence.md")
    parser.add_argument("--batch-id", required=True)
    args = parser.parse_args()

    batch = load(Path(args.batch), {})
    current_fp = fingerprint()
    evidence_path = Path(args.evidence)
    evidence = load(evidence_path, None)
    if evidence is None:
        evidence = {
            "version": "e4-v11-forward-evidence-v1",
            "strategy_fingerprint": current_fp,
            "fingerprint_paths": list(FINGERPRINT_PATHS),
            "batches": [],
            "gambit_positions": {},
            "same_window_e4_positions": {},
        }
    elif evidence.get("strategy_fingerprint") != current_fp:
        raise RuntimeError(
            "frozen strategy fingerprint changed; refusing to mix forward evidence "
            f"old={evidence.get('strategy_fingerprint')} new={current_fp}"
        )

    if any(str(row.get("batch_id")) == str(args.batch_id) for row in evidence["batches"]):
        print("batch already accumulated")
        return 0

    primary = (((batch.get("hypothetical_scenarios") or {}).get("36ms") or {}).get("balances") or {}).get("1.2") or {}
    positions = primary.get("positions") or []
    for row in positions:
        evidence["gambit_positions"].setdefault(position_key(row), dict(row))
    for row in same_window_e4(batch):
        evidence["same_window_e4_positions"].setdefault(position_key(row), dict(row))

    evidence["batches"].append({
        "batch_id": str(args.batch_id),
        "commit": batch.get("commit"),
        "generated_at_epoch": batch.get("generated_at_epoch"),
        "launches": int((batch.get("capture") or {}).get("new_launches") or 0),
        "trade_events": int((batch.get("capture") or {}).get("trade_events") or 0),
        "closed_positions": int(primary.get("closed_positions") or 0),
        "net_pnl_sol": float(primary.get("net_pnl_sol") or 0.0),
        "reentries": int(primary.get("reentries") or 0),
        "max_concurrent_positions": int(primary.get("max_concurrent_positions") or 0),
    })
    evidence["summary"] = aggregate(evidence)
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    write_markdown(Path(args.markdown), evidence)
    print(json.dumps(evidence["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

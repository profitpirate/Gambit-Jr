#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping


def finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number and abs(number) != float("inf") else default


def trades(evidence: Mapping[str, Any], batch_ids: list[str]) -> list[dict[str, Any]]:
    audits = evidence.get("copy_audits") or {}
    rows: list[dict[str, Any]] = []
    for batch_id in batch_ids:
        audit = audits.get(str(batch_id)) or {}
        for row in audit.get("direct_copy_trades") or []:
            if row.get("v12_closed"):
                item = dict(row)
                item["batch_id"] = str(batch_id)
                rows.append(item)
    return rows


def profit_factor(rows: list[Mapping[str, Any]]) -> float | None:
    positive = sum(finite(row.get("v12_pnl_sol")) for row in rows if finite(row.get("v12_pnl_sol")) > 0)
    negative = sum(finite(row.get("v12_pnl_sol")) for row in rows if finite(row.get("v12_pnl_sol")) < 0)
    return positive / abs(negative) if negative < 0 else None


def evaluate(rows: list[Mapping[str, Any]], cap_bps: float) -> dict[str, Any]:
    kept = [row for row in rows if finite(row.get("fill_drift_bps"), 1e12) <= cap_bps]
    wins = [row for row in kept if bool(row.get("v12_won"))]
    e4_winners = [row for row in rows if bool(row.get("e4_won"))]
    kept_e4_winners = [row for row in kept if bool(row.get("e4_won"))]
    return {
        "drift_cap_bps": cap_bps,
        "source_trades": len(rows),
        "kept": len(kept),
        "skipped": len(rows) - len(kept),
        "wins": len(wins),
        "win_rate": len(wins) / len(kept) if kept else 0.0,
        "net_pnl_sol": sum(finite(row.get("v12_pnl_sol")) for row in kept),
        "profit_factor": profit_factor(kept),
        "e4_winner_capture": len(kept_e4_winners) / len(e4_winners) if e4_winners else 0.0,
        "e4_trade_capture": len(kept) / len(rows) if rows else 0.0,
    }


def objective(m: Mapping[str, Any]) -> tuple[float, float, float, float]:
    # Avoid tiny cherry-picked cohorts: at least 25% of direct-copy trades and
    # at least 3 retained positions on training evidence.
    valid = 1.0 if finite(m.get("e4_trade_capture")) >= 0.25 and int(m.get("kept") or 0) >= 3 else 0.0
    pnl = finite(m.get("net_pnl_sol"))
    wr = finite(m.get("win_rate"))
    winner_capture = finite(m.get("e4_winner_capture"))
    return (valid, 1.0 if pnl > 0 else 0.0, wr, winner_capture + min(0.0, pnl))


def main() -> int:
    parser = argparse.ArgumentParser(description="Learn causal V12 post-E4 drift gate")
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--train-batches", required=True)
    parser.add_argument("--holdout-batches", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    train_rows = trades(evidence, [x for x in args.train_batches.split(",") if x])
    holdout_rows = trades(evidence, [x for x in args.holdout_batches.split(",") if x])
    caps = [100, 250, 500, 750, 1000, 1250, 1500, 1750, 2000, 2500, 3000]
    ranked = [(objective(evaluate(train_rows, cap)), cap, evaluate(train_rows, cap)) for cap in caps]
    ranked.sort(key=lambda row: row[0], reverse=True)
    _, cap, train = ranked[0]
    holdout = evaluate(holdout_rows, cap)
    safe = bool(
        holdout["kept"] >= 3
        and holdout["net_pnl_sol"] > 0
        and holdout["win_rate"] >= 0.50
    )
    payload = {
        "version": "e4-v12-economic-parity-gate-v1",
        "drift_cap_bps": cap,
        "train": train,
        "holdout": holdout,
        "safe_to_authorize": safe,
        "all_caps_train": [evaluate(train_rows, x) for x in caps],
        "all_caps_holdout": [evaluate(holdout_rows, x) for x in caps],
    }
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

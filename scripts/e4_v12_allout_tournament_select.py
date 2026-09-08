#!/usr/bin/env python3
"""Aggregate independent all-out hazard lanes without weakening the golden gate."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Mapping


def finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def rank_key(row: Mapping[str, Any]) -> tuple[float, ...]:
    holdout = ((row.get("winner") or {}).get("holdout") or {})
    return (
        1.0 if row.get("status") == "HISTORICAL_CANDIDATE" else 0.0,
        finite(holdout.get("minimum_win_rate")),
        finite(holdout.get("minimum_wilson_lower")),
        min(20.0, finite(holdout.get("minimum_profit_factor"))),
        finite(holdout.get("minimum_net_pnl_sol"), -999.0),
        -finite(holdout.get("maximum_drawdown_fraction"), 1.0),
        finite(holdout.get("minimum_trades")),
    )


def compact_lane(report: Mapping[str, Any]) -> dict[str, Any]:
    winner = report.get("winner") or {}
    holdout = winner.get("holdout") or {}
    validation = winner.get("validation") or {}
    return {
        "lane": report.get("lane"),
        "experiment_id": report.get("experiment_id"),
        "status": report.get("status"),
        "policy": winner.get("policy"),
        "threshold": winner.get("threshold"),
        "formula": winner.get("formula"),
        "historical_pass": winner.get("historical_pass", False),
        "failed_requirements": winner.get("failed_requirements") or [],
        "validation": {
            key: value for key, value in validation.items() if key != "latencies"
        },
        "holdout": {
            key: value for key, value in holdout.items() if key != "latencies"
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--status-output", type=Path, required=True)
    args = parser.parse_args()

    reports = []
    errors = []
    for path in sorted(args.input.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append({"path": str(path), "error": str(exc)})
            continue
        if payload.get("version") != "e4-v12-allout-profit-hazard-v1":
            continue
        reports.append(payload)
    if not reports:
        raise SystemExit("no all-out lane reports were found")

    unique = {}
    for report in reports:
        experiment_id = str(report.get("experiment_id") or "")
        if not experiment_id:
            errors.append({"lane": report.get("lane"), "error": "missing experiment_id"})
            continue
        unique.setdefault(experiment_id, report)
    ranked = sorted(unique.values(), key=rank_key, reverse=True)
    finalists = [row for row in ranked if row.get("status") == "HISTORICAL_CANDIDATE"]
    status = "HISTORICAL_CANDIDATE_FOUND" if finalists else "NOT_CONCLUSIVE"
    output = {
        "version": "e4-v12-allout-profit-hazard-tournament-v1",
        "status": status,
        "lane_reports_discovered": len(reports),
        "unique_experiments": len(unique),
        "historical_candidates": len(finalists),
        "winner": compact_lane(finalists[0] if finalists else ranked[0]),
        "leaderboard": [compact_lane(row) for row in ranked],
        "errors": errors,
        "interpretation": (
            "A historical candidate is a discovery result only. It must be frozen and "
            "tested on new post-freeze live windows before any golden-thesis claim."
        ),
        "production_paths_changed": 0,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.status_output.write_text(status + "\n", encoding="utf-8")
    print(json.dumps({key: output[key] for key in ("status", "unique_experiments", "historical_candidates", "winner")}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

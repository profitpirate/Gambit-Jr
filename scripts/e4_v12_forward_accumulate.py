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

# V12 is the permanent product version. Certification uses the exact observed
# E4 role-model benchmarks: no version bump, tolerance discount or smaller
# sample may be substituted for these targets.
V12_ROLE_MODEL_MIN_WR = E4_HISTORICAL_NET_WR
V12_ROLE_MODEL_MIN_PF = E4_HISTORICAL_NET_PF
V12_ROLE_MODEL_MIN_POSITIONS = E4_HISTORICAL_NET_POSITIONS
V12_ROLE_MODEL_MIN_BATCHES = 3
V12_ROLE_MODEL_MAX_WILSON_HALF_WIDTH = 0.10

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
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def wilson(wins: int, total: int, z: float = 1.959963984540054) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    p = wins / total
    d = 1 + z * z / total
    c = (p + z * z / (2 * total)) / d
    m = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / d
    return max(0.0, c - m), min(1.0, c + m)


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
    start = min(starts) / 1e9 - 5
    tail = float((batch.get("capture") or {}).get("tail_seconds_observed") or 0)
    end = max(starts) / 1e9 + max(5.0, tail + 5.0)
    return [dict(r) for r in fresh if start <= float(r.get("entry_time") or 0) <= end]


def role_model_checks(summary: Mapping[str, Any]) -> dict[str, bool]:
    closed = int(summary.get("gambit_closed_positions") or 0)
    win_rate = float(summary.get("gambit_net_win_rate") or 0.0)
    profit_factor_value = float(summary.get("gambit_profit_factor") or 0.0)
    pnl = float(summary.get("gambit_net_pnl_sol") or 0.0)
    batches = int(summary.get("batch_count") or 0)
    half_width = summary.get("gambit_wilson_half_width")
    return {
        "closed_positions": closed >= V12_ROLE_MODEL_MIN_POSITIONS,
        "win_rate": win_rate >= V12_ROLE_MODEL_MIN_WR,
        "profit_factor": profit_factor_value >= V12_ROLE_MODEL_MIN_PF,
        "positive_net_pnl": pnl > 0.0,
        "independent_batches": batches >= V12_ROLE_MODEL_MIN_BATCHES,
        "wilson_precision": (
            half_width is not None
            and float(half_width) <= V12_ROLE_MODEL_MAX_WILSON_HALF_WIDTH
        ),
        "invariants": bool(summary.get("invariants_ok")),
    }


def role_model_failures(summary: Mapping[str, Any]) -> list[str]:
    checks = summary.get("role_model_checks")
    if not isinstance(checks, Mapping):
        checks = role_model_checks(summary)
    return [name for name, passed in checks.items() if not bool(passed)]


def aggregate(evidence: Mapping[str, Any]) -> dict[str, Any]:
    gambit = list((evidence.get("gambit_positions") or {}).values())
    e4 = list((evidence.get("same_window_e4_positions") or {}).values())
    batches = list(evidence.get("batches") or [])
    launches = sum(int(r.get("launches") or 0) for r in batches)
    wins = sum(float(r.get("pnl_sol") or 0) > 0 for r in gambit)
    e4_wins = sum(float(r.get("pnl_sol") or 0) > 0 for r in e4)
    lo, hi = wilson(wins, len(gambit))
    elo, ehi = wilson(e4_wins, len(e4))
    half = (hi - lo) / 2 if lo is not None and hi is not None else None
    losers = [r for r in gambit if float(r.get("pnl_sol") or 0) <= 0]
    invariants = all(
        int(r.get("reentries") or 0) == 0
        and int(r.get("max_concurrent_positions") or 0) <= 2
        for r in batches
    )
    target_wins = math.ceil(V12_ROLE_MODEL_MIN_WR * V12_ROLE_MODEL_MIN_POSITIONS)
    current_required_wins = math.ceil(V12_ROLE_MODEL_MIN_WR * len(gambit)) if gambit else 0
    summary = {
        "batch_count": len(batches),
        "unique_launches_observed": launches,
        "gambit_closed_positions": len(gambit),
        "gambit_wins": wins,
        "gambit_losses": len(gambit) - wins,
        "gambit_net_win_rate": wins / len(gambit) if gambit else None,
        "gambit_wilson_95_low": lo,
        "gambit_wilson_95_high": hi,
        "gambit_wilson_half_width": half,
        "gambit_net_pnl_sol": sum(float(r.get("pnl_sol") or 0) for r in gambit),
        "gambit_profit_factor": profit_factor(gambit),
        "gambit_trade_rate_per_launch": len(gambit) / launches if launches else None,
        "gambit_median_hold_ms": (
            statistics.median([float(r.get("hold_ms") or 0) for r in gambit])
            if gambit
            else None
        ),
        "gambit_median_entry_fdv_usd": (
            statistics.median([float(r.get("entry_fdv_usd") or 0) for r in gambit])
            if gambit
            else None
        ),
        "gambit_losers_within_5s": (
            sum(float(r.get("hold_ms") or 0) <= 5000 for r in losers) / len(losers)
            if losers
            else None
        ),
        "same_window_e4_closed_positions": len(e4),
        "same_window_e4_wins": e4_wins,
        "same_window_e4_net_win_rate": e4_wins / len(e4) if e4 else None,
        "same_window_e4_wilson_95_low": elo,
        "same_window_e4_wilson_95_high": ehi,
        "same_window_e4_net_pnl_sol": sum(float(r.get("pnl_sol") or 0) for r in e4),
        "same_window_e4_trade_rate_per_launch": len(e4) / launches if launches else None,
        "e4_historical_net_win_rate": E4_HISTORICAL_NET_WR,
        "e4_historical_net_profit_factor": E4_HISTORICAL_NET_PF,
        "e4_historical_exact_positions": E4_HISTORICAL_NET_POSITIONS,
        "role_model_target_win_rate": V12_ROLE_MODEL_MIN_WR,
        "role_model_target_profit_factor": V12_ROLE_MODEL_MIN_PF,
        "role_model_target_closed_positions": V12_ROLE_MODEL_MIN_POSITIONS,
        "role_model_target_wins": target_wins,
        "role_model_required_wins_at_current_sample": current_required_wins,
        "role_model_remaining_positions": max(0, V12_ROLE_MODEL_MIN_POSITIONS - len(gambit)),
        "role_model_remaining_wins": max(0, target_wins - wins),
        "invariants_ok": invariants,
    }
    checks = role_model_checks(summary)
    summary["role_model_checks"] = checks
    summary["role_model_targets_met"] = all(checks.values())
    sufficient = (
        checks["closed_positions"]
        and checks["independent_batches"]
        and checks["wilson_precision"]
        and checks["invariants"]
    )
    summary["sufficient_evidence"] = sufficient
    if not sufficient:
        summary["classification"] = "INSUFFICIENT_EVIDENCE"
    elif summary["role_model_targets_met"]:
        summary["classification"] = "E4_ROLE_MODEL_TARGETS_MET"
    else:
        summary["classification"] = "FAILED_E4_ROLE_MODEL_TARGETS"
    return summary


def markdown(path: Path, evidence: Mapping[str, Any]) -> None:
    s = evidence["summary"]

    def pct(value: Any) -> str:
        return "n/a" if value is None else f"{100 * float(value):.2f}%"

    def number(value: Any) -> str:
        return "n/a" if value is None else f"{float(value):.6f}"

    checks = s.get("role_model_checks") or {}
    check_text = ", ".join(
        f"{name}={'PASS' if passed else 'FAIL'}" for name, passed in checks.items()
    )
    path.write_text(
        "\n".join(
            [
                "# Gambit E4 V12 forward evidence",
                "",
                f"**Classification:** {s['classification']}",
                f"**Frozen strategy fingerprint:** `{evidence['strategy_fingerprint']}`",
                f"**Independent batches:** {s['batch_count']}",
                f"**Fresh launches:** {s['unique_launches_observed']}",
                f"**Gambit closed:** {s['gambit_closed_positions']} / {V12_ROLE_MODEL_MIN_POSITIONS}",
                f"**Gambit wins:** {s['gambit_wins']} / {s['role_model_target_wins']} target wins",
                f"**Gambit WR:** {pct(s['gambit_net_win_rate'])}",
                f"**Gambit 95% CI:** {pct(s['gambit_wilson_95_low'])} – {pct(s['gambit_wilson_95_high'])}",
                f"**Gambit P&L:** {s['gambit_net_pnl_sol']:.6f} SOL",
                f"**Gambit PF:** {number(s['gambit_profit_factor'])}",
                f"**Gambit selection rate:** {pct(s['gambit_trade_rate_per_launch'])}",
                f"**Same-window E4 closed:** {s['same_window_e4_closed_positions']}",
                f"**Same-window E4 WR:** {pct(s['same_window_e4_net_win_rate'])}",
                f"**Same-window E4 P&L:** {s['same_window_e4_net_pnl_sol']:.6f} SOL",
                f"**Same-window E4 selection rate:** {pct(s['same_window_e4_trade_rate_per_launch'])}",
                f"**Exact E4 role-model target:** {pct(V12_ROLE_MODEL_MIN_WR)}, PF {V12_ROLE_MODEL_MIN_PF}, {V12_ROLE_MODEL_MIN_POSITIONS} closed positions",
                f"**Role-model gate:** {'PASS' if s['role_model_targets_met'] else 'FAIL'}",
                f"**Target checks:** {check_text}",
                f"**Evidence sufficient:** {'YES' if s['sufficient_evidence'] else 'NO'}",
                "",
                "V12 is permanent. Evidence may continue accumulating under V12, but certification only passes when every exact E4 role-model target is met: at least 258 closed positions, at least 60.08% net win rate, profit factor at least 4.92, positive net P&L, at least three independent batches, Wilson half-width no greater than 10 percentage points, and intact reentry/concurrency invariants.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--evidence", default="models/e4/e4-v12-forward-evidence.json")
    parser.add_argument("--markdown", default="models/e4/e4-v12-forward-evidence.md")
    args = parser.parse_args()
    batch = load(Path(args.batch), {})
    current_fingerprint = fingerprint()
    path = Path(args.evidence)
    evidence = load(path, None)
    if evidence is None:
        evidence = {
            "version": "e4-v12-forward-evidence-v1",
            "strategy_fingerprint": current_fingerprint,
            "fingerprint_paths": list(FINGERPRINT_PATHS),
            "batches": [],
            "gambit_positions": {},
            "same_window_e4_positions": {},
        }
    elif evidence.get("strategy_fingerprint") != current_fingerprint:
        raise RuntimeError(
            "V12 strategy fingerprint changed "
            f"old={evidence.get('strategy_fingerprint')} new={current_fingerprint}"
        )
    if any(str(r.get("batch_id")) == str(args.batch_id) for r in evidence["batches"]):
        return 0
    primary = (
        (((batch.get("hypothetical_scenarios") or {}).get("36ms") or {}).get("balances") or {})
        .get("1.2")
        or {}
    )
    for row in primary.get("positions") or []:
        evidence["gambit_positions"].setdefault(key(row), dict(row))
    for row in same_window_e4(batch):
        evidence["same_window_e4_positions"].setdefault(key(row), dict(row))
    evidence["batches"].append(
        {
            "batch_id": str(args.batch_id),
            "commit": batch.get("commit"),
            "generated_at_epoch": batch.get("generated_at_epoch"),
            "launches": int((batch.get("capture") or {}).get("new_launches") or 0),
            "trade_events": int((batch.get("capture") or {}).get("trade_events") or 0),
            "closed_positions": int(primary.get("closed_positions") or 0),
            "net_pnl_sol": float(primary.get("net_pnl_sol") or 0),
            "reentries": int(primary.get("reentries") or 0),
            "max_concurrent_positions": int(primary.get("max_concurrent_positions") or 0),
        }
    )
    evidence["summary"] = aggregate(evidence)
    path.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    markdown(Path(args.markdown), evidence)
    print(json.dumps(evidence["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping


def finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def flatten(value: Any, path: str = ""):
    if isinstance(value, Mapping):
        yield path, value
        for key, nested in value.items():
            child = f"{path}.{key}" if path else str(key)
            yield from flatten(nested, child)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from flatten(nested, f"{path}[{index}]")


def lower_map(row: Mapping[str, Any]) -> dict[str, tuple[str, Any]]:
    return {str(key).lower(): (str(key), value) for key, value in row.items()}


def first_number(row: Mapping[str, Any], aliases: tuple[str, ...]) -> float | None:
    lowered = lower_map(row)
    for alias in aliases:
        pair = lowered.get(alias)
        if pair:
            number = finite(pair[1])
            if number is not None:
                return number
    for key, value in row.items():
        low = str(key).lower()
        if any(alias in low for alias in aliases):
            number = finite(value)
            if number is not None:
                return number
    return None


def candidate(path: str, row: Mapping[str, Any]) -> dict[str, Any] | None:
    closed = first_number(row, ("closed_positions", "closed_trades", "positions_closed", "trades"))
    net_wr = first_number(row, ("net_win_rate", "win_rate_net", "net_wr"))
    gross_wr = first_number(row, ("gross_win_rate", "win_rate_gross", "gross_wr"))
    generic_wr = first_number(row, ("win_rate",))
    pnl = first_number(row, ("net_pnl_sol", "net_profit_sol", "net_pnl", "pnl_sol", "pnl"))
    pf = first_number(row, ("profit_factor", "net_profit_factor"))
    ending = first_number(row, ("ending_balance", "final_balance", "balance_after"))
    starting = first_number(row, ("starting_balance", "initial_balance", "balance_before"))
    if net_wr is None:
        net_wr = generic_wr
    if closed is None or net_wr is None:
        return None
    if net_wr > 1.0 and net_wr <= 100.0:
        net_wr /= 100.0
    if gross_wr is not None and gross_wr > 1.0 and gross_wr <= 100.0:
        gross_wr /= 100.0
    return {
        "path": path,
        "closed_trades": int(round(closed)),
        "net_win_rate": net_wr,
        "gross_win_rate": gross_wr,
        "net_pnl_sol": pnl,
        "profit_factor": pf,
        "starting_balance_sol": starting,
        "ending_balance_sol": ending,
    }


def score_gambit(row: Mapping[str, Any]) -> float:
    path = str(row.get("path") or "").lower()
    score = 0.0
    if any(word in path for word in ("gambit", "scenario", "simulation", "hypothetical", "replay")):
        score += 20
    if any(word in path for word in ("500ms", "500_ms", "delay_500", "latency_500", "500")):
        score += 14
    if any(word in path for word in ("1.2", "1_2", "wallet_1", "balance_1")):
        score += 10
    if any(word in path for word in ("e4", "actual", "oracle", "benchmark")):
        score -= 30
    if row.get("net_pnl_sol") is not None:
        score += 5
    if row.get("profit_factor") is not None:
        score += 5
    if row.get("closed_trades", 0) > 0:
        score += 3
    return score


def score_e4(row: Mapping[str, Any]) -> float:
    path = str(row.get("path") or "").lower()
    score = 0.0
    if "e4" in path:
        score += 20
    if any(word in path for word in ("actual", "oracle", "benchmark", "fresh")):
        score += 14
    if any(word in path for word in ("gambit", "scenario", "hypothetical")):
        score -= 20
    if row.get("net_pnl_sol") is not None:
        score += 4
    if row.get("profit_factor") is not None:
        score += 4
    if row.get("closed_trades", 0) > 0:
        score += 3
    return score


def scalar_paths(value: Any, path: str = ""):
    if isinstance(value, Mapping):
        for key, nested in value.items():
            child = f"{path}.{key}" if path else str(key)
            yield from scalar_paths(nested, child)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from scalar_paths(nested, f"{path}[{index}]")
    else:
        yield path, value


def launch_count(data: Mapping[str, Any]) -> int | None:
    possibilities: list[tuple[int, str]] = []
    for path, value in scalar_paths(data):
        low = path.lower()
        if not any(term in low for term in ("unique_launches", "captured_launches", "target_launches", "launches_captured")):
            continue
        number = finite(value)
        if number is not None and 0 < number < 1_000_000:
            possibilities.append((int(round(number)), path))
    if not possibilities:
        return None
    # Prefer actual/unique captured counts over configured target counts.
    possibilities.sort(key=lambda pair: ("target" in pair[1].lower(), -pair[0]))
    return possibilities[0][0]


def write_markers(root: Path, canonical: Mapping[str, Any]) -> None:
    marker = root / "canonical-markers"
    if marker.exists():
        for path in marker.iterdir():
            if path.is_file():
                path.unlink()
    marker.mkdir(parents=True, exist_ok=True)

    def mark(name: str, value: Any = 1) -> None:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
        (marker / safe).write_text(str(value) + "\n", encoding="utf-8")

    gambit = canonical.get("gambit") or {}
    e4 = canonical.get("actual_e4") or {}
    for prefix, row in (("GAMBIT", gambit), ("E4", e4)):
        for key, value in row.items():
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                mark(f"{prefix}_{key}_ROUNDED_1E6_{round(float(value) * 1_000_000)}", value)
    launches = canonical.get("live_launches")
    if launches is not None:
        mark(f"LIVE_LAUNCHES_{int(launches)}", launches)

    wr = finite(gambit.get("net_win_rate"))
    if wr is not None:
        for percent in range(0, 101):
            if wr * 100 >= percent:
                mark(f"GAMBIT_WR_GE_{percent}PCT")
        for percent in range(0, 101):
            if wr * 100 <= percent:
                mark(f"GAMBIT_WR_LE_{percent}PCT")
    closed = int(gambit.get("closed_trades") or 0)
    for threshold in range(0, 101):
        if closed >= threshold:
            mark(f"GAMBIT_TRADES_GE_{threshold}")
    pnl = finite(gambit.get("net_pnl_sol"))
    if pnl is not None:
        mark("GAMBIT_PNL_POSITIVE" if pnl > 0 else "GAMBIT_PNL_NONPOSITIVE")
        for tenth in range(-50, 51):
            threshold = tenth / 10
            if pnl >= threshold:
                mark(f"GAMBIT_PNL_GE_{str(threshold).replace('-', 'M').replace('.', 'P')}")
    pf = finite(gambit.get("profit_factor"))
    if pf is not None:
        for tenth in range(0, 101):
            threshold = tenth / 10
            if pf >= threshold:
                mark(f"GAMBIT_PF_GE_{str(threshold).replace('.', 'P')}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = json.loads(args.input.read_text(encoding="utf-8"))
    rows = [result for path, value in flatten(data) if isinstance(value, Mapping) and (result := candidate(path, value))]
    gambit_rows = sorted(rows, key=score_gambit, reverse=True)
    e4_rows = sorted(rows, key=score_e4, reverse=True)
    gambit = gambit_rows[0] if gambit_rows and score_gambit(gambit_rows[0]) > 0 else None
    actual_e4 = e4_rows[0] if e4_rows and score_e4(e4_rows[0]) > 0 else None
    speed = ((data.get("e4_v10") or {}).get("speed_certification") or {}) if isinstance(data, Mapping) else {}
    canonical = {
        "version": "e4-v10-canonical-result-v1",
        "live_launches": launch_count(data),
        "gambit": gambit,
        "actual_e4": actual_e4,
        "historical_e4_net_benchmark": {"net_win_rate": 0.6008, "profit_factor": 4.92},
        "speed": speed,
        "candidate_count": len(rows),
        "top_gambit_candidates": gambit_rows[:10],
        "top_e4_candidates": e4_rows[:10],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(canonical, indent=2), encoding="utf-8")
    write_markers(args.output.parent, canonical)
    print(json.dumps(canonical, separators=(",", ":")))
    return 0 if gambit is not None else 2


if __name__ == "__main__":
    raise SystemExit(main())

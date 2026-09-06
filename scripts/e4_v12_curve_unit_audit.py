#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts import e4_v12_true_latency_replay as replay


def finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _normal_sol_amount(value: Any) -> float:
    amount = finite(value)
    return amount / replay.LAMPORTS if amount >= 1_000_000 else amount


def _normal_token_amount(value: Any) -> float:
    amount = finite(value)
    return amount / replay.TOKEN_SCALE if amount >= 10_000_000_000 else amount


def _load_groups(path: Path) -> tuple[dict[str, list[dict[str, Any]]], int]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    parse_errors = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            if not isinstance(row, Mapping):
                parse_errors += 1
                continue
            mint = str(row.get("mint") or "")
            if mint:
                grouped.setdefault(mint, []).append(dict(row))
    for rows in grouped.values():
        rows.sort(key=replay.event_sort_key)
        for sequence, row in enumerate(rows):
            row["__sequence"] = sequence
    return grouped, parse_errors


def _reserve_states(rows: Sequence[Mapping[str, Any]]) -> list[replay.ReserveState]:
    states: list[replay.ReserveState] = []
    for sequence, row in enumerate(rows):
        state = replay.reserve_from_row(row, sequence)
        if state is not None:
            states.append(state)
    return states


def _same_event_or_latest_state(
    states: Sequence[replay.ReserveState],
    event: Mapping[str, Any],
) -> replay.ReserveState | None:
    timestamp = replay.integer(event.get("received_ns"))
    sequence = replay.integer(event.get("__sequence"), -1)
    exact = next((state for state in states if state.sequence == sequence), None)
    if exact is not None:
        return exact
    candidate = replay.state_at_or_before(states, timestamp, sequence)
    if candidate is not None and candidate.received_ns == timestamp:
        return candidate
    return None


def audit_events(paths: Sequence[Path]) -> dict[str, Any]:
    total = 0
    reversible = 0
    ratios: list[float] = []
    errors_bps: list[float] = []
    examples: list[dict[str, Any]] = []
    parse_errors = 0
    mint_count = 0

    for path in paths:
        grouped, file_parse_errors = _load_groups(path)
        parse_errors += file_parse_errors
        mint_count += len(grouped)
        for mint, rows in grouped.items():
            states = _reserve_states(rows)
            for event in rows:
                if str(event.get("trader") or "") != replay.E4_WALLET:
                    continue
                if str(event.get("kind") or "").upper() not in replay.BUY_KINDS:
                    continue
                total += 1
                timestamp = replay.integer(event.get("received_ns"))
                post = _same_event_or_latest_state(states, event)
                source_sol = max(0.0, _normal_sol_amount(event.get("sol_amount")))
                source_tokens = max(0.0, _normal_token_amount(event.get("token_amount")))
                if post is None or source_sol <= 0 or source_tokens <= 0:
                    continue

                # Captured Pump events expose post-trade reserves. Reverse the
                # observed E4 exchange into the causal pre-buy state and quote
                # the same input again. A correct unit interpretation should
                # reproduce E4's observed token amount to within a tight band.
                pre_sol = post.virtual_sol - source_sol
                pre_tokens = post.virtual_tokens + source_tokens
                pre_real = (
                    post.real_tokens + source_tokens
                    if math.isfinite(post.real_tokens)
                    else float("inf")
                )
                if pre_sol <= 0 or pre_tokens <= 0:
                    continue
                pre_state = replay.ReserveState(
                    received_ns=timestamp,
                    sequence=replay.integer(event.get("__sequence"), 0),
                    virtual_sol=pre_sol,
                    virtual_tokens=pre_tokens,
                    real_tokens=pre_real,
                    price_sol=post.price_sol,
                    fdv_usd=post.fdv_usd,
                )
                reconstructed = replay.buy_tokens(source_sol, pre_state)
                if reconstructed <= 0:
                    continue

                reversible += 1
                ratio = reconstructed / source_tokens
                ratios.append(ratio)
                error_bps = abs(ratio - 1.0) * 10_000.0
                errors_bps.append(error_bps)
                if len(examples) < 20:
                    examples.append(
                        {
                            "mint": mint,
                            "source_sol": source_sol,
                            "source_tokens": source_tokens,
                            "reconstructed_tokens": reconstructed,
                            "ratio": ratio,
                            "absolute_error_bps": error_bps,
                            "post_virtual_sol": post.virtual_sol,
                            "post_virtual_tokens": post.virtual_tokens,
                        }
                    )

    within_100_bps = sum(error <= 100.0 for error in errors_bps)
    within_500_bps = sum(error <= 500.0 for error in errors_bps)
    result = {
        "version": "e4-v12-curve-unit-audit-v2",
        "input_files": len(paths),
        "mints_scanned": mint_count,
        "parse_errors": parse_errors,
        "total_e4_buys": total,
        "reversible_buys": reversible,
        "coverage": reversible / total if total else 0.0,
        "median_ratio": statistics.median(ratios) if ratios else None,
        "median_absolute_error_bps": statistics.median(errors_bps) if errors_bps else None,
        "p95_absolute_error_bps": (
            sorted(errors_bps)[min(len(errors_bps) - 1, round((len(errors_bps) - 1) * 0.95))]
            if errors_bps
            else None
        ),
        "within_100_bps_fraction": within_100_bps / reversible if reversible else 0.0,
        "within_500_bps_fraction": within_500_bps / reversible if reversible else 0.0,
        "examples": examples,
    }
    result["passed"] = bool(
        total >= 20
        and result["coverage"] >= 0.80
        and result["median_ratio"] is not None
        and 0.90 <= float(result["median_ratio"]) <= 1.10
        and result["within_500_bps_fraction"] >= 0.75
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate event/reserve units using E4's observed buy exchange"
    )
    parser.add_argument("--events", action="append", type=Path, default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit_events(args.events)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(
        json.dumps(
            {key: result[key] for key in result if key != "examples"},
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

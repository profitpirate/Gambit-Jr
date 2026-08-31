#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping


def flatten(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    rows: list[tuple[str, Any]] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            rows.extend(flatten(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            rows.extend(flatten(item, f"{prefix}[{index}]"))
    else:
        rows.append((prefix.lower(), value))
    return rows


def number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def find_metric(
    rows: Iterable[tuple[str, Any]],
    *,
    include_all: tuple[str, ...],
    include_any: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
) -> tuple[str, float] | None:
    candidates: list[tuple[int, str, float]] = []
    for path, value in rows:
        numeric = number(value)
        if numeric is None:
            continue
        if any(term not in path for term in include_all):
            continue
        if include_any and not any(term in path for term in include_any):
            continue
        if any(term in path for term in exclude):
            continue
        score = sum(path.endswith(term) for term in include_all + include_any)
        candidates.append((score, path, numeric))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
    _, path, value = candidates[0]
    return path, value


def first_metric(rows: list[tuple[str, Any]], specifications: list[dict[str, tuple[str, ...]]]) -> tuple[str, float] | None:
    for spec in specifications:
        found = find_metric(rows, **spec)
        if found is not None:
            return found
    return None


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must contain a JSON object")
    return dict(value)


def extract_holdout(path: Path) -> dict[str, Any]:
    payload = load(path)
    rows = flatten(payload)

    builder_p50 = first_metric(
        rows,
        [
            {"include_all": ("builder", "median"), "include_any": ("ms", "millisecond"), "exclude": ("sign", "route")},
            {"include_all": ("transaction", "build", "p50"), "include_any": ("ms",)},
        ],
    )
    builder_p95 = first_metric(
        rows,
        [
            {"include_all": ("builder", "p95"), "include_any": ("ms", "millisecond"), "exclude": ("sign", "route")},
            {"include_all": ("build", "p95"), "include_any": ("ms",), "exclude": ("sign", "route")},
        ],
    )
    signing_p95 = first_metric(
        rows,
        [
            {"include_all": ("sign", "p95"), "include_any": ("ms", "millisecond")},
            {"include_all": ("signing", "p95")},
        ],
    )
    route_p95 = first_metric(
        rows,
        [
            {"include_all": ("route", "p95"), "include_any": ("ack", "submit", "latency", "ms")},
            {"include_all": ("landing", "p95"), "include_any": ("ms",)},
        ],
    )
    builder_success = first_metric(
        rows,
        [
            {"include_all": ("builder", "success"), "include_any": ("rate", "ratio")},
            {"include_all": ("build", "success"), "include_any": ("rate", "ratio")},
        ],
    )
    gambit_win_rate = first_metric(
        rows,
        [
            {"include_all": ("gambit", "net", "win", "rate")},
            {"include_all": ("simulation", "net", "win", "rate")},
            {"include_all": ("net", "win", "rate"), "exclude": ("e4", "actual")},
        ],
    )
    gambit_trades = first_metric(
        rows,
        [
            {"include_all": ("gambit", "closed")},
            {"include_all": ("simulation", "closed")},
            {"include_all": ("closed", "positions"), "exclude": ("e4", "actual")},
        ],
    )
    gambit_pnl = first_metric(
        rows,
        [
            {"include_all": ("gambit", "net", "pnl")},
            {"include_all": ("simulation", "net", "pnl")},
            {"include_all": ("net", "pnl"), "exclude": ("e4", "actual")},
        ],
    )
    e4_win_rate = first_metric(
        rows,
        [
            {"include_all": ("e4", "net", "win", "rate")},
            {"include_all": ("actual", "e4", "win", "rate")},
        ],
    )
    e4_trades = first_metric(
        rows,
        [
            {"include_all": ("e4", "closed")},
            {"include_all": ("actual", "positions")},
        ],
    )
    unique_launches = first_metric(
        rows,
        [
            {"include_all": ("unique", "launch")},
            {"include_all": ("captured", "launch")},
        ],
    )
    return {
        "payload": payload,
        "metrics": {
            "builder_p50_ms": builder_p50,
            "builder_p95_ms": builder_p95,
            "signing_p95_ms": signing_p95,
            "route_ack_p95_ms": route_p95,
            "builder_success_rate": builder_success,
            "gambit_net_win_rate": gambit_win_rate,
            "gambit_closed_trades": gambit_trades,
            "gambit_net_pnl_sol": gambit_pnl,
            "e4_net_win_rate": e4_win_rate,
            "e4_closed_trades": e4_trades,
            "unique_launches": unique_launches,
        },
    }


def metric_value(metric: tuple[str, float] | None, default: float | None = None) -> float | None:
    return metric[1] if metric is not None else default


def main() -> int:
    parser = argparse.ArgumentParser(description="Certify E4 V10 speed and live hypothetical performance")
    parser.add_argument("--pipeline", type=Path, required=True)
    parser.add_argument("--holdout", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minimum-strategy-trades", type=int, default=20)
    args = parser.parse_args()

    pipeline = load(args.pipeline)
    holdout = extract_holdout(args.holdout)
    metrics = holdout["metrics"]
    pipeline_p99_us = number((((pipeline.get("sequential") or {}).get("latency") or {}).get("p99_us")))
    builder_p95_ms = metric_value(metrics["builder_p95_ms"])
    signing_p95_ms = metric_value(metrics["signing_p95_ms"], 0.0)
    route_p95_ms = metric_value(metrics["route_ack_p95_ms"])

    recognition_ms = (pipeline_p99_us or float("inf")) / 1_000.0
    recognition_to_signed_ms = recognition_ms + (builder_p95_ms or float("inf")) + (signing_p95_ms or 0.0)
    full_e2e_ms = recognition_to_signed_ms + (route_p95_ms if route_p95_ms is not None else float("inf"))

    gambit_trades = int(metric_value(metrics["gambit_closed_trades"], 0.0) or 0)
    gambit_wr = metric_value(metrics["gambit_net_win_rate"])
    e4_wr = metric_value(metrics["e4_net_win_rate"])
    gates = {
        "pipeline_stress_passed": bool(pipeline.get("passed")),
        "recognition_p99_under_1ms": recognition_ms <= 1.0,
        "recognition_to_signed_p95_under_36ms": recognition_to_signed_ms <= 36.0,
        "route_ack_measured": route_p95_ms is not None,
        "full_launch_to_route_ack_p95_under_36ms": full_e2e_ms <= 36.0,
        "builder_success_at_least_99_9pct": (metric_value(metrics["builder_success_rate"], 1.0) or 0.0) >= 0.999,
        "strategy_sample_large_enough": gambit_trades >= args.minimum_strategy_trades,
        "strategy_net_win_rate_measured": gambit_wr is not None,
        "same_window_e4_win_rate_measured": e4_wr is not None,
    }
    speed_certified = all(
        gates[key]
        for key in (
            "pipeline_stress_passed",
            "recognition_p99_under_1ms",
            "recognition_to_signed_p95_under_36ms",
            "route_ack_measured",
            "full_launch_to_route_ack_p95_under_36ms",
            "builder_success_at_least_99_9pct",
        )
    )
    strategy_certified = all(
        gates[key]
        for key in (
            "strategy_sample_large_enough",
            "strategy_net_win_rate_measured",
            "same_window_e4_win_rate_measured",
        )
    )
    report = {
        "version": "e4-v10-certification-v1",
        "speed": {
            "pipeline_decision_p99_us": pipeline_p99_us,
            "builder_p95_ms": builder_p95_ms,
            "signing_p95_ms": signing_p95_ms,
            "route_ack_p95_ms": route_p95_ms,
            "recognition_to_signed_p95_ms": recognition_to_signed_ms,
            "launch_to_first_route_ack_p95_ms": None if not math.isfinite(full_e2e_ms) else full_e2e_ms,
            "certified_under_36ms": speed_certified,
        },
        "strategy": {
            "launches": metric_value(metrics["unique_launches"]),
            "gambit_closed_trades": gambit_trades,
            "gambit_net_win_rate": gambit_wr,
            "gambit_net_pnl_sol": metric_value(metrics["gambit_net_pnl_sol"]),
            "same_window_e4_closed_trades": int(metric_value(metrics["e4_closed_trades"], 0.0) or 0),
            "same_window_e4_net_win_rate": e4_wr,
            "win_rate_gap": (gambit_wr - e4_wr) if gambit_wr is not None and e4_wr is not None else None,
            "certified": strategy_certified,
        },
        "gates": gates,
        "fully_certified": speed_certified and strategy_certified,
        "metric_paths": metrics,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    markdown = args.output.with_suffix(".md")
    speed = report["speed"]
    strategy = report["strategy"]
    markdown.write_text(
        "# E4 V10 certification\n\n"
        f"## Speed\n\n"
        f"- Pipeline decision p99: **{speed['pipeline_decision_p99_us']} µs**\n"
        f"- Builder p95: **{speed['builder_p95_ms']} ms**\n"
        f"- Signing p95: **{speed['signing_p95_ms']} ms**\n"
        f"- Route ACK p95: **{speed['route_ack_p95_ms']} ms**\n"
        f"- Recognition → signed p95: **{speed['recognition_to_signed_p95_ms']} ms**\n"
        f"- Launch → first route ACK p95: **{speed['launch_to_first_route_ack_p95_ms']} ms**\n"
        f"- ≤36ms certified: **{speed['certified_under_36ms']}**\n\n"
        f"## Live hypothetical strategy\n\n"
        f"- Launches: **{strategy['launches']}**\n"
        f"- Gambit trades: **{strategy['gambit_closed_trades']}**\n"
        f"- Gambit net win rate: **{strategy['gambit_net_win_rate']}**\n"
        f"- Gambit net P&L: **{strategy['gambit_net_pnl_sol']} SOL**\n"
        f"- Same-window E4 trades: **{strategy['same_window_e4_closed_trades']}**\n"
        f"- Same-window E4 net win rate: **{strategy['same_window_e4_net_win_rate']}**\n"
        f"- Win-rate gap: **{strategy['win_rate_gap']}**\n\n"
        f"## Verdict\n\n"
        f"**Fully certified: {report['fully_certified']}**\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(args.output), "speed_certified": speed_certified, "strategy_certified": strategy_certified, "fully_certified": report["fully_certified"]}), flush=True)
    return 0 if report["fully_certified"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

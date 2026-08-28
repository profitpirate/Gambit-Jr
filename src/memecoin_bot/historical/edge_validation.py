from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

CORE_TIERS = frozenset({"PREMIUM", "STRONG"})
SPECIALIST_TIERS = frozenset({"HIGH_RISK_MOMENTUM", "CATALYST_REVIVAL"})
PRIMARY_PRECISION_TARGET = 0.80
MINIMUM_MATURED_CORE_SIGNALS = 250
MINIMUM_SEALED_WINDOWS = 3
REQUIRED_MATURITY_HOURS = 7 * 24


@dataclass(frozen=True, slots=True)
class SealedWindow:
    name: str
    start: str
    end: str

    def parsed(self) -> tuple[datetime, datetime]:
        return datetime.fromisoformat(self.start), datetime.fromisoformat(self.end)


def wilson_interval(
    successes: int, sample: int, z: float = 1.959963984540054
) -> tuple[float, float] | None:
    if sample <= 0:
        return None
    estimate = successes / sample
    denominator = 1 + z * z / sample
    center = (estimate + z * z / (2 * sample)) / denominator
    spread = (
        z
        / denominator
        * math.sqrt(estimate * (1 - estimate) / sample + z * z / (4 * sample * sample))
    )
    return max(0.0, center - spread), min(1.0, center + spread)


def validate_sealed_windows(windows: Iterable[SealedWindow], maturity_hours: int) -> None:
    ordered = sorted(windows, key=lambda window: window.parsed()[0])
    for window in ordered:
        start, end = window.parsed()
        if start >= end:
            raise ValueError(f"invalid sealed window {window.name}")
    for previous, current in pairwise(ordered):
        _, previous_end = previous.parsed()
        current_start, _ = current.parsed()
        if previous_end + timedelta(hours=maturity_hours) > current_start:
            raise ValueError(f"maturity leakage between {previous.name} and {current.name}")


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _cohort_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    sample = len(rows)
    runners_2x_24h = sum(float(row.get("peak_24h") or 0) >= 2 for row in rows)
    runners_2x = sum(float(row.get("peak_maturity") or 0) >= 2 for row in rows)
    runners_5x = sum(float(row.get("peak_maturity") or 0) >= 5 for row in rows)
    runners_10x = sum(float(row.get("peak_maturity") or 0) >= 10 for row in rows)
    return {
        "matured_signals": sample,
        "2x_24h_precision": runners_2x_24h / sample if sample else None,
        "2x_maturity_precision": runners_2x / sample if sample else None,
        "2x_maturity_wilson_95": wilson_interval(runners_2x, sample),
        "5x_precision": runners_5x / sample if sample else None,
        "10x_precision": runners_10x / sample if sample else None,
        "failure_rate": (
            sum(bool(row.get("terminal_failure")) for row in rows) / sample if sample else None
        ),
        "median_max_adverse_excursion": _median(
            [
                float(row["max_adverse_excursion"])
                for row in rows
                if row.get("max_adverse_excursion") is not None
            ]
        ),
        "median_market_cap_at_signal": _median(
            [
                float(row["market_cap_at_signal"])
                for row in rows
                if row.get("market_cap_at_signal") is not None
            ]
        ),
        "median_time_to_detection_seconds": _median(
            [
                float(row["time_to_detection_seconds"])
                for row in rows
                if row.get("time_to_detection_seconds") is not None
            ]
        ),
    }


def compare_predictions(
    rows: Iterable[dict[str, Any]],
    predictions: Mapping[str, Mapping[str, str]],
    windows: Iterable[SealedWindow],
    *,
    launch_count: int,
    maturity_hours_available: int,
) -> dict[str, Any]:
    evidence = list(rows)
    by_mint = {str(row["mint"]): row for row in evidence}
    window_rows: dict[str, list[dict[str, Any]]] = {}
    ordered_windows = list(windows)
    validate_sealed_windows(ordered_windows, maturity_hours_available)
    for window in ordered_windows:
        start, end = window.parsed()
        window_rows[window.name] = [
            row
            for row in evidence
            if start <= datetime.fromisoformat(str(row["decision_at"])) < end
        ]

    models: dict[str, Any] = {}
    total_major_runners = sum(float(row.get("peak_maturity") or 0) >= 20 for row in evidence)
    for model_name, model_predictions in predictions.items():
        unknown = sorted(set(model_predictions) - set(by_mint))
        if unknown:
            raise ValueError(f"{model_name} predicts {len(unknown)} tokens outside the universe")
        per_window = {}
        aggregate: list[dict[str, Any]] = []
        specialist: list[dict[str, Any]] = []
        for window in ordered_windows:
            rows_in_window = window_rows[window.name]
            core_rows = [
                row
                for row in rows_in_window
                if model_predictions.get(str(row["mint"])) in CORE_TIERS
            ]
            specialist_rows = [
                row
                for row in rows_in_window
                if model_predictions.get(str(row["mint"])) in SPECIALIST_TIERS
            ]
            aggregate.extend(core_rows)
            specialist.extend(specialist_rows)
            per_window[window.name] = {
                "core": _cohort_metrics(core_rows),
                "specialist": _cohort_metrics(specialist_rows),
            }
        result = _cohort_metrics(aggregate)
        captured_major = sum(float(row.get("peak_maturity") or 0) >= 20 for row in aggregate)
        premium = sum(model_predictions.get(str(row["mint"])) == "PREMIUM" for row in evidence)
        strong = sum(model_predictions.get(str(row["mint"])) == "STRONG" for row in evidence)
        result.update(
            {
                "major_runner_recall": (
                    captured_major / total_major_runners if total_major_runners else None
                ),
                "signals_per_10k_launches": (
                    len(aggregate) / launch_count * 10_000 if launch_count else None
                ),
                "premium_frequency": premium,
                "strong_frequency": strong,
                "per_window": per_window,
                "specialist": _cohort_metrics(specialist),
            }
        )
        models[model_name] = result

    candidate = models.get("CANDIDATE_V15") or {}
    valid_windows = sum(
        int((details["core"]["matured_signals"] or 0) > 0)
        for details in candidate.get("per_window", {}).values()
    )
    gate_failures = []
    if maturity_hours_available < REQUIRED_MATURITY_HOURS:
        gate_failures.append("SEVEN_DAY_MATURITY_UNAVAILABLE")
    if int(candidate.get("matured_signals") or 0) < MINIMUM_MATURED_CORE_SIGNALS:
        gate_failures.append("INSUFFICIENT_MATURED_CORE_SIGNALS")
    if valid_windows < MINIMUM_SEALED_WINDOWS:
        gate_failures.append("INSUFFICIENT_SEALED_WINDOWS")
    if float(candidate.get("2x_maturity_precision") or 0) < PRIMARY_PRECISION_TARGET:
        gate_failures.append("PRIMARY_PRECISION_BELOW_80_PERCENT")
    return {
        "models": models,
        "contract": {
            "primary_metric": "qualified core signal 2x precision",
            "core_tiers": sorted(CORE_TIERS),
            "target": PRIMARY_PRECISION_TARGET,
            "minimum_matured_signals": MINIMUM_MATURED_CORE_SIGNALS,
            "minimum_sealed_windows": MINIMUM_SEALED_WINDOWS,
            "required_maturity_hours": REQUIRED_MATURITY_HOURS,
            "available_maturity_hours": maturity_hours_available,
        },
        "acceptance": "PASS" if not gate_failures else "FAIL TARGET",
        "gate_failures": gate_failures,
    }

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

FRONTIER_FREQUENCIES = (0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.02, 0.05)


@dataclass(frozen=True, slots=True)
class WalkForwardWindow:
    outer_index: int
    train_indexes: tuple[int, ...]
    validation_indexes: tuple[int, ...]
    test_indexes: tuple[int, ...]
    train_end: str
    validation_end: str
    test_end: str


def nested_walk_forward_windows(
    rows: Sequence[Mapping[str, Any]],
    *,
    timestamp_field: str = "decision_timestamp",
    train_days: int,
    validation_days: int,
    test_days: int,
    maturity_embargo_days: int,
    step_days: int | None = None,
) -> list[WalkForwardWindow]:
    """Build chronological outer tests with purged train/validation maturity gaps."""
    if min(train_days, validation_days, test_days) <= 0 or maturity_embargo_days < 0:
        raise ValueError("window lengths must be positive and embargo non-negative")
    ordered = sorted(range(len(rows)), key=lambda index: _time(str(rows[index][timestamp_field])))
    if not ordered:
        return []
    start = _time(str(rows[ordered[0]][timestamp_field]))
    finish = _time(str(rows[ordered[-1]][timestamp_field]))
    stride = timedelta(days=step_days or test_days)
    train_span = timedelta(days=train_days)
    validation_span = timedelta(days=validation_days)
    test_span = timedelta(days=test_days)
    embargo = timedelta(days=maturity_embargo_days)
    cursor = start
    result: list[WalkForwardWindow] = []
    while True:
        train_end = cursor + train_span
        validation_start = train_end + embargo
        validation_end = validation_start + validation_span
        test_start = validation_end + embargo
        test_end = test_start + test_span
        if test_end > finish + timedelta(microseconds=1):
            break
        train = tuple(
            index
            for index in ordered
            if cursor <= _time(str(rows[index][timestamp_field])) < train_end
        )
        validation = tuple(
            index
            for index in ordered
            if validation_start <= _time(str(rows[index][timestamp_field])) < validation_end
        )
        test = tuple(
            index
            for index in ordered
            if test_start <= _time(str(rows[index][timestamp_field])) < test_end
        )
        if train and validation and test:
            result.append(
                WalkForwardWindow(
                    outer_index=len(result),
                    train_indexes=train,
                    validation_indexes=validation,
                    test_indexes=test,
                    train_end=train_end.isoformat(),
                    validation_end=validation_end.isoformat(),
                    test_end=test_end.isoformat(),
                )
            )
        cursor += stride
    return result


def assert_group_isolation(
    rows: Sequence[Mapping[str, Any]],
    window: WalkForwardWindow,
    group_fields: Iterable[str] = ("creator_id", "funder_id", "wallet_cluster_id"),
) -> None:
    for field in group_fields:
        train_groups = _groups(rows, window.train_indexes, field)
        validation_groups = _groups(rows, window.validation_indexes, field)
        test_groups = _groups(rows, window.test_indexes, field)
        overlap = (train_groups | validation_groups) & test_groups
        overlap |= train_groups & validation_groups
        if overlap:
            sample = ", ".join(sorted(overlap)[:3])
            raise ValueError(f"{field} leakage across walk-forward partitions: {sample}")


def group_purge_window(
    rows: Sequence[Mapping[str, Any]],
    window: WalkForwardWindow,
    group_fields: Iterable[str] = ("creator_id", "funder_id", "wallet_cluster_id"),
) -> WalkForwardWindow:
    """Remove earlier rows sharing any entity with validation/test; never move future rows backward."""
    test_groups = {field: _groups(rows, window.test_indexes, field) for field in group_fields}

    def allowed(index: int, blocked: Mapping[str, set[str]]) -> bool:
        return all(
            not _row_groups(rows[index], field).intersection(blocked[field])
            for field in blocked
        )

    validation = tuple(index for index in window.validation_indexes if allowed(index, test_groups))
    all_later = {
        field: test_groups[field] | _groups(rows, validation, field) for field in test_groups
    }
    train = tuple(index for index in window.train_indexes if allowed(index, all_later))
    return WalkForwardWindow(
        outer_index=window.outer_index,
        train_indexes=train,
        validation_indexes=validation,
        test_indexes=window.test_indexes,
        train_end=window.train_end,
        validation_end=window.validation_end,
        test_end=window.test_end,
    )


def natural_prevalence_sample_weights(labels: Sequence[bool], sampled: Sequence[bool]) -> list[float]:
    """Weights a computational sample back to untouched event prevalence."""
    if len(labels) != len(sampled) or not labels:
        raise ValueError("labels and sampled masks must be non-empty and equal length")
    population_positive = sum(labels)
    population_negative = len(labels) - population_positive
    selected_positive = sum(label and keep for label, keep in zip(labels, sampled, strict=True))
    selected_negative = sum((not label) and keep for label, keep in zip(labels, sampled, strict=True))
    if selected_positive == 0 or selected_negative == 0:
        raise ValueError("sample must retain both classes")
    positive_weight = population_positive / selected_positive
    negative_weight = population_negative / selected_negative
    return [
        positive_weight if label else negative_weight if keep else 0.0
        for label, keep in zip(labels, sampled, strict=True)
    ]


def calibration_report(probabilities: Sequence[float], labels: Sequence[bool], bins: int = 10) -> dict[str, Any]:
    if len(probabilities) != len(labels) or not probabilities:
        raise ValueError("probabilities and labels must be non-empty and equal length")
    if any(not 0 <= value <= 1 for value in probabilities):
        raise ValueError("probabilities must be between zero and one")
    brier = sum((probability - float(label)) ** 2 for probability, label in zip(probabilities, labels, strict=True)) / len(labels)
    reliability = []
    ece = 0.0
    for bin_index in range(bins):
        lower = bin_index / bins
        upper = (bin_index + 1) / bins
        indexes = [
            index
            for index, probability in enumerate(probabilities)
            if lower <= probability < upper or (bin_index == bins - 1 and probability == 1)
        ]
        if not indexes:
            continue
        predicted = sum(probabilities[index] for index in indexes) / len(indexes)
        observed = sum(labels[index] for index in indexes) / len(indexes)
        ece += len(indexes) / len(labels) * abs(predicted - observed)
        reliability.append(
            {"lower": lower, "upper": upper, "count": len(indexes), "predicted": predicted, "observed": observed}
        )
    return {"sample": len(labels), "prevalence": sum(labels) / len(labels), "brier": brier, "ece": ece, "reliability": reliability}


def precision_frequency_frontier(
    rows: Sequence[Mapping[str, Any]],
    scores: Sequence[float],
    *,
    outcome: Callable[[Mapping[str, Any], float], bool],
    frequencies: Sequence[float] = FRONTIER_FREQUENCIES,
) -> list[dict[str, Any]]:
    if len(rows) != len(scores):
        raise ValueError("rows and scores must have equal length")
    order = sorted(range(len(rows)), key=lambda index: (-scores[index], index))
    reports = []
    for frequency in frequencies:
        if not 0 < frequency <= 1:
            raise ValueError("frontier frequencies must be in (0, 1]")
        count = max(1, round(len(rows) * frequency)) if rows else 0
        selected = [rows[index] for index in order[:count]]
        record: dict[str, Any] = {"frequency": frequency, "signal_count": count}
        for threshold in (2.0, 5.0, 10.0):
            hits = sum(outcome(row, threshold) for row in selected)
            low, high = wilson_interval(hits, count)
            record[f"{int(threshold)}x_precision"] = hits / count if count else None
            record[f"{int(threshold)}x_wilson_95"] = [low, high]
        for threshold in (20.0, 50.0):
            universe_hits = sum(outcome(row, threshold) for row in rows)
            selected_hits = sum(outcome(row, threshold) for row in selected)
            record[f"{int(threshold)}x_recall"] = selected_hits / universe_hits if universe_hits else None
        reports.append(record)
    return reports


def wilson_interval(successes: int, sample: int, confidence_z: float = 1.959963984540054) -> tuple[float | None, float | None]:
    if sample == 0:
        return None, None
    if not 0 <= successes <= sample:
        raise ValueError("successes must be between zero and sample")
    rate = successes / sample
    denominator = 1 + confidence_z**2 / sample
    centre = (rate + confidence_z**2 / (2 * sample)) / denominator
    spread = confidence_z * math.sqrt(rate * (1 - rate) / sample + confidence_z**2 / (4 * sample**2)) / denominator
    return max(0.0, centre - spread), min(1.0, centre + spread)


def _groups(rows: Sequence[Mapping[str, Any]], indexes: Iterable[int], field: str) -> set[str]:
    values: set[str] = set()
    for index in indexes:
        values.update(_row_groups(rows[index], field))
    return values


def _row_groups(row: Mapping[str, Any], field: str) -> set[str]:
    value = row.get(field)
    if value is None or value == "":
        return set()
    if isinstance(value, (list, tuple, set, frozenset)):
        return {str(item) for item in value if item not in (None, "")}
    return {str(value)}


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include timezone: {value}")
    return parsed.astimezone(UTC)

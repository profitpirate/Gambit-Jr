from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

AUTOPSY_TIMESTAMPS = (30, 60, 180, 300, 600, 1800, 3600)
RUNNER_COHORTS = (2, 3, 5, 10, 20, 50)
CORE_TIERS = frozenset({"PREMIUM", "STRONG"})
FEATURES = (
    "log_market_cap",
    "market_cap_growth",
    "curve_progress",
    "momentum_score",
    "log_trade_count",
    "buy_pressure",
    "log_buy_volume",
    "log_buyer_count",
    "buyer_growth_score",
    "creator_score",
    "concentration_score",
)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _mean(values: Iterable[float | None]) -> float | None:
    known = [value for value in values if value is not None and math.isfinite(value)]
    return sum(known) / len(known) if known else None


def _rate(values: Sequence[bool]) -> float | None:
    return sum(values) / len(values) if values else None


def entry_gate(multiple: float | None, age_seconds: int, acceleration: float | None) -> str:
    """Mirror V1.5's fixed entry thresholds using dimensionless market-cap growth."""
    if multiple is None or multiple <= 0:
        return "UNKNOWN"
    if multiple >= 3 or (acceleration is not None and acceleration >= 2.5):
        return "CHASING"
    if multiple >= 1.7 or age_seconds >= 90 * 60:
        return "EXTENDED"
    return "OPEN"


def reconstruct_decision(row: Mapping[str, Any]) -> dict[str, Any]:
    """Reconstruct only the observable portion of CONTROL_V15.

    The public corpus does not contain exact production provider vectors.  Missing fields stay
    missing and the failure score is therefore explicitly a lower bound.
    """
    stage = str(row.get("stage") or "NEW")
    common = {
        "launch_verified": 100.0,
        "momentum": _number(row.get("momentum_score")),
        "buyers": _number(row.get("buyer_growth_score")),
        "creator": _number(row.get("creator_score")),
        "concentration": _number(row.get("concentration_score")),
        "liquidity": _number(row.get("liquidity_score")),
        "survival": _number(row.get("survival_score")),
        "payoff": _number(row.get("payoff_score")),
        "curve": _number(row.get("curve_progress")),
    }
    if stage == "MIGRATED":
        values = (
            common["liquidity"],
            _number(row.get("tradeability_score")),
            None,  # migration_continuity is also always None in the live builder
            common["buyers"],
            common["buyers"],
            common["concentration"],
            common["momentum"],
            common["survival"],
            common["payoff"],
        )
    elif stage == "BONDING":
        values = (
            common["curve"],
            common["momentum"],
            common["buyers"],
            common["buyers"],
            None,  # concentration trend is not present in this source
            common["survival"],
            common["payoff"],
        )
    else:
        values = (
            common["launch_verified"],
            common["momentum"],
            common["concentration"],
            common["creator"],
            common["liquidity"],
            common["survival"],
            common["payoff"],
        )
    known = [value for value in values if value is not None]
    runner = _mean(known) or 0.0
    coverage = len(known) / len(values) * 100
    risks: list[str] = []
    failure = 0.0
    for enabled, name, weight in (
        (bool(row.get("concentration_unknown")), "CONCENTRATION_UNKNOWN", 20),
        (bool(row.get("toxic_creator")), "TOXIC_CREATOR", 35),
        (bool(row.get("poor_tradeability")), "POOR_TRADEABILITY", 30),
        (bool(row.get("buyer_collapse_proxy")), "BUYER_COLLAPSE_PROXY", 35),
        (bool(row.get("liquidity_deterioration")), "LIQUIDITY_DETERIORATION", 35),
    ):
        if enabled:
            failure += weight
            risks.append(name)
    failure = min(100.0, failure)
    entry = entry_gate(
        _number(row.get("market_cap_growth")),
        int(row.get("timestamp_seconds") or 0),
        _number(row.get("vertical_acceleration")),
    )
    critical = bool(row.get("critical_unknown"))
    if runner >= 75 and failure >= 50:
        tier = "HIGH_RISK_MOMENTUM"
    elif runner >= 75 and failure < 30:
        tier = "PREMIUM"
    elif runner >= 60 and failure < 40:
        tier = "STRONG"
    else:
        tier = "SILENT_WATCH"
    if tier == "PREMIUM" and (coverage < 75 or critical or entry != "OPEN"):
        tier = "STRONG" if runner >= 60 and entry != "CHASING" else "SILENT_WATCH"
    if entry in {"CHASING", "UNKNOWN"} and tier in CORE_TIERS:
        tier = "SILENT_WATCH"
    return {
        "runner_score": round(runner, 4),
        "failure_score_lower_bound": round(failure, 4),
        "coverage": round(coverage, 4),
        "entry_status": entry,
        "tier": tier,
        "failure_reasons": risks,
        "known_stage_features": len(known),
        "total_stage_features": len(values),
    }


def miss_reason(row: Mapping[str, Any]) -> str:
    if not row.get("discovered"):
        return "NOT_DISCOVERED"
    if not row.get("discovered_early"):
        return "DISCOVERED_TOO_LATE"
    if not row.get("evaluated"):
        return "STATE/LIFECYCLE ISSUE"
    if float(row.get("coverage") or 0) < 75:
        return "LOW_COVERAGE"
    if row.get("critical_unknown"):
        return "CRITICAL_UNKNOWN"
    if row.get("provider_conflict"):
        return "PROVIDER_CONFLICT"
    if str(row.get("entry_status")) == "CHASING":
        return "ENTRY_CHASING"
    if str(row.get("entry_status")) == "UNKNOWN":
        return "STATE/LIFECYCLE ISSUE"
    if float(row.get("runner_score") or 0) < 60:
        return "LOW_RUNNER_SCORE"
    reasons = set(row.get("failure_reasons") or [])
    if float(row.get("failure_score_lower_bound") or 0) >= 40:
        if "TOXIC_CREATOR" in reasons:
            return "CREATOR_PENALTY"
        if "CONCENTRATION_UNKNOWN" in reasons:
            return "CONCENTRATION_PENALTY"
        if "BUYER_COLLAPSE_PROXY" in reasons:
            return "BUYER_COLLAPSE"
        if "POOR_TRADEABILITY" in reasons or "LIQUIDITY_DETERIORATION" in reasons:
            return "LIQUIDITY_FILTER"
        return "FAILURE_SCORE_TOO_HIGH"
    if str(row.get("entry_status")) == "EXTENDED" and float(row.get("runner_score") or 0) >= 75:
        return "ENTRY_EXTENDED"
    if str(row.get("tier")) not in CORE_TIERS:
        return "TIER_THRESHOLD"
    return "OTHER"


def cohort_funnel(rows: Iterable[Mapping[str, Any]], threshold: int) -> dict[str, Any]:
    cohort = [row for row in rows if float(row.get("peak_multiple") or 0) >= threshold]
    stages: list[tuple[str, Any]] = [
        ("total_runners", lambda row: True),
        ("discovered", lambda row: bool(row.get("discovered"))),
        ("discovered_early_enough", lambda row: bool(row.get("discovered_early"))),
        ("evaluated", lambda row: bool(row.get("evaluated"))),
        ("sufficient_coverage", lambda row: float(row.get("coverage") or 0) >= 75),
        ("runner_score_gte_watch", lambda row: float(row.get("runner_score") or 0) >= 60),
        ("runner_score_gte_strong", lambda row: float(row.get("runner_score") or 0) >= 75),
        ("entry_open", lambda row: row.get("entry_status") == "OPEN"),
        ("failure_below_cap", lambda row: float(row.get("failure_score_lower_bound") or 0) < 40),
        ("premium_or_strong", lambda row: row.get("tier") in CORE_TIERS),
        ("public_eligible", lambda row: row.get("tier") in CORE_TIERS),
        ("reconstructed_signaled", lambda row: row.get("tier") in CORE_TIERS),
    ]
    total = len(cohort)
    result = {}
    for name, predicate in stages:
        count = sum(predicate(row) for row in cohort)
        result[name] = {"count": count, "percent": count / total if total else None}
    strict_path = list(cohort)
    for name, predicate in (
        ("discovered", lambda row: bool(row.get("discovered"))),
        ("discovered_early_enough", lambda row: bool(row.get("discovered_early"))),
        ("evaluated", lambda row: bool(row.get("evaluated"))),
        ("sufficient_coverage", lambda row: float(row.get("coverage") or 0) >= 75),
        ("runner_score_gte_watch", lambda row: float(row.get("runner_score") or 0) >= 60),
        ("entry_open", lambda row: row.get("entry_status") == "OPEN"),
        ("failure_below_cap", lambda row: float(row.get("failure_score_lower_bound") or 0) < 40),
        ("public_eligible", lambda row: row.get("tier") in CORE_TIERS),
    ):
        strict_path = [row for row in strict_path if predicate(row)]
        result.setdefault("strict_attrition_path", {})[name] = {
            "count": len(strict_path),
            "percent": len(strict_path) / total if total else None,
        }
    result["miss_reasons"] = dict(
        Counter(miss_reason(row) for row in cohort if row.get("tier") not in CORE_TIERS)
    )
    return result


def standardized_effect(runners: Sequence[float], others: Sequence[float]) -> float | None:
    if len(runners) < 2 or len(others) < 2:
        return None
    runner_var = statistics.variance(runners)
    other_var = statistics.variance(others)
    denominator = len(runners) + len(others) - 2
    pooled = math.sqrt(
        ((len(runners) - 1) * runner_var + (len(others) - 1) * other_var) / denominator
    )
    return (statistics.mean(runners) - statistics.mean(others)) / pooled if pooled else 0.0


def mutual_information_binary(
    values: Sequence[float], labels: Sequence[bool], bins: int = 10
) -> float:
    if not values or len(values) != len(labels):
        return 0.0
    ordered = sorted(values)
    cuts = [
        ordered[min(len(ordered) - 1, int(len(ordered) * index / bins))] for index in range(1, bins)
    ]
    cells: Counter[tuple[int, bool]] = Counter()
    label_counts = Counter(labels)
    bucket_counts: Counter[int] = Counter()
    for value, label in zip(values, labels, strict=True):
        bucket = sum(value > cut for cut in cuts)
        cells[bucket, label] += 1
        bucket_counts[bucket] += 1
    sample = len(values)
    result = 0.0
    for bucket, label in sorted(cells):
        count = cells[bucket, label]
        joint = count / sample
        result += joint * math.log(
            joint / (bucket_counts[bucket] / sample * label_counts[label] / sample)
        )
    return result


def feature_diagnostics(
    rows: Sequence[Mapping[str, Any]], feature_names: Sequence[str] = FEATURES
) -> list[dict[str, Any]]:
    findings = []
    for name in feature_names:
        known = sorted(
            ((value, row) for row in rows if (value := _number(row.get(name))) is not None),
            key=lambda item: (item[0], str(item[1].get("mint"))),
        )
        runners = [value for value, row in known if float(row.get("peak_multiple") or 0) >= 2]
        others = [value for value, row in known if float(row.get("peak_multiple") or 0) < 2]
        effect = standardized_effect(runners, others)
        labels = [float(row.get("peak_multiple") or 0) >= 2 for _, row in known]
        values = [value for value, _ in known]
        ordered_values = sorted(values)
        threshold_effects = {}
        for threshold in (2, 5, 10, 20):
            positive = [
                value for value, row in known if float(row.get("peak_multiple") or 0) >= threshold
            ]
            negative = [
                value for value, row in known if float(row.get("peak_multiple") or 0) < threshold
            ]
            threshold_effects[f"standardized_{threshold}x_effect"] = standardized_effect(
                positive, negative
            )
        week_effects: list[float] = []
        by_week: dict[str, list[tuple[float, bool]]] = defaultdict(list)
        for value, row in known:
            by_week[str(row.get("week_label"))].append(
                (value, float(row.get("peak_multiple") or 0) >= 2)
            )
        for group in by_week.values():
            positive = [value for value, label in group if label]
            negative = [value for value, label in group if not label]
            value = standardized_effect(positive, negative)
            if value is not None:
                week_effects.append(value)
        stable = not week_effects or all(
            value == 0 or math.copysign(1, value) == math.copysign(1, effect or 1)
            for value in week_effects
        )
        magnitude = abs(effect or 0)
        if len(known) < len(rows) * 0.25:
            classification = "INSUFFICIENT DATA"
        elif not stable:
            classification = "UNSTABLE"
        elif magnitude >= 0.35:
            classification = "STRONG POSITIVE" if (effect or 0) > 0 else "NEGATIVE"
        elif magnitude >= 0.1:
            classification = "WEAK POSITIVE" if (effect or 0) > 0 else "NEGATIVE"
        elif (effect or 0) < -0.05:
            classification = "MISLEADING"
        else:
            classification = "NEUTRAL"
        findings.append(
            {
                "feature": name,
                "coverage": len(known) / len(rows) if rows else None,
                "missingness": 1 - len(known) / len(rows) if rows else None,
                "runner_mean": statistics.mean(runners) if runners else None,
                "runner_median": statistics.median(runners) if runners else None,
                "non_runner_mean": statistics.mean(others) if others else None,
                "non_runner_median": statistics.median(others) if others else None,
                "standardized_2x_effect": effect,
                "mutual_information_2x": mutual_information_binary(values, labels),
                "distribution_p10": ordered_values[len(ordered_values) // 10]
                if ordered_values
                else None,
                "distribution_p50": statistics.median(ordered_values) if ordered_values else None,
                "distribution_p90": ordered_values[len(ordered_values) * 9 // 10]
                if ordered_values
                else None,
                "weekly_direction_stable": stable,
                "classification": classification,
                **threshold_effects,
            }
        )
    return sorted(findings, key=lambda row: abs(row["standardized_2x_effect"] or 0), reverse=True)


def selection_metrics(
    selected: Sequence[Mapping[str, Any]], universe: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    total_20 = sum(float(row.get("peak_multiple") or 0) >= 20 for row in universe)
    total_50 = sum(float(row.get("peak_multiple") or 0) >= 50 for row in universe)
    sample = len(selected)
    return {
        "signals": sample,
        "signal_frequency": sample / len(universe) if universe else None,
        **{
            f"{threshold}x_precision": _rate(
                [float(row.get("peak_multiple") or 0) >= threshold for row in selected]
            )
            for threshold in (2, 3, 5, 10)
        },
        "20x_recall": (
            sum(float(row.get("peak_multiple") or 0) >= 20 for row in selected) / total_20
            if total_20
            else None
        ),
        "50x_recall": (
            sum(float(row.get("peak_multiple") or 0) >= 50 for row in selected) / total_50
            if total_50
            else None
        ),
        "failure_rate": _rate([bool(row.get("terminal_failure")) for row in selected]),
    }


def rank_model(
    rows: Sequence[Mapping[str, Any]], score: Any, fraction: float = 0.01
) -> dict[str, Any]:
    scored = [(value, row) for row in rows if (value := _number(score(row))) is not None]
    scored.sort(key=lambda item: (-item[0], str(item[1]["mint"])))
    selected = [row for _, row in scored[: max(1, round(len(rows) * fraction))]]
    return {
        "metrics": selection_metrics(selected, rows),
        "selected_mints": [row["mint"] for row in selected],
    }


def stable_random_score(row: Mapping[str, Any]) -> float:
    digest = hashlib.sha256(str(row["mint"]).encode()).hexdigest()
    return int(digest[:13], 16) / float(16**13 - 1)


def fit_histogram_score(
    train: Sequence[Mapping[str, Any]], features: Sequence[str], bins: int = 5
) -> Any:
    """Fit a smoothed nonlinear histogram classifier on earlier diagnostic rows only."""
    specs: dict[str, tuple[list[float], list[float]]] = {}
    for feature in features:
        pairs = [
            (value, float(row.get("peak_multiple") or 0) >= 2)
            for row in train
            if (value := _number(row.get(feature))) is not None
        ]
        if not pairs:
            continue
        ordered = sorted(value for value, _ in pairs)
        cuts = [
            ordered[min(len(ordered) - 1, len(ordered) * index // bins)] for index in range(1, bins)
        ]
        counts = [[1, 2] for _ in range(bins)]
        for value, label in pairs:
            bucket = sum(value > cut for cut in cuts)
            counts[bucket][0] += int(label)
            counts[bucket][1] += 1
        rates = [successes / sample for successes, sample in counts]
        specs[feature] = (cuts, rates)

    def score(row: Mapping[str, Any]) -> float | None:
        values = []
        for feature, (cuts, rates) in specs.items():
            value = _number(row.get(feature))
            if value is not None:
                values.append(rates[sum(value > cut for cut in cuts)])
        return _mean(values)

    return score


def fit_interaction_grid(
    train: Sequence[Mapping[str, Any]], first: str, second: str, bins: int = 5
) -> Any:
    """Fit a shallow two-feature decision grid without reading later outcomes."""
    known = [
        (one, two, float(row.get("peak_multiple") or 0) >= 2)
        for row in train
        if (one := _number(row.get(first))) is not None
        and (two := _number(row.get(second))) is not None
    ]
    if not known:
        return lambda row: None

    def cuts(values: list[float]) -> list[float]:
        ordered = sorted(values)
        return [
            ordered[min(len(ordered) - 1, len(ordered) * index // bins)] for index in range(1, bins)
        ]

    first_cuts = cuts([row[0] for row in known])
    second_cuts = cuts([row[1] for row in known])
    cells: dict[tuple[int, int], list[int]] = defaultdict(lambda: [1, 2])
    for one, two, label in known:
        cell = (sum(one > cut for cut in first_cuts), sum(two > cut for cut in second_cuts))
        cells[cell][0] += int(label)
        cells[cell][1] += 1

    def score(row: Mapping[str, Any]) -> float | None:
        one = _number(row.get(first))
        two = _number(row.get(second))
        if one is None or two is None:
            return None
        cell = (sum(one > cut for cut in first_cuts), sum(two > cut for cut in second_cuts))
        successes, sample = cells[cell]
        return successes / sample

    return score


def replay_cards(rows: Iterable[Mapping[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["mint"])].append(row)
    cards = []
    for mint, history in grouped.items():
        ordered = sorted(history, key=lambda row: int(row.get("timestamp_seconds") or 0))
        reference = next(
            (row for row in ordered if row.get("timestamp_seconds") == 180), ordered[-1]
        )
        cards.append(
            {
                "mint": mint,
                "peak_multiple": reference.get("peak_multiple"),
                "miss_reason": miss_reason(reference),
                "timeline": [
                    {
                        "timestamp_seconds": row.get("timestamp_seconds"),
                        "stage": row.get("stage"),
                        "market_cap": row.get("current_market_cap"),
                        "market_cap_unit": row.get("market_cap_unit"),
                        "buyers": row.get("buyer_count"),
                        "runner_score": row.get("runner_score"),
                        "failure_score_lower_bound": row.get("failure_score_lower_bound"),
                        "coverage": row.get("coverage"),
                        "entry_status": row.get("entry_status"),
                        "tier": row.get("tier"),
                    }
                    for row in ordered
                ],
            }
        )
    cards.sort(key=lambda card: float(card.get("peak_multiple") or 0), reverse=True)
    return cards[:limit]


def _percent(value: Any) -> str:
    return "N/A" if value is None else f"{float(value) * 100:.2f}%"


def render_autopsy_markdown(result: Mapping[str, Any]) -> str:
    """Render the immutable human review artifact from the machine-readable result."""
    models = result["models"]

    def best(metric: str) -> tuple[str, Mapping[str, Any]]:
        return max(
            models.items(),
            key=lambda item: float(item[1][metric]) if item[1].get(metric) is not None else -1,
        )

    best_2x = best("2x_precision")
    best_5x = best("5x_precision")
    best_10x = best("10x_precision")
    best_20x = best("20x_recall")
    best_50x = best("50x_recall")
    miss_5x = result["miss_attribution"]["5x"]
    miss_20x = result["miss_attribution"]["20x"]
    miss_50x = result["miss_attribution"]["50x"]
    mc_buckets = {row["bucket_sol"]: row for row in result["market_cap_buckets"]}
    extreme_low = mc_buckets["0.01-1"]
    overextended = mc_buckets["250-1000"]
    lines = [
        "# Gambit Jr V1.5 runner-intelligence failure autopsy",
        "",
        (
            "This is a diagnostic-only reconstruction. It does not modify `CONTROL_V15`, approve a "
            "feature, create a challenger, or authorize production deployment. June/July rows are "
            "retired diagnostics and are not sealed evidence."
        ),
        "",
        "## Evidence boundary and outcome-label correction",
        "",
        f"- T+3m source rows: **{result['source_rows']:,}**",
        f"- Quality-bounded analysis rows: **{result['valid_analysis_rows']:,}**",
        "- Exact historical production provider vectors: **unavailable**",
        "- Outcome maturity: **48 hours**, not the required seven days",
        (
            "- Rejected prior fields: `edge_3m.peak_48h` and `tokens.peak_market_cap_sol` "
            "contained impossible unit/reserve outliers"
        ),
        "- Replacement: dimensionless point-in-time market-cap and post-graduation price ratios",
        "",
        (
            "The previously reported 34.22% market-cap-only figure used the rejected derived peak "
            "field. It is not retained as valid autopsy evidence. The corrected chronological "
            "diagnostic result is reported below."
        ),
        "",
        "## 1–6. Runner attrition funnels at T+3m",
        "",
        (
            "Counts in the main columns are independent diagnostics; `strict final` applies discovery, "
            "coverage, score, OPEN entry, failure and public-tier gates in sequence."
        ),
        "",
        (
            "| Cohort | Total | Early | Coverage | Score ≥60 | Score ≥75 | Entry OPEN | Failure <40 | "
            "Core tier | Strict final |"
        ),
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for cohort, funnel in result["funnels"].items():
        strict = funnel["strict_attrition_path"]["public_eligible"]["count"]
        lines.append(
            f"| {cohort} | {funnel['total_runners']['count']:,} | "
            f"{funnel['discovered_early_enough']['count']:,} | "
            f"{funnel['sufficient_coverage']['count']:,} | "
            f"{funnel['runner_score_gte_watch']['count']:,} | "
            f"{funnel['runner_score_gte_strong']['count']:,} | "
            f"{funnel['entry_open']['count']:,} | "
            f"{funnel['failure_below_cap']['count']:,} | "
            f"{funnel['premium_or_strong']['count']:,} | {strict:,} |"
        )
    lines.extend(
        [
            "",
            (
                "`reconstructed_signaled` means the observable-field reconstruction reached PREMIUM "
                "or STRONG. It is not a claim that a historical live Discord alert exists."
            ),
            "",
            "## 7–12. Exact miss attribution",
            "",
            (
                "| Cohort | Missed | Discovery | Intelligence/score | Entry | Failure gate | Coverage | "
                "State/provider |"
            ),
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for cohort, row in result["miss_attribution"].items():
        lines.append(
            f"| {cohort} | {row['missed']:,} | {_percent(row['discovery']['percent_of_misses'])} "
            f"| {_percent(row['intelligence']['percent_of_misses'])} | "
            f"{_percent(row['entry']['percent_of_misses'])} | "
            f"{_percent(row['failure_gate']['percent_of_misses'])} | "
            f"{_percent(row['coverage']['percent_of_misses'])} | "
            f"{_percent(row['state_or_provider']['percent_of_misses'])} |"
        )
    lines.extend(
        [
            "",
            (
                "The dominant measured loss is intelligence: RunnerScore remains below 60 despite "
                "the evidence being present. CHASING is the second-largest decisive loss. Failure "
                "penalties are not the decisive reason for any reconstructed 5x+ miss, although the "
                "failure result is only a lower bound because several live risk inputs are absent."
            ),
            "",
            "## Multi-timestamp runner replay",
            "",
        ]
    )
    for cohort, timestamps in result["timestamp_replay"].items():
        lines.extend(
            [
                f"### {cohort}",
                "",
                (
                    "| Time | Observed | Median score | Core tier | Coverage ≥75 | OPEN | EXTENDED | "
                    "CHASING | UNKNOWN |"
                ),
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for label, row in timestamps.items():
            entry = row["entry_status"]
            lines.append(
                f"| {label} | {row['observed']:,} | {row['median_runner_score']:.2f} | "
                f"{row['core_tier']:,} | {row['coverage_gte_75']:,} | "
                f"{entry.get('OPEN', 0):,} | {entry.get('EXTENDED', 0):,} | "
                f"{entry.get('CHASING', 0):,} | {entry.get('UNKNOWN', 0):,} |"
            )
        lines.append("")
    lines.extend(
        [
            (
                "The earliest aggregate identification point is usually T+60s, not T+3m. Core-tier "
                "counts then decay, while UNKNOWN entry states rise sharply after migration because "
                "the source lacks a unit-consistent SOL/USD call-market-cap bridge."
            ),
            "",
            "## 13–17. Feature diagnostics",
            "",
            (
                "| Rank | Feature | Coverage | 2x effect | 5x effect | 10x effect | 20x effect | "
                "Stability/class |"
            ),
            "|---:|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for index, row in enumerate(result["feature_diagnostics"], 1):
        lines.append(
            f"| {index} | `{row['feature']}` | {_percent(row['coverage'])} | "
            f"{float(row.get('standardized_2x_effect') or 0):.3f} | "
            f"{float(row.get('standardized_5x_effect') or 0):.3f} | "
            f"{float(row.get('standardized_10x_effect') or 0):.3f} | "
            f"{float(row.get('standardized_20x_effect') or 0):.3f} | "
            f"{row['classification']} |"
        )
    lines.extend(
        [
            "",
            (
                "Buyer count/growth are the strongest stable positive right-tail descriptors. Volume "
                "and trade count have positive right-tail effects but reverse direction across weeks. "
                "Corrected concentration score is negatively associated with runners: the current "
                "intuition that lower concentration is always better is misleading in this corpus and "
                "should not be promoted without causal/safety review."
            ),
            "",
            "## 18. Discovery versus intelligence",
            "",
            (
                f"For 5x misses, discovery accounts for "
                f"{_percent(miss_5x['discovery']['percent_of_misses'])}, intelligence/score for "
                f"{_percent(miss_5x['intelligence']['percent_of_misses'])}, entry for "
                f"{_percent(miss_5x['entry']['percent_of_misses'])}, coverage for "
                f"{_percent(miss_5x['coverage']['percent_of_misses'])}, and state/provider gaps for "
                f"{_percent(miss_5x['state_or_provider']['percent_of_misses'])}. The intelligence "
                f"share is {_percent(miss_20x['intelligence']['percent_of_misses'])} at 20x and "
                f"{_percent(miss_50x['intelligence']['percent_of_misses'])} at 50x."
            ),
            "",
            "## 19–20. Replay cards",
            "",
            (
                "Each card contains what the reconstruction knew and decided at every available "
                "timestamp. Full machine-readable cards remain in the JSON evidence artifact."
            ),
            "",
        ]
    )
    for cohort in ("2x", "5x", "10x", "20x", "50x"):
        lines.append(f"### Missed {cohort} examples")
        lines.append("")
        for card in result["missed_runner_cards"].get(cohort, []):
            timeline = "; ".join(
                f"T+{point['timestamp_seconds']}s score={point['runner_score']:.1f} "
                f"entry={point['entry_status']} tier={point['tier']}"
                for point in card["timeline"]
            )
            lines.append(
                f"- `{card['mint']}` — peak {float(card['peak_multiple']):.2f}x; "
                f"miss `{card['miss_reason']}`. {timeline}"
            )
        lines.append("")
    lines.extend(["### False-positive examples", ""])
    for card in result["false_positive_cards"][:20]:
        reference = next(point for point in card["timeline"] if point["timestamp_seconds"] == 180)
        lines.append(
            f"- `{card['mint']}` — peak {float(card['peak_multiple']):.2f}x; T+3m "
            f"score={reference['runner_score']:.1f}, failure="
            f"{reference['failure_score_lower_bound']:.1f}, tier={reference['tier']}."
        )
    lines.extend(
        [
            "",
            "## 21. Diagnostic-window protection",
            "",
            (
                f"Feature ordering was fitted on {result['feature_ranking_fit_window']['rows']:,} rows "
                "from June 5–20. Models were evaluated on "
                f"{result['model_diagnostic_window']['rows']:,} later rows from July 5–13. Both are "
                "retired diagnostics; neither is sealed or eligible for approval."
            ),
            "",
            "## 22–25. Experimental model comparison",
            "",
            (
                "All models emit the top 1% of the identical corrected, pre-graduation SOL-denominated "
                "diagnostic universe."
            ),
            "",
            "| Model | 2x | 3x | 5x | 10x | 20x recall | 50x recall | Failure |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for name, row in result["models"].items():
        lines.append(
            f"| {name} | {_percent(row['2x_precision'])} | {_percent(row['3x_precision'])} | "
            f"{_percent(row['5x_precision'])} | {_percent(row['10x_precision'])} | "
            f"{_percent(row['20x_recall'])} | {_percent(row['50x_recall'])} | "
            f"{_percent(row['failure_rate'])} |"
        )
    lines.extend(
        [
            "",
            (
                f"No experiment is approvable. `{best_2x[0]}` has the highest corrected 2x precision "
                f"({_percent(best_2x[1]['2x_precision'])}); `{best_5x[0]}` has the highest 5x "
                f"precision ({_percent(best_5x[1]['5x_precision'])}); and `{best_10x[0]}` has the "
                f"highest 10x precision ({_percent(best_10x[1]['10x_precision'])}). Market-cap "
                "priors dominate the corrected right tail, while the observable CONTROL mean remains "
                "strongest for 2x. The objectives conflict, so a single uncalibrated mean dilutes "
                "cohort-specific evidence."
            ),
            "",
            "## Market-cap prior and sweet spots",
            "",
            "| T+3m MC (SOL) | Population | 2x | 5x | 10x | 20x | 50x | Failure | Median MAE |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in result["market_cap_buckets"]:
        lines.append(
            f"| {row['bucket_sol']} | {row['population']:,} | {_percent(row['2x_rate'])} | "
            f"{_percent(row['5x_rate'])} | {_percent(row['10x_rate'])} | "
            f"{_percent(row['20x_rate'])} | {_percent(row['50x_rate'])} | "
            f"{_percent(row['failure_rate'])} | "
            f"{float(row.get('median_max_adverse_excursion') or 0):.3f} |"
        )
    lines.extend(
        [
            "",
            (
                f"The <1 SOL revival/extreme-low bucket is the strongest right-tail prior but carries "
                f"{_percent(extreme_low['failure_rate'])} terminal failure. The 250–1000 SOL bucket "
                f"has {_percent(overextended['2x_rate'])} 2x but "
                f"{_percent(overextended['failure_rate'])} failure and a "
                f"{float(overextended.get('median_max_adverse_excursion') or 0):.3f} median adverse "
                "excursion. Both are high-risk specialist regimes, not general PREMIUM evidence. The "
                "20–30 SOL mass is a dead zone."
            ),
            "",
            "## Runner-score calibration",
            "",
            "| Score bucket | Sample | 2x | 5x | 10x | 20x |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in result["runner_score_calibration"]:
        lines.append(
            f"| {row['bucket']} | {row['sample']:,} | {_percent(row['2x_rate'])} | "
            f"{_percent(row['5x_rate'])} | {_percent(row['10x_rate'])} | "
            f"{_percent(row['20x_rate'])} |"
        )
    lines.extend(
        [
            "",
            (
                "The score is not calibrated: most rows collapse into 50–60, and the tiny 20–30 "
                "bucket has much higher outcomes than adjacent buckets. Thresholds 60 and 75 separate "
                "some 2x probability, but not a monotonic calibrated probability scale."
            ),
            "",
            "## Failure-score calibration and penalty effectiveness",
            "",
            "| Failure score | Sample | 2x | Terminal failure |",
            "|---|---:|---:|---:|",
        ]
    )
    for row in result["failure_score_calibration"]:
        lines.append(
            f"| {row['bucket']} | {row['sample']:,} | {_percent(row['2x_rate'])} | "
            f"{_percent(row['terminal_failure_rate'])} |"
        )
    lines.extend(
        [
            "",
            "| Observable penalty | Sample | 2x | 5x | Terminal failure |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for name, row in result["penalty_effectiveness"].items():
        lines.append(
            f"| {name} | {row['sample']:,} | {_percent(row['2x_rate'])} | "
            f"{_percent(row['5x_rate'])} | {_percent(row['terminal_failure_rate'])} |"
        )
    lines.extend(
        [
            "",
            (
                "The observed failure score is a lower bound. Missing sell-restriction, connected "
                "cluster, terminal-safety and liquidity-deterioration histories prevent a definitive "
                "claim that the live FailureScore is or is not killing runners."
            ),
            "",
            "## Buyer, creator, wallet and interaction findings",
            "",
            (
                "- Buyer count/growth/acceleration are observed and highly informative; retention, "
                "seller replacement, actor independence and cluster concentration are not."
            ),
            (
                "- The live 85/55/20 buyer compression discards magnitude and acceleration. Buyer "
                "count and growth show strong positive 5x/10x/right-tail descriptive effects."
            ),
            (
                "- Source-reported point-in-time creator counts are positive descriptively, but no "
                "aligned funder history or point-in-time wallet-quality vector exists for control tests."
            ),
            (
                "- No tested interaction grid beats the additive observable CONTROL mean. Feature "
                "interactions are therefore not shown necessary by this evidence."
            ),
            "",
            "## Stage and regime results",
            "",
            (
                "NEW has higher reconstructed core precision than BONDING; MIGRATED has no public-core "
                "signals because unit-consistent entry evidence is missing. Weekly precision varies "
                "materially, confirming regime instability. SOL-volatility segmentation is unavailable "
                "and was not fabricated; launch-intensity results are present in the JSON artifact."
            ),
            "",
            "## 26. Intelligence-path code audit",
            "",
            (
                "1. RunnerScore is the unweighted mean of whatever numeric stage features happen to be "
                "known; missingness silently changes feature weights per token."
            ),
            "2. Market cap is absent from RunnerScore and only influences entry/payoff indirectly.",
            (
                "3. `survival_engine` can emit score 100 with fewer than three known inputs even while "
                "grading the same evidence merely ACCEPTABLE."
            ),
            "4. `migration_continuity` is always populated as `None` in the live service.",
            (
                "5. `liquidity_deterioration` has a failure weight but is never populated by the live "
                "feature builder."
            ),
            "6. Buyer trajectory compresses evidence to 85/55/20, losing magnitude and timing.",
            (
                "7. The autopsy itself found and rejected the prior corrupted peak field, salted random "
                "ranking, same-window feature ordering, and non-causal miss precedence before reporting."
            ),
            "",
            (
                "No live scoring change was made: changing these items would alter frozen CONTROL and "
                "requires later independent validation."
            ),
            "",
            "## 27. Evidence-backed proposed fixes",
            "",
            (
                "1. **Replace outcome plumbing first.** Use the dimensionless PIT outcome builder; risk "
                "is label drift; validate against later seven-day data and manual path samples."
            ),
            (
                "2. **Make market-cap regime explicit.** Separate extreme-low revival/high-risk and "
                "overextended specialist paths; risk is rug concentration; require terminal-failure and "
                "drawdown gates on a new holdout."
            ),
            (
                "3. **Preserve raw buyer dimensions.** Test buyer count, growth and acceleration instead "
                "of 85/55/20; risk is provider/gameability drift; validate chronologically by regime."
            ),
            (
                "4. **Calibrate per cohort.** Separate QUICK_2X, MID_5X and RIGHT_TAIL_20X objectives; "
                "risk is signal fragmentation; compare at fixed frequency on later data."
            ),
            (
                "5. **Align survival score with evidence confidence.** Do not award 100 for sparse "
                "acceptable evidence; risk is reduced recall; ablate prospectively."
            ),
            (
                "6. **Acquire missing vectors.** Production DB, aligned wallet/funder/cluster/liquidity and "
                "seven-day outcomes are prerequisites for a definitive failure-gate autopsy."
            ),
            "",
            "## 28–30. Best model, tests and final truth",
            "",
            (
                f"- Best corrected diagnostic 2x precision: "
                f"**{_percent(best_2x[1]['2x_precision'])}** (`{best_2x[0]}`)."
            ),
            (
                f"- Best corrected diagnostic 5x precision: "
                f"**{_percent(best_5x[1]['5x_precision'])}** (`{best_5x[0]}`)."
            ),
            (
                f"- Best corrected diagnostic 10x precision: "
                f"**{_percent(best_10x[1]['10x_precision'])}** (`{best_10x[0]}`)."
            ),
            (
                f"- Best 20x recall: **{_percent(best_20x[1]['20x_recall'])}** "
                f"(`{best_20x[0]}`); best 50x recall: "
                f"**{_percent(best_50x[1]['50x_recall'])}** (`{best_50x[0]}`)."
            ),
            "- Approved features: **0**. Challenger decisions: **0**.",
            "",
            "### Final truth",
            "",
            "- **DO WE KNOW WHY JR MISSES RUNNERS? YES**, within the observable reconstruction.",
            "- **PRIMARY FAILURE SOURCE: INTELLIGENCE**, followed by ENTRY.",
            "- **IS CURRENT RUNNER SCORE STRUCTURALLY FLAWED? YES.**",
            (
                "- **IS MARKET CAP BEING UNDERUSED? YES**, but market-cap-only is not the corrected best "
                "2x model and its strongest regimes are high failure-risk."
            ),
            (
                "- **IS FAILURE SCORE KILLING GOOD RUNNERS? INCONCLUSIVE.** Observable penalties are not "
                "the decisive 5x+ miss, but the exact live risk vector is unavailable."
            ),
            "- **ARE FEATURE INTERACTIONS NECESSARY? NO evidence of necessity.**",
            "- **DOES ANY EXPERIMENT BEAT MARKET-CAP-ONLY? YES**, on retired diagnostics only.",
            "- **PRODUCTION READY: NO.**",
            "",
        ]
    )
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class CorpusPaths:
    root: Path

    @property
    def corpus(self) -> Path:
        return self.root / "Pumpfun_Memecoin_Corpus"

    def file(self, name: str) -> str:
        return str(self.corpus / name)


class RunnerAutopsy:
    """Reproducible, diagnostic-only runner attrition analysis."""

    def __init__(self, database: str | Path, corpus_root: str | Path):
        try:
            import duckdb
        except ImportError as error:  # pragma: no cover - exercised by research environment only
            raise RuntimeError("install the research extra: pip install -e .[research]") from error
        self.paths = CorpusPaths(Path(corpus_root))
        self.connection = duckdb.connect(str(database))
        spill = Path(database).with_name("runner-autopsy-spill")
        spill.mkdir(parents=True, exist_ok=True)
        self.connection.execute("SET threads=2")
        self.connection.execute("SET preserve_insertion_order=false")
        self.connection.execute("SET memory_limit='8GB'")
        self.connection.execute("SET temp_directory=?", [str(spill)])

    def close(self) -> None:
        self.connection.close()

    def build_replay_table(self) -> None:
        targets = ",".join(f"('{seconds}s',{seconds})" for seconds in AUTOPSY_TIMESTAMPS)
        self.connection.execute(
            """
            CREATE OR REPLACE TABLE _autopsy_snapshot_base AS
          SELECT s.mint,s.bucket_start,s.market_cap_sol_eob,s.price_close,
            s.curve_pct_depleted_eob,s.trade_count,s.buy_count,s.sell_count,
            s.buy_volume_sol,s.sell_volume_sol,s.buy_pressure,s.price_return_pct,
            t.detected_at, t.graduated_at, t.creator,
            t.creator_past_tokens, t.creator_past_rugs, t.initial_market_cap_sol,
            t.initial_top10_pct_corrected, t.dev_buy_pct_corrected,
            date_diff('second', t.detected_at, s.bucket_start) age_seconds,
            sum(s.trade_count) OVER w cumulative_trades,
            sum(s.buy_count) OVER w cumulative_buys,
            sum(s.sell_count) OVER w cumulative_sells,
            sum(s.buy_volume_sol) OVER w cumulative_buy_volume,
            sum(s.sell_volume_sol) OVER w cumulative_sell_volume
          FROM read_parquet(?) s JOIN read_parquet(?) t USING(mint)
          WHERE date_diff('second', t.detected_at, s.bucket_start)<=3600
          WINDOW w AS (PARTITION BY s.mint ORDER BY s.bucket_start)
            """,
            [self.paths.file("snapshots.parquet"), self.paths.file("tokens.parquet")],
        )
        self.connection.execute(
            f"""
            CREATE OR REPLACE TABLE _autopsy_snapshot_at AS
            WITH targets(label,seconds) AS (VALUES {targets}),
            token_targets AS (
              SELECT DISTINCT mint,seconds FROM _autopsy_snapshot_base CROSS JOIN targets
            )
          SELECT b.*,tt.seconds target_seconds,
            tt.seconds-b.age_seconds snapshot_staleness_seconds
          FROM token_targets tt ASOF LEFT JOIN _autopsy_snapshot_base b
            ON tt.mint=b.mint AND tt.seconds>=b.age_seconds
            """
        )
        self.connection.execute(
            """
            CREATE OR REPLACE TABLE _autopsy_pre_future_base AS
          SELECT s.mint,
            date_diff('second',t.detected_at,s.bucket_start) age_seconds,
            max(s.market_cap_sol_eob) OVER (
              PARTITION BY s.mint ORDER BY s.bucket_start
              ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING
            ) future_peak_market_cap_sol,
            last_value(s.market_cap_sol_eob IGNORE NULLS) OVER (
              PARTITION BY s.mint ORDER BY s.bucket_start
              ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING
            ) graduation_market_cap_sol
          FROM read_parquet(?) s JOIN read_parquet(?) t USING(mint)
          WHERE t.graduated_at IS NULL OR s.bucket_start<=t.graduated_at
            """,
            [self.paths.file("snapshots.parquet"), self.paths.file("tokens.parquet")],
        )
        self.connection.execute(
            f"""
            CREATE OR REPLACE TABLE _autopsy_pre_future AS
            WITH targets(label,seconds) AS (VALUES {targets}),
            token_targets AS (
              SELECT DISTINCT mint,seconds FROM _autopsy_snapshot_base CROSS JOIN targets
            )
          SELECT b.mint,tt.seconds target_seconds,b.future_peak_market_cap_sol,
            b.graduation_market_cap_sol
          FROM token_targets tt ASOF LEFT JOIN _autopsy_pre_future_base b
            ON tt.mint=b.mint AND tt.seconds<=b.age_seconds
            """
        )
        self.connection.execute(
            """
            CREATE OR REPLACE TABLE _autopsy_post_base AS
          SELECT s.mint,s.seconds_since_graduation,s.price_usd,s.market_cap_usd,
            s.liquidity_usd,s.buy_pressure_1h,t.detected_at,t.graduated_at,
            coalesce(t.seconds_to_graduation,
              date_diff('second',t.detected_at,t.graduated_at)) + s.seconds_since_graduation
              age_seconds
          FROM read_parquet(?) s JOIN read_parquet(?) t USING(mint)
          WHERE NOT s.incomplete_data
            """,
            [
                self.paths.file("postgard_snapshots.parquet"),
                self.paths.file("tokens.parquet"),
            ],
        )
        self.connection.execute(
            f"""
            CREATE OR REPLACE TABLE _autopsy_post_at AS
            WITH targets(label,seconds) AS (VALUES {targets}),
            token_targets AS (
              SELECT DISTINCT mint,seconds FROM _autopsy_post_base CROSS JOIN targets
            )
          SELECT b.*,tt.seconds target_seconds,
            tt.seconds-b.age_seconds snapshot_staleness_seconds
          FROM token_targets tt ASOF LEFT JOIN _autopsy_post_base b
            ON tt.mint=b.mint AND tt.seconds>=b.age_seconds
            """
        )
        self.connection.execute(
            f"""
            CREATE OR REPLACE TABLE _autopsy_post_future AS
            WITH targets(label,seconds) AS (VALUES {targets})
          SELECT b.mint,x.seconds target_seconds,max(b.price_usd) future_peak_price
          FROM _autopsy_post_base b CROSS JOIN targets x WHERE b.age_seconds>=x.seconds
          GROUP BY b.mint,x.seconds
            """
        )
        self.connection.execute(
            """
            CREATE OR REPLACE TABLE _autopsy_buyers AS
          SELECT mint,seconds,buyer_count,
            buyer_count-lag(buyer_count) OVER(PARTITION BY mint ORDER BY seconds) buyer_growth,
            buyer_count-2*lag(buyer_count) OVER(PARTITION BY mint ORDER BY seconds)
              +lag(buyer_count,2) OVER(PARTITION BY mint ORDER BY seconds) buyer_acceleration
          FROM (
            SELECT mint, seconds, buyer_count FROM buyer_trajectory,
            LATERAL (VALUES
              (30,buyers_30s),(60,buyers_1m),(180,buyers_3m),(300,buyers_5m),
              (600,buyers_10m),(1800,buyers_30m),(3600,buyers_1h)
            ) v(seconds,buyer_count)
          )
            """
        )
        self.connection.execute(
            """
            CREATE OR REPLACE TABLE _autopsy_base AS
          SELECT s.mint,s.detected_at,s.target_seconds timestamp_seconds,
            s.detected_at + s.target_seconds*INTERVAL 1 SECOND decision_at,
            CASE WHEN p.price_usd IS NOT NULL THEN 'MIGRATED'
                 WHEN coalesce(s.curve_pct_depleted_eob,0)<25 THEN 'NEW'
                 ELSE 'BONDING' END stage,
            CASE WHEN p.price_usd IS NOT NULL THEN p.market_cap_usd
                 ELSE s.market_cap_sol_eob END current_market_cap,
            CASE WHEN p.price_usd IS NOT NULL THEN 'USD' ELSE 'SOL' END market_cap_unit,
            CASE WHEN p.price_usd IS NOT NULL THEN p.price_usd ELSE s.price_close END current_price,
            s.snapshot_staleness_seconds,
            s.curve_pct_depleted_eob curve_progress,
            s.cumulative_trades trade_count,s.cumulative_buys buy_count,
            s.cumulative_sells sell_count,s.cumulative_buy_volume buy_volume,
            s.cumulative_sell_volume sell_volume,s.buy_pressure,s.price_return_pct,
            b.buyer_count,b.buyer_growth,b.buyer_acceleration,
            s.creator_past_tokens,s.creator_past_rugs,s.initial_top10_pct_corrected,
            s.dev_buy_pct_corrected,p.liquidity_usd,p.buy_pressure_1h,
            CASE WHEN p.price_usd IS NOT NULL THEN NULL
                 ELSE s.market_cap_sol_eob/nullif(s.initial_market_cap_sol,0) END market_cap_growth,
            CASE WHEN p.price_usd IS NOT NULL THEN
                   greatest(1,pf.future_peak_price/nullif(p.price_usd,0))
                 ELSE greatest(1,
                   sf.future_peak_market_cap_sol/nullif(s.market_cap_sol_eob,0),
                   CASE WHEN s.graduated_at>=
                       s.detected_at+s.target_seconds*INTERVAL 1 SECOND
                     AND o.price_at_grad_usd>0 AND o.peak_price_at>=
                       s.detected_at+s.target_seconds*INTERVAL 1 SECOND
                     THEN sf.graduation_market_cap_sol
                       /nullif(s.market_cap_sol_eob,0)
                       *o.peak_price_usd/o.price_at_grad_usd ELSE 1 END)
                 END peak_multiple,
            coalesce(o.rug_detected,false)
              OR o.outcome_label IN ('dead','pump_dump','slow_bleed') terminal_failure,
            o.outcome_label,
            s.initial_market_cap_sol,s.graduated_at
          FROM _autopsy_snapshot_at s
          LEFT JOIN _autopsy_post_at p ON p.mint=s.mint AND p.target_seconds=s.target_seconds
          LEFT JOIN _autopsy_post_future pf
            ON pf.mint=s.mint AND pf.target_seconds=s.target_seconds
          LEFT JOIN _autopsy_pre_future sf
            ON sf.mint=s.mint AND sf.target_seconds=s.target_seconds
          LEFT JOIN _autopsy_buyers b ON b.mint=s.mint AND b.seconds=s.target_seconds
          LEFT JOIN read_parquet(?) o ON o.mint=s.mint
            """,
            [self.paths.file("postgard_outcomes.parquet")],
        )
        self.connection.execute(
            """
            CREATE OR REPLACE TABLE runner_autopsy_replay AS
          SELECT *,ln(1+greatest(current_market_cap,0)) log_market_cap,
            least(100,greatest(0,50
              +25*ln(greatest(coalesce(market_cap_growth,1),.01))/ln(2)
              +30*(coalesce(buy_pressure,buy_pressure_1h,.5)-.5))) momentum_score,
            ln(1+greatest(trade_count,0)) log_trade_count,
            ln(1+greatest(buy_volume,0)) log_buy_volume,
            ln(1+greatest(coalesce(buyer_count,0),0)) log_buyer_count,
            least(100,greatest(0,50+10*coalesce(buyer_growth,0)
              +5*coalesce(buyer_acceleration,0))) buyer_growth_score,
            CASE WHEN creator_past_tokens IS NULL THEN NULL
              WHEN creator_past_tokens<=0 THEN 50
              ELSE 100*(1-least(1,creator_past_rugs/creator_past_tokens)) END creator_score,
            CASE WHEN initial_top10_pct_corrected IS NULL THEN NULL
              ELSE least(100,greatest(0,100-initial_top10_pct_corrected)) END concentration_score,
            CASE WHEN liquidity_usd IS NULL THEN NULL
              ELSE least(100,greatest(0,liquidity_usd/250)) END liquidity_score,
            CASE WHEN liquidity_usd IS NULL THEN NULL WHEN liquidity_usd<=0 THEN 20
              WHEN 1000/(liquidity_usd/2+1000)<=.05 THEN 90
              WHEN 1000/(liquidity_usd/2+1000)<=.15 THEN 55 ELSE 20 END tradeability_score,
            CASE WHEN creator_past_tokens>=3
              AND creator_past_rugs/nullif(creator_past_tokens,0)>=.5 THEN 56
              WHEN initial_top10_pct_corrected>=60 THEN 56 ELSE 100 END survival_score,
            CASE WHEN market_cap_growth>=3 THEN 15 WHEN market_cap_growth>=1.7 THEN 30
              WHEN market_cap_growth IS NULL THEN NULL ELSE 70 END payoff_score,
            initial_top10_pct_corrected IS NULL concentration_unknown,
            creator_past_tokens>=3
              AND creator_past_rugs/nullif(creator_past_tokens,0)>=.5 toxic_creator,
            stage='MIGRATED' AND coalesce(liquidity_usd,0)<5000 poor_tradeability,
            coalesce(buyer_growth,0)<=0 AND coalesce(sell_count,0)>coalesce(buy_count,0)
              buyer_collapse_proxy,
            false liquidity_deterioration,
            false critical_unknown,
            false provider_conflict,
            true discovered,
            snapshot_staleness_seconds<=60 discovered_early,
            current_market_cap IS NOT NULL AND current_market_cap>0 evaluated,
            CASE WHEN market_cap_growth>0 THEN
              (market_cap_growth-lag(market_cap_growth) OVER(
                PARTITION BY mint ORDER BY timestamp_seconds))
              /nullif(lag(market_cap_growth) OVER(
                PARTITION BY mint ORDER BY timestamp_seconds),0)
              ELSE NULL END vertical_acceleration,
            strftime(decision_at,'%G-W%V') week_label,
            strftime(decision_at,'%Y-%m-%d') day_label
          FROM _autopsy_base
            """
        )
        for table in (
            "_autopsy_snapshot_base",
            "_autopsy_snapshot_at",
            "_autopsy_pre_future_base",
            "_autopsy_pre_future",
            "_autopsy_post_base",
            "_autopsy_post_at",
            "_autopsy_post_future",
            "_autopsy_buyers",
            "_autopsy_base",
        ):
            self.connection.execute(f"DROP TABLE {table}")

    def rows(self, where: str = "timestamp_seconds=180") -> list[dict[str, Any]]:
        cursor = self.connection.execute(
            f"SELECT * FROM runner_autopsy_replay WHERE {where} ORDER BY mint,timestamp_seconds"
        )
        names = [column[0] for column in cursor.description]
        result = []
        for raw in cursor.fetchall():
            row = dict(zip(names, raw, strict=True))
            row.update(reconstruct_decision(row))
            result.append(row)
        return result

    @staticmethod
    def _model_scores(rows: Sequence[Mapping[str, Any]], features: Sequence[str]) -> dict[str, Any]:
        diagnostics = feature_diagnostics(rows, features)
        directions = {
            row["feature"]: 1 if float(row["standardized_2x_effect"] or 0) >= 0 else -1
            for row in diagnostics
        }
        scales = {}
        for feature in features:
            values = [_number(row.get(feature)) for row in rows]
            known = [value for value in values if value is not None]
            scales[feature] = (
                statistics.mean(known) if known else 0,
                statistics.pstdev(known) or 1 if known else 1,
            )

        def normalized(row: Mapping[str, Any], selected: Sequence[str]) -> float | None:
            contributions = []
            for feature in selected:
                value = _number(row.get(feature))
                if value is not None:
                    center, scale = scales[feature]
                    contributions.append(directions[feature] * (value - center) / scale)
            return _mean(contributions)

        ranked = [row["feature"] for row in diagnostics]
        return {
            "diagnostics": diagnostics,
            "directions": directions,
            "ranked": ranked,
            "normalized": normalized,
        }

    def run(self) -> dict[str, Any]:
        reference = self.rows()
        valid = [
            row
            for row in reference
            if row["evaluated"]
            and not bool(
                row.get("initial_top10_pct_corrected") is not None
                and float(row["initial_top10_pct_corrected"]) > 100
            )
            and 0.01 <= float(row.get("current_market_cap") or 0) <= 1_000_000
            and row.get("peak_multiple") is not None
        ]
        tables = {row[0] for row in self.connection.execute("SHOW TABLES").fetchall()}
        if "edge_3m" in tables:
            drawdowns = dict(
                self.connection.execute("SELECT mint,max_adverse_excursion FROM edge_3m").fetchall()
            )
        else:
            drawdowns = {}
        for row in valid:
            row["max_adverse_excursion"] = drawdowns.get(str(row["mint"]))
        model_universe = [row for row in valid if row["market_cap_unit"] == "SOL"]
        train = [
            row
            for row in model_universe
            if "2026-06-05" <= str(row["decision_at"])[:10] < "2026-06-21"
        ]
        diagnostic_test = [
            row
            for row in model_universe
            if "2026-07-05" <= str(row["decision_at"])[:10] < "2026-07-14"
        ]
        feature_model = self._model_scores(train, FEATURES)
        descriptive_features = feature_diagnostics(valid, FEATURES)
        ranked = feature_model["ranked"]
        normalized = feature_model["normalized"]
        effects = {
            row["feature"]: abs(float(row["standardized_2x_effect"] or 0))
            for row in feature_model["diagnostics"]
        }

        def weighted(row: Mapping[str, Any], selected: Sequence[str]) -> float | None:
            contributions = []
            weights = []
            for feature in selected:
                value = normalized(row, [feature])
                weight = effects.get(feature, 0)
                if value is not None and weight > 0:
                    contributions.append(value * weight)
                    weights.append(weight)
            return sum(contributions) / sum(weights) if weights else None

        models = {
            "RANDOM": rank_model(diagnostic_test, stable_random_score),
            "MARKET_CAP_ONLY": rank_model(
                diagnostic_test, lambda row: -float(row["log_market_cap"])
            ),
            "VOLUME_ONLY": rank_model(diagnostic_test, lambda row: row.get("log_buy_volume")),
            "MOMENTUM_ONLY": rank_model(diagnostic_test, lambda row: row.get("momentum_score")),
            "SAFETY_FILTERED_MOMENTUM": rank_model(
                diagnostic_test,
                lambda row: (
                    row.get("momentum_score")
                    if float(row.get("failure_score_lower_bound") or 0) < 40
                    else None
                ),
            ),
            "CONTROL_AVAILABLE_MEAN": rank_model(
                diagnostic_test, lambda row: row.get("runner_score")
            ),
            "WEIGHTED_FEATURES": rank_model(diagnostic_test, lambda row: weighted(row, ranked)),
        }
        for count in (1, 2, 3, 5, len(ranked)):
            selected = ranked[:count]
            models[f"TOP_{count}_FEATURES"] = rank_model(
                diagnostic_test, lambda row, names=selected: normalized(row, names)
            )
        models["MC_PRIOR_PLUS_TOP3"] = rank_model(
            diagnostic_test,
            lambda row: (
                -0.65 * float(row["log_market_cap"])
                + 0.35 * float(normalized(row, ranked[:3]) or 0)
            ),
        )
        interactions = {
            "MC_MOMENTUM": ("log_market_cap", "momentum_score"),
            "MC_BUYER_GROWTH": ("log_market_cap", "buyer_growth_score"),
            "MC_CONCENTRATION": ("log_market_cap", "concentration_score"),
            "MOMENTUM_BUYER_GROWTH": ("momentum_score", "buyer_growth_score"),
            "CREATOR_BUYER_GROWTH": ("creator_score", "buyer_growth_score"),
        }
        for name, (first, second) in interactions.items():
            models[f"INTERACTION_{name}"] = rank_model(
                diagnostic_test, fit_interaction_grid(train, first, second)
            )
        models["CALIBRATED_HISTOGRAM"] = rank_model(
            diagnostic_test, fit_histogram_score(train, ranked[:5])
        )
        stage_models = {
            stage: self._model_scores([row for row in train if row["stage"] == stage], FEATURES)
            for stage in ("NEW", "BONDING", "MIGRATED")
        }

        def stage_score(row: Mapping[str, Any]) -> float | None:
            model = stage_models[str(row["stage"])]
            return model["normalized"](row, model["ranked"][:5])

        models["STAGE_SPECIFIC"] = rank_model(diagnostic_test, stage_score)
        funnels = {f"{threshold}x": cohort_funnel(valid, threshold) for threshold in RUNNER_COHORTS}
        calibration = []
        for lower in range(0, 100, 10):
            bucket = [
                row for row in valid if lower <= float(row.get("runner_score") or 0) < lower + 10
            ]
            calibration.append(
                {
                    "bucket": f"{lower}-{lower + 10}",
                    "sample": len(bucket),
                    **{
                        f"{threshold}x_rate": _rate(
                            [float(row.get("peak_multiple") or 0) >= threshold for row in bucket]
                        )
                        for threshold in (2, 5, 10, 20)
                    },
                }
            )
        mc_rows = sorted(model_universe, key=lambda row: float(row["current_market_cap"]))
        mc_buckets = []
        boundaries = (0.01, 1, 5, 10, 20, 30, 50, 100, 250, 1000, float("inf"))
        for lower, upper in pairwise(boundaries):
            bucket = [row for row in mc_rows if lower <= float(row["current_market_cap"]) < upper]
            if not bucket:
                continue
            mc_buckets.append(
                {
                    "bucket_sol": f"{lower:g}-{upper:g}"
                    if math.isfinite(upper)
                    else f">={lower:g}",
                    "population": len(bucket),
                    "market_cap_min": min(float(row["current_market_cap"]) for row in bucket),
                    "market_cap_max": max(float(row["current_market_cap"]) for row in bucket),
                    **{
                        f"{threshold}x_rate": _rate(
                            [float(row.get("peak_multiple") or 0) >= threshold for row in bucket]
                        )
                        for threshold in RUNNER_COHORTS
                    },
                    "failure_rate": _rate([bool(row["terminal_failure"]) for row in bucket]),
                    "median_max_adverse_excursion": statistics.median(
                        [
                            float(row["max_adverse_excursion"])
                            for row in bucket
                            if row.get("max_adverse_excursion") is not None
                        ]
                    )
                    if any(row.get("max_adverse_excursion") is not None for row in bucket)
                    else None,
                }
            )
        stage_results = {}
        for stage in ("NEW", "BONDING", "MIGRATED"):
            cohort = [row for row in valid if row["stage"] == stage]
            stage_results[stage] = selection_metrics(
                [row for row in cohort if row["tier"] in CORE_TIERS], cohort
            )
        regime_results = {}
        by_week: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in valid:
            by_week[str(row["week_label"])].append(row)
        for week, cohort in sorted(by_week.items()):
            regime_results[week] = selection_metrics(
                [row for row in cohort if row["tier"] in CORE_TIERS], cohort
            )
        daily_results = {}
        by_day: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in valid:
            by_day[str(row["day_label"])].append(row)
        for day, cohort in sorted(by_day.items()):
            daily_results[day] = selection_metrics(
                [row for row in cohort if row["tier"] in CORE_TIERS], cohort
            )
        daily_counts = sorted(len(cohort) for cohort in by_day.values())
        low_cut = daily_counts[len(daily_counts) // 3]
        high_cut = daily_counts[len(daily_counts) * 2 // 3]
        intensity: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for cohort in by_day.values():
            name = "LOW" if len(cohort) <= low_cut else "HIGH" if len(cohort) >= high_cut else "MID"
            intensity[name].extend(cohort)
        launch_intensity_results = {
            name: {
                "launches": len(cohort),
                **selection_metrics([row for row in cohort if row["tier"] in CORE_TIERS], cohort),
            }
            for name, cohort in sorted(intensity.items())
        }
        false_negatives = {
            f"{threshold}x": dict(
                Counter(
                    miss_reason(row)
                    for row in valid
                    if float(row.get("peak_multiple") or 0) >= threshold
                    and row.get("tier") not in CORE_TIERS
                )
            )
            for threshold in (5, 10, 20, 50)
        }
        miss_attribution = {}
        for threshold in RUNNER_COHORTS:
            misses = [
                row
                for row in valid
                if float(row.get("peak_multiple") or 0) >= threshold
                and row.get("tier") not in CORE_TIERS
            ]
            grouped = Counter(miss_reason(row) for row in misses)
            categories = {
                "discovery": grouped["NOT_DISCOVERED"] + grouped["DISCOVERED_TOO_LATE"],
                "intelligence": grouped["LOW_RUNNER_SCORE"] + grouped["TIER_THRESHOLD"],
                "entry": grouped["ENTRY_EXTENDED"] + grouped["ENTRY_CHASING"],
                "failure_gate": sum(
                    grouped[name]
                    for name in (
                        "FAILURE_SCORE_TOO_HIGH",
                        "CREATOR_PENALTY",
                        "CONCENTRATION_PENALTY",
                        "BUYER_COLLAPSE",
                        "LIQUIDITY_FILTER",
                    )
                ),
                "coverage": grouped["LOW_COVERAGE"] + grouped["CRITICAL_UNKNOWN"],
                "state_or_provider": grouped["STATE/LIFECYCLE ISSUE"]
                + grouped["PROVIDER_CONFLICT"]
                + grouped["OTHER"],
            }
            miss_attribution[f"{threshold}x"] = {
                "missed": len(misses),
                **{
                    name: {
                        "count": count,
                        "percent_of_misses": count / len(misses) if misses else None,
                    }
                    for name, count in categories.items()
                },
            }
        failure_suppression = {}
        for threshold in RUNNER_COHORTS:
            blocked = [
                row
                for row in valid
                if float(row.get("peak_multiple") or 0) >= threshold
                and float(row.get("failure_score_lower_bound") or 0) >= 40
            ]
            failure_suppression[f"{threshold}x"] = {
                "blocked": len(blocked),
                "reasons": dict(
                    Counter(
                        reason for row in blocked for reason in row.get("failure_reasons") or []
                    )
                ),
            }
        failure_calibration = []
        for lower in range(0, 100, 20):
            bucket = [
                row
                for row in valid
                if lower <= float(row.get("failure_score_lower_bound") or 0) < lower + 20
            ]
            failure_calibration.append(
                {
                    "bucket": f"{lower}-{lower + 20}",
                    "sample": len(bucket),
                    "2x_rate": _rate([float(row.get("peak_multiple") or 0) >= 2 for row in bucket]),
                    "terminal_failure_rate": _rate(
                        [bool(row.get("terminal_failure")) for row in bucket]
                    ),
                }
            )
        penalty_effectiveness = {}
        for reason in (
            "CONCENTRATION_UNKNOWN",
            "TOXIC_CREATOR",
            "POOR_TRADEABILITY",
            "BUYER_COLLAPSE_PROXY",
            "LIQUIDITY_DETERIORATION",
        ):
            cohort = [row for row in valid if reason in (row.get("failure_reasons") or [])]
            penalty_effectiveness[reason] = {
                "sample": len(cohort),
                "2x_rate": _rate([float(row.get("peak_multiple") or 0) >= 2 for row in cohort]),
                "5x_rate": _rate([float(row.get("peak_multiple") or 0) >= 5 for row in cohort]),
                "terminal_failure_rate": _rate(
                    [bool(row.get("terminal_failure")) for row in cohort]
                ),
            }
        false_positives = [
            row for row in valid if row.get("tier") in CORE_TIERS and row["peak_multiple"] < 2
        ]
        false_positive_causes = {
            "high_momentum": sum(
                float(row.get("momentum_score") or 0) >= 75 for row in false_positives
            ),
            "coarse_buyer_positive": sum(
                float(row.get("buyer_growth_score") or 0) >= 75 for row in false_positives
            ),
            "survival_score_100": sum(
                float(row.get("survival_score") or 0) >= 100 for row in false_positives
            ),
            "payoff_positive": sum(
                float(row.get("payoff_score") or 0) >= 70 for row in false_positives
            ),
        }
        full_history = self.rows(
            "timestamp_seconds=180 OR mint IN (SELECT mint FROM runner_autopsy_replay "
            "WHERE timestamp_seconds=180 AND peak_multiple>=2)"
        )
        reference_by_mint = {str(row["mint"]): row for row in valid}
        timestamp_replay = {}
        for threshold in RUNNER_COHORTS:
            mints = {
                mint
                for mint, row in reference_by_mint.items()
                if float(row.get("peak_multiple") or 0) >= threshold
            }
            timestamp_replay[f"{threshold}x"] = {}
            for seconds in AUTOPSY_TIMESTAMPS:
                cohort = [
                    row
                    for row in full_history
                    if str(row["mint"]) in mints and row["timestamp_seconds"] == seconds
                ]
                timestamp_replay[f"{threshold}x"][f"T+{seconds}s"] = {
                    "observed": len(cohort),
                    "median_runner_score": statistics.median(
                        [float(row["runner_score"]) for row in cohort]
                    )
                    if cohort
                    else None,
                    "core_tier": sum(row["tier"] in CORE_TIERS for row in cohort),
                    "entry_status": dict(Counter(str(row["entry_status"]) for row in cohort)),
                    "coverage_gte_75": sum(float(row["coverage"]) >= 75 for row in cohort),
                }
        missed_cards = {}
        for threshold in (2, 5, 10, 20, 50):
            mints = {
                str(row["mint"])
                for row in valid
                if float(row.get("peak_multiple") or 0) >= threshold
                and row.get("tier") not in CORE_TIERS
            }
            missed_cards[f"{threshold}x"] = replay_cards(
                [row for row in full_history if str(row["mint"]) in mints], 10
            )
        fp_mints = {str(row["mint"]) for row in false_positives}
        fp_cards = replay_cards([row for row in full_history if str(row["mint"]) in fp_mints], 20)
        return {
            "truth_state": "DIAGNOSTIC_ONLY_RETIRED_WINDOWS_NOT_SEALED",
            "reference_timestamp_seconds": 180,
            "source_rows": len(reference),
            "valid_analysis_rows": len(valid),
            "outcome_correction": {
                "label": "DIMENSIONLESS_PIT_REBUILD_V2",
                "prior_peak_field_rejected": "edge_3m.peak_48h",
                "reason": "impossible currency/unit and raw-reserve outliers",
            },
            "funnels": funnels,
            "feature_diagnostics": descriptive_features,
            "feature_ranking_fit_window": {
                "start": "2026-06-05",
                "end_exclusive": "2026-06-21",
                "rows": len(train),
                "ranking": ranked,
            },
            "model_diagnostic_window": {
                "start": "2026-07-05",
                "end_exclusive": "2026-07-14",
                "rows": len(diagnostic_test),
                "state": "RETIRED_DIAGNOSTIC_NOT_SEALED",
                "universe": "PRE_GRADUATION_SOL_DENOMINATED_ONLY",
            },
            "runner_score_calibration": calibration,
            "market_cap_buckets": mc_buckets,
            "stage_results": stage_results,
            "regime_results": regime_results,
            "daily_results": daily_results,
            "launch_intensity_results": launch_intensity_results,
            "regime_unavailable": ["SOL volatility", "external market regime labels"],
            "false_negative_causes": false_negatives,
            "miss_attribution": miss_attribution,
            "failure_suppression_lower_bound": failure_suppression,
            "failure_score_calibration": failure_calibration,
            "penalty_effectiveness": penalty_effectiveness,
            "false_positive_causes": false_positive_causes,
            "false_positive_sample": len(false_positives),
            "models": {name: value["metrics"] for name, value in models.items()},
            "interaction_observability": {
                "tested": interactions,
                "unavailable": [
                    "market_cap+liquidity (pre-graduation liquidity absent)",
                    "liquidity+buyer_quality (pre-graduation liquidity absent)",
                    "creator+funder (aligned funder history absent)",
                    "wallet_quality+buyer_acceleration (PIT wallet quality not aligned)",
                ],
            },
            "missed_runner_cards": missed_cards,
            "false_positive_cards": fp_cards,
            "timestamp_replay": timestamp_replay,
            "buyer_trajectory_observability": {
                "observed": ["distinct buyer count", "buyer growth", "buyer acceleration"],
                "not_observed": [
                    "retention",
                    "seller replacement",
                    "actor independence",
                    "repeat-wallet PIT quality",
                    "cluster concentration",
                ],
            },
            "creator_wallet_funder_observability": {
                "creator": "SOURCE_REPORTED_PIT_COUNTS_AVAILABLE",
                "wallet": "RAW_IDENTITIES_AVAILABLE_BUT_PIT_QUALITY_NOT_ALIGNED_IN_REPLAY",
                "funder": "NO_ALIGNED_PUMPFUN_CORPUS_HISTORY",
            },
            "limitations": [
                "No production Jr database copy or exact historical provider vectors.",
                "CONTROL decisions are observable-field reconstructions, not recorded live decisions.",
                "Failure scores are lower bounds because sell restrictions, clusters and terminal safety are absent.",
                "Pre-graduation liquidity and SOL/USD history are unavailable.",
                "Snapshots are 60-second buckets in the first 30 minutes; T+30s uses T0 state.",
                "Only 48-hour outcomes exist; all June/July windows are retired diagnostics.",
            ],
        }

    def write_report(
        self, output: str | Path, markdown_output: str | Path | None = None
    ) -> dict[str, Any]:
        result = self.run()
        Path(output).write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
        if markdown_output is not None:
            Path(markdown_output).write_text(render_autopsy_markdown(result), encoding="utf-8")
        return result

from __future__ import annotations

import json
import math
import statistics
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from .store import HistoricalWarehouse, _json, _parse_timestamp


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _rate(values: list[bool]) -> float | None:
    return None if not values else sum(values) / len(values)


def _numeric(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


class LeakageError(ValueError):
    pass


class ResearchEngine:
    """Deterministic chronological research with hard leakage rejection."""

    def __init__(self, warehouse: HistoricalWarehouse):
        self.warehouse = warehouse

    @staticmethod
    def validate_windows(
        train: tuple[str, str], validation: tuple[str, str], test: tuple[str, str]
    ) -> None:
        points = [*train, *validation, *test]
        parsed = [_parse_timestamp(value) for value in points]
        if not (
            parsed[0] < parsed[1]
            and parsed[1] <= parsed[2] < parsed[3]
            and parsed[3] <= parsed[4] < parsed[5]
        ):
            raise LeakageError("walk-forward windows must be strictly chronological and disjoint")

    @staticmethod
    def assert_point_in_time(rows: list[dict[str, Any]]) -> None:
        violations = []
        for row in rows:
            decision = _parse_timestamp(row["decision_at"])
            available = _parse_timestamp(row["available_at"])
            observed = _parse_timestamp(row["observed_at"])
            if available > decision or observed > decision:
                violations.append(row.get("feature_id") or row.get("feature_name") or "UNKNOWN")
        if violations:
            raise LeakageError(f"future information detected in {len(violations)} feature rows")

    def dataset_rows(
        self,
        dataset_version: str,
        feature_version: str,
        outcome_version: str,
        start: str,
        end: str,
        chain: str | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT o.outcome_id,o.entity_key,o.decision_at,o.peak_multiple,o.rugged,o.class_name,"
            "e.chain,f.feature_id,f.feature_name,f.feature_value_json,f.observed_at,f.available_at "
            "FROM outcomes o JOIN canonical_entities e ON e.entity_key=o.entity_key "
            "JOIN point_in_time_features f ON f.entity_key=o.entity_key AND f.dataset_version="
            "o.dataset_version WHERE o.dataset_version=? AND o.outcome_version=? AND "
            "f.feature_version=? AND o.decision_at>=? AND o.decision_at<? AND f.observed_at<="
            "o.decision_at AND f.available_at<=o.decision_at"
        )
        parameters: list[Any] = [dataset_version, outcome_version, feature_version, start, end]
        if chain:
            query += " AND e.chain=?"
            parameters.append(chain)
        rows = [dict(row) for row in self.warehouse.conn.execute(query, parameters)]
        for row in rows:
            row["value"] = (
                None
                if row["feature_value_json"] is None
                else json.loads(row["feature_value_json"])
            )
        self.assert_point_in_time(rows)
        return rows

    @staticmethod
    def outcome_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
        outcomes: dict[str, dict[str, Any]] = {}
        for row in rows:
            outcomes[row["outcome_id"]] = row
        unique = list(outcomes.values())
        peaks = [float(row["peak_multiple"] or 0) for row in unique]
        return {
            "sample": len(unique),
            "5x_recall_denominator": sum(peak >= 5 for peak in peaks),
            "10x_recall_denominator": sum(peak >= 10 for peak in peaks),
            "20x_recall_denominator": sum(peak >= 20 for peak in peaks),
            "runner_5x_rate": _rate([peak >= 5 for peak in peaks]),
            "runner_10x_rate": _rate([peak >= 10 for peak in peaks]),
            "runner_20x_rate": _rate([peak >= 20 for peak in peaks]),
            "rug_rate": _rate([bool(row["rugged"]) for row in unique]),
            "median_peak_multiple": statistics.median(peaks) if peaks else None,
        }

    @staticmethod
    def fingerprint_findings(rows: list[dict[str, Any]], threshold: float = 5) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, list[float]]] = defaultdict(
            lambda: {"runner": [], "non_runner": []}
        )
        for row in rows:
            value = _numeric(row.get("value"))
            if value is None:
                continue
            cohort = "runner" if float(row.get("peak_multiple") or 0) >= threshold else "non_runner"
            grouped[row["feature_name"]][cohort].append(value)
        findings = []
        for feature, cohorts in sorted(grouped.items()):
            runners = cohorts["runner"]
            failures = cohorts["non_runner"]
            if not runners or not failures:
                continue
            runner_median = statistics.median(runners)
            other_median = statistics.median(failures)
            findings.append(
                {
                    "feature_name": feature,
                    "runner_sample": len(runners),
                    "non_runner_sample": len(failures),
                    "runner_median": runner_median,
                    "non_runner_median": other_median,
                    "median_effect": runner_median - other_median,
                }
            )
        return findings

    @staticmethod
    def baseline_comparison(rows: list[dict[str, Any]]) -> dict[str, Any]:
        by_outcome: dict[str, dict[str, Any]] = defaultdict(dict)
        outcome_values: dict[str, dict[str, Any]] = {}
        for row in rows:
            by_outcome[row["outcome_id"]][row["feature_name"]] = row.get("value")
            outcome_values[row["outcome_id"]] = row
        selectors = {
            "random": lambda features: 0.5,
            "mc_only": lambda features: _numeric(features.get("market_cap_usd")),
            "volume_only": lambda features: _numeric(features.get("volume_5m_usd")),
            "momentum_only": lambda features: _numeric(features.get("momentum_acceleration")),
            "liquidity_only": lambda features: _numeric(features.get("liquidity_usd")),
            "safety_filtered_momentum": lambda features: (
                _numeric(features.get("momentum_acceleration"))
                if not features.get("terminal_safety_failure")
                else None
            ),
        }
        results = {}
        for name, selector in selectors.items():
            scored = [
                (selector(features), outcome_values[outcome_id])
                for outcome_id, features in by_outcome.items()
            ]
            usable = [(score, outcome) for score, outcome in scored if score is not None]
            usable.sort(key=lambda item: float(item[0]), reverse=True)
            selected = usable[: max(1, len(usable) // 10)]
            results[name] = {
                "sample": len(selected),
                "5x_precision": _rate(
                    [float(outcome.get("peak_multiple") or 0) >= 5 for _, outcome in selected]
                ),
            }
        return results

    @staticmethod
    def ablations(rows: list[dict[str, Any]]) -> dict[str, Any]:
        families = {
            "wallet": ("wallet", "alpha"),
            "creator": ("creator", "deployer"),
            "narrative": ("narrative", "social"),
            "buyer_quality": ("buyer",),
            "runner_fingerprint": ("fingerprint", "similarity"),
            "survival": ("survival",),
            "payoff": ("payoff",),
            "regime": ("regime",),
        }
        return {
            family: {
                "removed_feature_rows": sum(
                    any(marker in row["feature_name"].lower() for marker in markers)
                    for row in rows
                ),
                "requires_prospective_scoring": True,
            }
            for family, markers in families.items()
        }

    def run_walk_forward(
        self,
        *,
        research_type: str,
        dataset_version: str,
        feature_version: str,
        outcome_version: str,
        rules_version: str,
        code_version: str,
        provider_set: list[str],
        train: tuple[str, str],
        validation: tuple[str, str],
        test: tuple[str, str],
        chain: str | None = None,
        limitations: list[str] | None = None,
    ) -> dict[str, Any]:
        self.validate_windows(train, validation, test)
        windows = {
            "train": self.dataset_rows(
                dataset_version, feature_version, outcome_version, *train, chain
            ),
            "validation": self.dataset_rows(
                dataset_version, feature_version, outcome_version, *validation, chain
            ),
            "test": self.dataset_rows(
                dataset_version, feature_version, outcome_version, *test, chain
            ),
        }
        metrics = {name: self.outcome_metrics(rows) for name, rows in windows.items()}
        test_rows = windows["test"]
        result = {
            "fingerprints": self.fingerprint_findings(windows["train"]),
            "baselines": self.baseline_comparison(test_rows),
            "ablations": self.ablations(test_rows),
        }
        run_id = str(uuid.uuid4())
        with self.warehouse._lock, self.warehouse.conn:
            self.warehouse.conn.execute(
                "INSERT INTO research_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    research_type,
                    dataset_version,
                    feature_version,
                    rules_version,
                    code_version,
                    _json(provider_set),
                    chain,
                    train[0],
                    train[1],
                    validation[0],
                    validation[1],
                    test[0],
                    test[1],
                    _json({"split": "strict_chronological_walk_forward"}),
                    _json(metrics),
                    _json(result),
                    _json(limitations or []),
                    None,
                    "PASS",
                    _now(),
                ),
            )
            for finding in result["fingerprints"]:
                self.warehouse.conn.execute(
                    "INSERT INTO research_findings VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        run_id,
                        research_type,
                        finding["feature_name"],
                        "5X_RUNNER_VS_REST",
                        finding["runner_sample"] + finding["non_runner_sample"],
                        _json(finding),
                        _json({"state": "DESCRIPTIVE_NOT_CAUSAL"}),
                        _json(limitations or []),
                    ),
                )
        return {"research_run_id": run_id, "metrics": metrics, "result": result}

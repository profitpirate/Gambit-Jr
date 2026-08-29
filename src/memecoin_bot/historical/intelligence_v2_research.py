from __future__ import annotations

import json
import math
import statistics
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any

from .intelligence_v2 import (
    CONTROL_FREEZE_SHA,
    IDENTIFIER_REGISTRY,
    INTELLIGENCE_V2_VERSION,
    OUTCOME_VERSION,
    IdentifierState,
    IntelligenceV2Research,
)
from .runner_autopsy import reconstruct_decision

FREQUENCIES = (0.001, 0.0025, 0.005, 0.01, 0.02, 0.05)
TIMESTAMPS = (30, 60, 180, 300, 600, 1800)
WINDOWS = {
    "train": ("2026-06-05", "2026-06-16"),
    "validation": ("2026-06-16", "2026-06-21"),
    "retired_diagnostic": ("2026-07-05", "2026-07-14"),
}

FEATURE_GROUPS: dict[str, tuple[str, ...]] = {
    "market_cap": ("log_market_cap", "market_cap_growth", "curve_progress"),
    "buyers": ("log_buyer_count", "buyer_growth", "buyer_acceleration"),
    "momentum": (
        "momentum_score",
        "log_trade_count",
        "log_buy_volume",
        "buy_pressure",
        "price_return_pct",
        "vertical_acceleration",
    ),
    "liquidity": ("liquidity_score", "tradeability_score"),
    "creator": ("creator_score",),
    "wallet": (),
    "funder": (),
    "concentration": ("concentration_score",),
    "regime": ("launch_intensity_percentile",),
    "entry": ("entry_quality_numeric",),
    "survival": ("survival_score",),
    "failure": ("failure_score_lower_bound",),
}

MODEL_FEATURES = {
    "QUICK_2X_V2": (
        "market_cap",
        "buyers",
        "momentum",
        "entry",
        "survival",
        "failure",
        "concentration",
        "regime",
    ),
    "MID_5X_V2": (
        "market_cap",
        "buyers",
        "momentum",
        "liquidity",
        "creator",
        "concentration",
        "entry",
        "survival",
        "failure",
        "regime",
    ),
    "RIGHT_TAIL_V2": (
        "market_cap",
        "buyers",
        "momentum",
        "liquidity",
        "creator",
        "concentration",
        "entry",
        "survival",
        "failure",
        "regime",
    ),
    "BUYER_TRAJECTORY_ONLY": ("buyers",),
    "MARKET_CAP_PLUS_BUYERS": ("market_cap", "buyers"),
    "MARKET_CAP_PLUS_BUYERS_PLUS_LIQUIDITY": ("market_cap", "buyers", "liquidity"),
}

MODEL_TARGETS = {
    "QUICK_2X_V2": 2,
    "MID_5X_V2": 5,
    "RIGHT_TAIL_V2": 20,
    "BUYER_TRAJECTORY_ONLY": 5,
    "MARKET_CAP_PLUS_BUYERS": 5,
    "MARKET_CAP_PLUS_BUYERS_PLUS_LIQUIDITY": 5,
    "SHALLOW_TREE_V2": 5,
    "BOOSTED_TREE_V2": 5,
}


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _features(groups: Sequence[str]) -> tuple[str, ...]:
    return tuple(feature for group in groups for feature in FEATURE_GROUPS[group])


def _rate(values: Sequence[bool]) -> float | None:
    return sum(values) / len(values) if values else None


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def _selection_metrics(
    selected: Sequence[Mapping[str, Any]], universe: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    total_20 = sum(float(row["peak_multiple"]) >= 20 for row in universe)
    total_50 = sum(float(row["peak_multiple"]) >= 50 for row in universe)
    drawdowns = [
        float(row["max_adverse_excursion"])
        for row in selected
        if _number(row.get("max_adverse_excursion")) is not None
        and -1 <= float(row["max_adverse_excursion"]) <= 0
    ]
    return {
        "signals": len(selected),
        "signal_frequency": len(selected) / len(universe) if universe else None,
        **{
            f"{threshold}x_precision": _rate(
                [float(row["peak_multiple"]) >= threshold for row in selected]
            )
            for threshold in (2, 3, 5, 10)
        },
        "20x_recall": (
            sum(float(row["peak_multiple"]) >= 20 for row in selected) / total_20
            if total_20
            else None
        ),
        "50x_recall": (
            sum(float(row["peak_multiple"]) >= 50 for row in selected) / total_50
            if total_50
            else None
        ),
        "failure_rate": _rate([bool(row["terminal_failure"]) for row in selected]),
        "median_drawdown": _median(drawdowns),
    }


def _rank(
    rows: Sequence[Mapping[str, Any]], scores: Sequence[float], frequency: float
) -> tuple[list[Mapping[str, Any]], list[int]]:
    order = sorted(
        range(len(rows)), key=lambda index: (-float(scores[index]), str(rows[index]["mint"]))
    )
    sample = max(1, round(len(rows) * frequency))
    selected_indexes = order[:sample]
    return [rows[index] for index in selected_indexes], selected_indexes


def _calibration(
    probabilities: Sequence[float], labels: Sequence[bool], bins: int = 10
) -> dict[str, Any]:
    if not probabilities:
        return {"brier": None, "ece": None, "reliability": []}
    brier = statistics.fmean(
        (float(probability) - int(label)) ** 2
        for probability, label in zip(probabilities, labels, strict=True)
    )
    reliability = []
    ece = 0.0
    for lower_index in range(bins):
        lower, upper = lower_index / bins, (lower_index + 1) / bins
        indexes = [
            index
            for index, probability in enumerate(probabilities)
            if lower <= probability < upper or (upper == 1 and probability == 1)
        ]
        if not indexes:
            continue
        predicted = statistics.fmean(probabilities[index] for index in indexes)
        observed = statistics.fmean(int(labels[index]) for index in indexes)
        ece += len(indexes) / len(probabilities) * abs(predicted - observed)
        reliability.append(
            {
                "lower": lower,
                "upper": upper,
                "sample": len(indexes),
                "predicted": predicted,
                "observed": observed,
            }
        )
    return {"brier": brier, "ece": ece, "reliability": reliability}


@dataclass(slots=True)
class CalibratedCandidate:
    name: str
    objective_threshold: int
    features: tuple[str, ...]
    estimator: Any
    calibrator: Any
    fit_rows: int
    validation_rows: int

    def score_outputs(self, rows: Sequence[Mapping[str, Any]]) -> tuple[list[float], list[float]]:
        import numpy as np

        matrix = np.asarray(
            [
                [
                    float(row[feature]) if _number(row.get(feature)) is not None else float("nan")
                    for feature in self.features
                ]
                for row in rows
            ],
            dtype=float,
        )
        raw = self.estimator.predict_proba(matrix)[:, 1]
        calibrated = self.calibrator.predict(raw)
        return [float(value) for value in raw], [float(value) for value in calibrated]

    def probabilities(self, rows: Sequence[Mapping[str, Any]]) -> list[float]:
        return self.score_outputs(rows)[1]


def _percentile_scores(scores: Sequence[float]) -> list[float]:
    order = sorted(range(len(scores)), key=lambda index: (-float(scores[index]), index))
    denominator = max(1, len(scores) - 1)
    result = [0.0] * len(scores)
    for rank, index in enumerate(order):
        result[index] = 1 - rank / denominator
    return result


class IntelligenceV2Experiment:
    def __init__(self, database: str | Path):
        try:
            import duckdb
        except ImportError as error:  # pragma: no cover
            raise RuntimeError("install the research extra: pip install -e .[research]") from error
        self.connection = duckdb.connect(str(database), read_only=True)
        self.engine = IntelligenceV2Research()

    def close(self) -> None:
        self.connection.close()

    def _load_rows(self, timestamp_seconds: int) -> list[dict[str, Any]]:
        tables = {row[0] for row in self.connection.execute("SHOW TABLES").fetchall()}
        drawdown_join = (
            "LEFT JOIN (SELECT mint,min(max_adverse_excursion) max_adverse_excursion "
            "FROM edge_3m GROUP BY mint) d USING(mint)"
            if "edge_3m" in tables
            else ""
        )
        drawdown_field = "d.max_adverse_excursion" if "edge_3m" in tables else "NULL"
        cursor = self.connection.execute(
            f"""
            SELECT r.*,{drawdown_field} max_adverse_excursion
            FROM runner_autopsy_replay r {drawdown_join}
            WHERE timestamp_seconds=? AND evaluated
              AND market_cap_unit='SOL'
              AND current_market_cap BETWEEN .01 AND 1000000
              AND peak_multiple IS NOT NULL
              AND NOT (initial_top10_pct_corrected IS NOT NULL
                       AND initial_top10_pct_corrected>100)
            ORDER BY mint
            """,
            [timestamp_seconds],
        )
        names = [column[0] for column in cursor.description]
        rows = [dict(zip(names, raw, strict=True)) for raw in cursor.fetchall()]
        day_counts = Counter(str(row["decision_at"])[:10] for row in rows)
        ordered_counts = sorted(day_counts.values())
        for row in rows:
            row.update(reconstruct_decision(row))
            age = max(1, int(row["timestamp_seconds"]))
            row["entry_quality_numeric"] = {
                "OPEN": 85,
                "EXTENDED": 45,
                "CHASING": 15,
                "UNKNOWN": 25,
            }.get(str(row["entry_status"]), 25)
            row["launch_intensity_percentile"] = (
                sum(value <= day_counts[str(row["decision_at"])[:10]] for value in ordered_counts)
                / len(ordered_counts)
                * 100
            )
            row["market_cap_velocity"] = ((_number(row.get("market_cap_growth")) or 1) - 1) / age
            row["market_cap_acceleration"] = (_number(row.get("vertical_acceleration")) or 0) / age
            row["age_seconds"] = age
            row["net_buyers"] = (
                None
                if _number(row.get("buy_count")) is None or _number(row.get("sell_count")) is None
                else float(row["buy_count"]) - float(row["sell_count"])
            )
        return rows

    @staticmethod
    def _window(rows: Sequence[dict[str, Any]], name: str) -> list[dict[str, Any]]:
        lower, upper = WINDOWS[name]
        return [row for row in rows if lower <= str(row["decision_at"])[:10] < upper]

    @staticmethod
    def _usable_features(
        rows: Sequence[Mapping[str, Any]], features: Sequence[str]
    ) -> tuple[str, ...]:
        return tuple(
            feature
            for feature in features
            if sum(_number(row.get(feature)) is not None for row in rows)
            >= max(20, len(rows) // 1000)
        )

    @staticmethod
    def _fit_sample(
        rows: Sequence[dict[str, Any]], threshold: int, limit: int = 80_000
    ) -> list[dict[str, Any]]:
        if len(rows) <= limit:
            return list(rows)
        positives = [row for row in rows if float(row["peak_multiple"]) >= threshold]
        negatives = [row for row in rows if float(row["peak_multiple"]) < threshold]
        remaining = max(1, limit - len(positives))
        stride = max(1, len(negatives) // remaining)
        sampled = positives + negatives[::stride][:remaining]
        return sorted(sampled, key=lambda row: str(row["mint"]))

    def _fit_candidate(
        self,
        name: str,
        threshold: int,
        features: Sequence[str],
        train: Sequence[dict[str, Any]],
        validation: Sequence[dict[str, Any]],
        kind: str = "logistic",
    ) -> CalibratedCandidate:
        import numpy as np
        from sklearn.ensemble import HistGradientBoostingClassifier
        from sklearn.impute import SimpleImputer
        from sklearn.isotonic import IsotonicRegression
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
        from sklearn.tree import DecisionTreeClassifier

        train = self._fit_sample(train, threshold)
        usable = self._usable_features(train, features)
        if not usable:
            raise ValueError(f"{name}: no observable features")

        def matrix(rows: Sequence[Mapping[str, Any]]) -> Any:
            return np.asarray(
                [
                    [
                        float(row[feature])
                        if _number(row.get(feature)) is not None
                        else float("nan")
                        for feature in usable
                    ]
                    for row in rows
                ],
                dtype=float,
            )

        labels = np.asarray([float(row["peak_multiple"]) >= threshold for row in train], dtype=int)
        if kind == "tree":
            estimator = Pipeline(
                [
                    ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
                    (
                        "model",
                        DecisionTreeClassifier(
                            max_depth=4,
                            min_samples_leaf=max(50, len(train) // 1000),
                            class_weight="balanced",
                            random_state=15,
                        ),
                    ),
                ]
            )
        elif kind == "boosted":
            estimator = Pipeline(
                [
                    ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
                    (
                        "model",
                        HistGradientBoostingClassifier(
                            max_iter=80,
                            max_leaf_nodes=15,
                            learning_rate=0.06,
                            l2_regularization=2,
                            class_weight="balanced",
                            random_state=15,
                        ),
                    ),
                ]
            )
        else:
            estimator = Pipeline(
                [
                    ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
                    ("scale", StandardScaler()),
                    (
                        "model",
                        LogisticRegression(
                            C=0.25,
                            max_iter=400,
                            class_weight="balanced",
                            random_state=15,
                        ),
                    ),
                ]
            )
        estimator.fit(matrix(train), labels)
        validation_raw = estimator.predict_proba(matrix(validation))[:, 1]
        validation_labels = np.asarray(
            [float(row["peak_multiple"]) >= threshold for row in validation], dtype=int
        )
        calibrator = IsotonicRegression(y_min=0, y_max=1, out_of_bounds="clip").fit(
            validation_raw, validation_labels
        )
        return CalibratedCandidate(
            name,
            threshold,
            usable,
            estimator,
            calibrator,
            len(train),
            len(validation),
        )

    def _evaluate_model(
        self,
        model: CalibratedCandidate,
        diagnostic: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        started = time.perf_counter()
        rank_scores, probabilities = model.score_outputs(diagnostic)
        latency_ms = (time.perf_counter() - started) * 1000
        labels = [float(row["peak_multiple"]) >= model.objective_threshold for row in diagnostic]
        frontiers = {}
        for frequency in FREQUENCIES:
            selected, _ = _rank(diagnostic, rank_scores, frequency)
            frontiers[f"{frequency:.4f}"] = _selection_metrics(selected, diagnostic)
        return {
            "objective_threshold": model.objective_threshold,
            "model_type": type(model.estimator[-1]).__name__,
            "features": list(model.features),
            "fit_rows": model.fit_rows,
            "validation_rows": model.validation_rows,
            "frontiers": frontiers,
            "calibration": _calibration(probabilities, labels),
            "batch_inference_ms": latency_ms,
            "per_decision_inference_ms": latency_ms / len(diagnostic) if diagnostic else None,
            "rank_scores": rank_scores,
            "probabilities": probabilities,
        }

    @staticmethod
    def _baseline(
        name: str,
        rows: Sequence[dict[str, Any]],
        scores: Sequence[float],
    ) -> dict[str, Any]:
        frontiers = {}
        for frequency in FREQUENCIES:
            selected, _ = _rank(rows, scores, frequency)
            frontiers[f"{frequency:.4f}"] = _selection_metrics(selected, rows)
        return {
            "objective_threshold": None,
            "model_type": name,
            "features": [],
            "frontiers": frontiers,
            "calibration": {"brier": None, "ece": None, "reliability": []},
            "batch_inference_ms": 0.0,
            "per_decision_inference_ms": 0.0,
            "probabilities": [float(value) for value in scores],
        }

    @staticmethod
    def _utility(rows: Sequence[dict[str, Any]], scores: Sequence[float], objective: str) -> float:
        selected, _ = _rank(rows, scores, 0.01)
        metrics = _selection_metrics(selected, rows)
        failure = float(metrics["failure_rate"] or 0)
        if objective == "right_tail":
            return (
                float(metrics["20x_recall"] or 0)
                + float(metrics["50x_recall"] or 0)
                + float(metrics["10x_precision"] or 0)
                - 0.25 * failure
            )
        if objective == "mid":
            return (
                2 * float(metrics["5x_precision"] or 0)
                + float(metrics["10x_precision"] or 0)
                + 0.25 * float(metrics["20x_recall"] or 0)
                - 0.25 * failure
            )
        return (
            float(metrics["2x_precision"] or 0)
            + 1.5 * float(metrics["5x_precision"] or 0)
            + float(metrics["10x_precision"] or 0)
            + 0.25 * float(metrics["20x_recall"] or 0)
            + 0.25 * float(metrics["50x_recall"] or 0)
            - 0.25 * failure
        )

    def _validated_blend(
        self,
        name: str,
        threshold: int,
        objective: str,
        validation: Sequence[dict[str, Any]],
        diagnostic: Sequence[dict[str, Any]],
        validation_components: Mapping[str, Sequence[float]],
        diagnostic_components: Mapping[str, Sequence[float]],
        weights: Sequence[Mapping[str, float]],
    ) -> dict[str, Any]:
        from sklearn.isotonic import IsotonicRegression

        validation_ranks = {
            component: _percentile_scores(scores)
            for component, scores in validation_components.items()
        }
        diagnostic_ranks = {
            component: _percentile_scores(scores)
            for component, scores in diagnostic_components.items()
        }

        def blend(
            ranks: Mapping[str, Sequence[float]], selected: Mapping[str, float]
        ) -> list[float]:
            sample = len(next(iter(ranks.values())))
            return [
                sum(weight * ranks[component][index] for component, weight in selected.items())
                for index in range(sample)
            ]

        candidates = []
        for selected in weights:
            validation_scores = blend(validation_ranks, selected)
            candidates.append(
                (
                    self._utility(validation, validation_scores, objective),
                    tuple(sorted(selected.items())),
                    selected,
                    validation_scores,
                )
            )
        _, _, selected_weights, validation_scores = max(candidates)
        inference_started = time.perf_counter()
        diagnostic_scores = blend(diagnostic_ranks, selected_weights)
        validation_labels = [float(row["peak_multiple"]) >= threshold for row in validation]
        calibrator = IsotonicRegression(y_min=0, y_max=1, out_of_bounds="clip").fit(
            validation_scores, validation_labels
        )
        probabilities = [float(value) for value in calibrator.predict(diagnostic_scores)]
        latency_ms = (time.perf_counter() - inference_started) * 1000
        result = self._baseline(name, diagnostic, diagnostic_scores)
        result.update(
            {
                "objective_threshold": threshold,
                "model_type": "VALIDATION_SELECTED_RANK_BLEND",
                "selected_weights": dict(selected_weights),
                "selection_window": "validation",
                "batch_inference_ms": latency_ms,
                "per_decision_inference_ms": (latency_ms / len(diagnostic) if diagnostic else None),
                "calibration": _calibration(
                    probabilities,
                    [float(row["peak_multiple"]) >= threshold for row in diagnostic],
                ),
                "rank_scores": diagnostic_scores,
                "probabilities": probabilities,
            }
        )
        return result

    def _timestamp_experiment(self, timestamp_seconds: int) -> dict[str, Any]:
        rows = self._load_rows(timestamp_seconds)
        train = self._window(rows, "train")
        validation = self._window(rows, "validation")
        diagnostic = self._window(rows, "retired_diagnostic")
        candidates: dict[str, CalibratedCandidate] = {}
        for name, groups in MODEL_FEATURES.items():
            candidates[name] = self._fit_candidate(
                name,
                MODEL_TARGETS[name],
                _features(groups),
                train,
                validation,
            )
        all_available = _features(
            (
                "market_cap",
                "buyers",
                "momentum",
                "liquidity",
                "creator",
                "concentration",
                "entry",
                "survival",
                "failure",
                "regime",
            )
        )
        candidates["SHALLOW_TREE_V2"] = self._fit_candidate(
            "SHALLOW_TREE_V2", 5, all_available, train, validation, "tree"
        )
        candidates["BOOSTED_TREE_V2"] = self._fit_candidate(
            "BOOSTED_TREE_V2", 5, all_available, train, validation, "boosted"
        )
        models = {
            name: self._evaluate_model(model, diagnostic) for name, model in candidates.items()
        }
        models["MID_5X_LOGISTIC_V2"] = models.pop("MID_5X_V2")
        models["RIGHT_TAIL_LOGISTIC_V2"] = models.pop("RIGHT_TAIL_V2")
        models["CURRENT_CONTROL_MEAN"] = self._baseline(
            "CONTROL_V15_RECONSTRUCTION",
            diagnostic,
            [float(row["runner_score"]) / 100 for row in diagnostic],
        )
        models["MARKET_CAP_PRIOR_CONTROL"] = self._baseline(
            "NEGATIVE_LOG_MARKET_CAP",
            diagnostic,
            [-float(row["log_market_cap"]) for row in diagnostic],
        )
        validation_components = {
            name: candidate.score_outputs(validation)[0] for name, candidate in candidates.items()
        }
        validation_components["market_cap"] = [-float(row["log_market_cap"]) for row in validation]
        diagnostic_components = {
            name: candidate.score_outputs(diagnostic)[0] for name, candidate in candidates.items()
        }
        diagnostic_components["market_cap"] = [-float(row["log_market_cap"]) for row in diagnostic]
        models["MID_5X_V2"] = self._validated_blend(
            "MID_5X_V2",
            5,
            "mid",
            validation,
            diagnostic,
            validation_components,
            diagnostic_components,
            (
                {"market_cap": 1.0},
                {"MID_5X_V2": 1.0},
                {"market_cap": 0.75, "MID_5X_V2": 0.25},
                {"market_cap": 0.5, "MID_5X_V2": 0.5},
                {"market_cap": 0.25, "MID_5X_V2": 0.75},
            ),
        )
        models["RIGHT_TAIL_V2"] = self._validated_blend(
            "RIGHT_TAIL_V2",
            20,
            "right_tail",
            validation,
            diagnostic,
            validation_components,
            diagnostic_components,
            (
                {"market_cap": 1.0},
                {"RIGHT_TAIL_V2": 1.0},
                {"market_cap": 0.75, "RIGHT_TAIL_V2": 0.25},
                {"market_cap": 0.5, "RIGHT_TAIL_V2": 0.5},
                {"market_cap": 0.25, "RIGHT_TAIL_V2": 0.75},
            ),
        )
        models["COMBINED_POLICY_V2"] = self._validated_blend(
            "COMBINED_POLICY_V2",
            5,
            "combined",
            validation,
            diagnostic,
            validation_components,
            diagnostic_components,
            (
                {"market_cap": 1.0},
                {"QUICK_2X_V2": 0.4, "MID_5X_V2": 0.35, "RIGHT_TAIL_V2": 0.25},
                {
                    "market_cap": 0.4,
                    "QUICK_2X_V2": 0.3,
                    "MID_5X_V2": 0.2,
                    "RIGHT_TAIL_V2": 0.1,
                },
                {
                    "market_cap": 0.4,
                    "QUICK_2X_V2": 0.1,
                    "MID_5X_V2": 0.3,
                    "RIGHT_TAIL_V2": 0.2,
                },
                {
                    "market_cap": 0.4,
                    "QUICK_2X_V2": 0.1,
                    "MID_5X_V2": 0.1,
                    "RIGHT_TAIL_V2": 0.4,
                },
            ),
        )
        combined = models["COMBINED_POLICY_V2"]["rank_scores"]
        for row in models.values():
            row.pop("rank_scores", None)
            row.pop("probabilities", None)
        return {
            "timestamp_seconds": timestamp_seconds,
            "rows": {
                "train": len(train),
                "validation": len(validation),
                "retired_diagnostic": len(diagnostic),
            },
            "models": models,
            "diagnostic_rows": diagnostic,
            "candidate_objects": candidates,
            "combined_scores": combined,
        }

    def _ablations(
        self,
        timestamp: dict[str, Any],
    ) -> dict[str, Any]:
        rows = self._load_rows(int(timestamp["timestamp_seconds"]))
        train = self._window(rows, "train")
        validation = self._window(rows, "validation")
        diagnostic = timestamp["diagnostic_rows"]
        available_groups = tuple(FEATURE_GROUPS)
        results = {}
        for removed in available_groups:
            if not FEATURE_GROUPS[removed]:
                results[removed] = {"state": "INCONCLUSIVE_NO_PIT_INPUTS"}
                continue
            retained = tuple(group for group in available_groups if group != removed)
            model = self._fit_candidate(
                f"ABLATE_{removed.upper()}", 5, _features(retained), train, validation
            )
            result = self._evaluate_model(model, diagnostic)
            results[removed] = {
                "state": "MEASURED_RETIRED_DIAGNOSTIC",
                "top_1_percent": result["frontiers"]["0.0100"],
            }
        return results

    def _identifier_analysis(
        self, rows: Sequence[dict[str, Any]], limit: int = 50_000
    ) -> dict[str, Any]:
        rare = [row for row in rows if float(row["peak_multiple"]) >= 5]
        stride = max(1, len(rows) // max(1, limit - len(rare)))
        sample_by_mint = {str(row["mint"]): row for row in rows[::stride]}
        sample_by_mint.update({str(row["mint"]): row for row in rare})
        sample = [sample_by_mint[mint] for mint in sorted(sample_by_mint)]
        findings: dict[str, dict[str, Any]] = {
            definition.identifier_id: {
                "known": 0,
                "present": 0,
                "present_2x": 0,
                "present_5x": 0,
                "present_10x": 0,
                "present_20x": 0,
                "present_50x": 0,
                "absent": 0,
                "absent_2x": 0,
                "absent_5x": 0,
                "absent_10x": 0,
                "absent_20x": 0,
                "absent_50x": 0,
                "definition": definition,
            }
            for definition in IDENTIFIER_REGISTRY.all()
        }
        daily: dict[str, dict[str, dict[str, int]]] = defaultdict(
            lambda: defaultdict(lambda: defaultdict(int))
        )
        token_patterns: list[tuple[dict[str, Any], tuple[str, ...]]] = []
        for row in sample:
            point = dict(row)
            point["timestamp_seconds"] = int(row["timestamp_seconds"])
            decision = self.engine.evaluate([point])
            present = tuple(
                signal.identifier_id
                for signal in decision.identifiers
                if signal.state == IdentifierState.PRESENT
            )
            token_patterns.append((row, present))
            for signal in decision.identifiers:
                finding = findings[signal.identifier_id]
                if signal.state == IdentifierState.UNKNOWN:
                    continue
                outcome = float(row["peak_multiple"])
                day = str(row["decision_at"])[:10]
                bucket = daily[signal.identifier_id][day]
                bucket["known"] += 1
                finding["known"] += 1
                if signal.state == IdentifierState.PRESENT:
                    bucket["present"] += 1
                    finding["present"] += 1
                    for threshold in (2, 5, 10, 20, 50):
                        finding[f"present_{threshold}x"] += outcome >= threshold
                        bucket[f"present_{threshold}x"] += outcome >= threshold
                else:
                    bucket["absent"] += 1
                    finding["absent"] += 1
                    for threshold in (2, 5, 10, 20, 50):
                        finding[f"absent_{threshold}x"] += outcome >= threshold
                        bucket[f"absent_{threshold}x"] += outcome >= threshold
        rendered = []
        for identifier_id, finding in findings.items():
            definition = finding.pop("definition")
            effects = {}
            present_rates = {}
            absent_rates = {}
            for threshold in (2, 5, 10, 20, 50):
                present_rate = (
                    finding[f"present_{threshold}x"] / finding["present"]
                    if finding["present"]
                    else None
                )
                absent_rate = (
                    finding[f"absent_{threshold}x"] / finding["absent"]
                    if finding["absent"]
                    else None
                )
                present_rates[f"{threshold}x"] = present_rate
                absent_rates[f"{threshold}x"] = absent_rate
                effects[f"{threshold}x"] = (
                    present_rate - absent_rate
                    if present_rate is not None and absent_rate is not None
                    else None
                )
            coverage = finding["known"] / len(sample) if sample else 0
            daily_effects = []
            for bucket in daily[identifier_id].values():
                if bucket["present"] < 20 or bucket["absent"] < 20:
                    continue
                daily_effects.append(
                    bucket["present_2x"] / bucket["present"]
                    - bucket["absent_2x"] / bucket["absent"]
                )
            expected_sign = 1 if definition.direction == "POSITIVE" else -1
            directional_days = sum(effect * expected_sign > 0 for effect in daily_effects)
            stability = directional_days / len(daily_effects) if daily_effects else None
            stability_state = (
                "UNKNOWN_INSUFFICIENT_TEMPORAL_SUPPORT"
                if len(daily_effects) < 3
                else "STABLE"
                if stability is not None and stability >= 0.75
                else "UNSTABLE"
            )
            primary_effect = effects["2x"]
            contradicted = (
                coverage >= 0.05
                and primary_effect is not None
                and definition.direction in {"POSITIVE", "NEGATIVE"}
                and primary_effect * expected_sign < -0.005
            )
            status = "REJECT" if contradicted else "RESEARCH_ONLY"
            rendered.append(
                {
                    "identifier_id": identifier_id,
                    "family": definition.family,
                    "direction": definition.direction,
                    "coverage": coverage,
                    "present": finding["present"],
                    "present_rates": present_rates,
                    "absent_rates": absent_rates,
                    "effects": effects,
                    "effect_2x": primary_effect,
                    "stability": stability,
                    "stability_state": stability_state,
                    "stability_periods": len(daily_effects),
                    "gameability": definition.gameability,
                    "pit_safety": definition.pit_availability,
                    "status": status,
                }
            )
        patterns: Counter[tuple[str, ...]] = Counter()
        pattern_outcomes: dict[tuple[str, ...], list[float]] = defaultdict(list)
        for row, present in token_patterns:
            relevant = tuple(sorted(present))
            for size in (2, 3):
                for pattern in combinations(relevant[:12], size):
                    patterns[pattern] += 1
                    pattern_outcomes[pattern].append(float(row["peak_multiple"]))
        fingerprints = []
        for pattern, count in patterns.most_common():
            if count < 30:
                continue
            outcomes = pattern_outcomes[pattern]
            fingerprints.append(
                {
                    "identifiers": list(pattern),
                    "sample": count,
                    "2x_rate": _rate([value >= 2 for value in outcomes]),
                    "5x_rate": _rate([value >= 5 for value in outcomes]),
                    "20x_rate": _rate([value >= 20 for value in outcomes]),
                }
            )
        fingerprints.sort(
            key=lambda row: (row["20x_rate"], row["5x_rate"], row["sample"]), reverse=True
        )
        return {
            "sample": len(sample),
            "identifiers": rendered,
            "fingerprints": fingerprints[:50],
        }

    @staticmethod
    def _recovery(
        rows: Sequence[dict[str, Any]],
        control_scores: Sequence[float],
        v2_scores: Sequence[float],
        frequency: float = 0.01,
    ) -> dict[str, Any]:
        _, control_indexes = _rank(rows, control_scores, frequency)
        _, v2_indexes = _rank(rows, v2_scores, frequency)
        control, v2 = set(control_indexes), set(v2_indexes)
        result = {}
        for threshold in (2, 3, 5, 10, 20, 50):
            runners = {
                index for index, row in enumerate(rows) if float(row["peak_multiple"]) >= threshold
            }
            recovered = (v2 - control) & runners
            lost = (control - v2) & runners
            result[f"{threshold}x"] = {
                "total": len(runners),
                "control_captured": len(control & runners),
                "v2_captured": len(v2 & runners),
                "recovered": len(recovered),
                "lost_vs_control": len(lost),
                "control_miss_percent": (
                    1 - len(control & runners) / len(runners) if runners else None
                ),
                "v2_miss_percent": 1 - len(v2 & runners) / len(runners) if runners else None,
            }
        result["new_false_positives"] = sum(
            float(rows[index]["peak_multiple"]) < 2 for index in v2 - control
        )
        return result

    def run(self) -> dict[str, Any]:
        timestamp_results = {
            f"T+{timestamp}s": self._timestamp_experiment(timestamp) for timestamp in TIMESTAMPS
        }
        reference = timestamp_results["T+60s"]
        diagnostic = reference["diagnostic_rows"]
        combined_scores = reference["combined_scores"]
        control_scores = [float(row["runner_score"]) / 100 for row in diagnostic]
        ablations = self._ablations(reference)
        identifier_analysis = self._identifier_analysis(diagnostic)
        recovery = self._recovery(diagnostic, control_scores, combined_scores)
        for result in timestamp_results.values():
            result.pop("diagnostic_rows", None)
            result.pop("candidate_objects", None)
            result.pop("combined_scores", None)
        return {
            "version": INTELLIGENCE_V2_VERSION,
            "registry_version": IDENTIFIER_REGISTRY.all()[0].version,
            "outcome_version": OUTCOME_VERSION,
            "control_freeze_sha": CONTROL_FREEZE_SHA,
            "state": "RESEARCH_ONLY_RETIRED_DIAGNOSTICS_NOT_SEALED",
            "windows": WINDOWS,
            "timestamps": timestamp_results,
            "missing_timestamp": {"T+90s": "UNKNOWN_NO_NATIVE_BUYER_TRAJECTORY_OBSERVATION"},
            "ablations_t60": ablations,
            "identifier_registry": IDENTIFIER_REGISTRY.to_dict(),
            "identifier_analysis_t60": identifier_analysis,
            "control_vs_v2_recovery_t60": recovery,
            "approved_features": 0,
            "challenger_decisions": 0,
            "sealed_validation_complete": False,
            "challenger_ready": False,
            "production_ready": False,
            "limitations": [
                "June/July windows are retired diagnostics and cannot prove production edge.",
                "Wallet, funder, Sybil, social and pre/post migration identity archives are absent.",
                "Pre-graduation liquidity is absent; liquidity models are coverage-limited.",
                "Failure history lacks sell restriction, cluster dump and terminal safety vectors.",
                "End-to-end provider and Discord latency is not measured by offline inference latency.",
                "Drawdown is accepted only when the independent dimensionless MAE is in [-1,0].",
            ],
        }

    def write(self, output: str | Path) -> dict[str, Any]:
        result = self.run()
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n")
        return result

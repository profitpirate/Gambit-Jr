from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .evaluation import EvaluationUniverse, evaluation_universe_hash

TARGETS = (2, 5, 10, 20, 50)


def _finite(value: Any) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _feature_names(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    return sorted(
        {
            name
            for row in rows
            for name, value in dict(row.get("features") or {}).items()
            if _finite(value) is not None
        }
    )


def _matrix(
    rows: Sequence[Mapping[str, Any]], names: Sequence[str]
) -> tuple[list[list[float]], list[list[bool]]]:
    matrix: list[list[float]] = []
    missing: list[list[bool]] = []
    for row in rows:
        features = dict(row.get("features") or {})
        matrix.append([_finite(features.get(name)) or 0.0 for name in names])
        missing.append([_finite(features.get(name)) is None for name in names])
    return matrix, missing


def enforce_nested_probabilities(
    probabilities: Mapping[int, Sequence[float]],
) -> dict[int, list[float]]:
    if not probabilities:
        return {}
    size = len(next(iter(probabilities.values())))
    output = {target: [0.0] * size for target in TARGETS if target in probabilities}
    for index in range(size):
        ceiling = 1.0
        for target in TARGETS:
            if target not in probabilities:
                continue
            value = min(ceiling, max(0.0, min(1.0, float(probabilities[target][index]))))
            output[target][index] = value
            ceiling = value
    return output


def _top_fraction_metrics(
    rows: Sequence[Mapping[str, Any]], scores: Sequence[float], target: int, fraction: float
) -> dict[str, float | int | None]:
    if not rows:
        return {"selected": 0, "precision": None, "recall": None}
    count = max(1, round(len(rows) * fraction))
    order = sorted(range(len(rows)), key=lambda index: (-float(scores[index]), index))[:count]
    winners = [float(rows[index]["peak_multiple_from_decision"]) >= target for index in order]
    all_winners = sum(float(row["peak_multiple_from_decision"]) >= target for row in rows)
    hits = sum(winners)
    return {
        "selected": count,
        "precision": hits / count,
        "recall": hits / all_winners if all_winners else None,
    }


@dataclass(slots=True)
class TargetExperiment:
    target: int
    model_kind: str
    scores: list[float]
    metrics: dict[str, Any]


class TargetSpecificRunnerResearch:
    """Chronological, calibrated runner/failure/actionability challenger research.

    Models returned here are research artifacts. This class never writes an approval
    or enables a public route.
    """

    def run(
        self,
        development: Sequence[Mapping[str, Any]],
        validation: Sequence[Mapping[str, Any]],
        universe: EvaluationUniverse,
    ) -> dict[str, Any]:
        if len(development) < 30 or len(validation) < 20:
            return {
                "status": "INSUFFICIENT_MATURED_CHRONOLOGICAL_SAMPLE",
                "development_sample": len(development),
                "validation_sample": len(validation),
                "approved": False,
            }
        if max(str(row["decision_at"]) for row in development) >= min(
            str(row["decision_at"]) for row in validation
        ):
            raise ValueError("development must strictly precede validation")
        names = _feature_names([*development, *validation])
        if not names:
            raise ValueError("no finite point-in-time features are available")
        dev_x, _ = _matrix(development, names)
        valid_x, _ = _matrix(validation, names)
        raw_probabilities: dict[int, list[float]] = {}
        experiments: dict[str, Any] = {}
        for target in TARGETS:
            labels = [
                int(float(row["peak_multiple_from_decision"]) >= target) for row in development
            ]
            if len(set(labels)) < 2:
                experiments[f"{target}x"] = {"status": "ONE_CLASS_DEVELOPMENT"}
                continue
            target_runs = self._fit_candidates(dev_x, labels, valid_x)
            for model_kind, scores in target_runs.items():
                metrics = _top_fraction_metrics(validation, scores, target, 0.1)
                experiments[f"{model_kind}_{target}x"] = metrics
            best_kind = max(
                target_runs,
                key=lambda kind: (
                    float(
                        experiments[f"{kind}_{target}x"].get("precision")
                        if experiments[f"{kind}_{target}x"].get("precision") is not None
                        else -1
                    ),
                    kind,
                ),
            )
            raw_probabilities[target] = target_runs[best_kind]
            experiments[f"{target}x"] = {
                "status": "EVALUATED",
                "selected_model": best_kind,
                **experiments[f"{best_kind}_{target}x"],
            }
        nested = enforce_nested_probabilities(raw_probabilities)
        universe_hash = evaluation_universe_hash(universe, validation)
        return {
            "status": "OFFLINE_VALIDATION_COMPLETE",
            "approved": False,
            "approval_reason": "HUMAN_APPROVAL_AND_UNSEEN_OUTER_TEST_REQUIRED",
            "evaluation_universe_hash": universe_hash,
            "feature_names": names,
            "targets": experiments,
            "nested_probabilities": nested,
            "development_sample": len(development),
            "validation_sample": len(validation),
        }

    @staticmethod
    def _fit_candidates(
        development_x: Sequence[Sequence[float]],
        labels: Sequence[int],
        validation_x: Sequence[Sequence[float]],
    ) -> dict[str, list[float]]:
        try:
            from sklearn.ensemble import HistGradientBoostingClassifier
            from sklearn.linear_model import LogisticRegression
            from sklearn.pipeline import make_pipeline
            from sklearn.preprocessing import StandardScaler
        except ImportError as exc:  # pragma: no cover - optional research dependency
            raise RuntimeError("install the research extra to fit runner models") from exc

        split = max(20, int(len(development_x) * 0.8))
        if split >= len(development_x) - 5 or len(set(labels[:split])) < 2:
            split = len(development_x)
        fit_x = development_x[:split]
        fit_labels = labels[:split]
        logistic = make_pipeline(
            StandardScaler(),
            LogisticRegression(C=0.25, max_iter=2_000, class_weight="balanced"),
        )
        logistic.fit(fit_x, fit_labels)
        hist = HistGradientBoostingClassifier(
            learning_rate=0.05,
            max_iter=150,
            max_leaf_nodes=15,
            l2_regularization=1.0,
            class_weight="balanced",
            random_state=15,
        )
        hist.fit(fit_x, fit_labels)

        def predict_calibrated(model: Any) -> list[float]:
            validation_scores = [float(row[1]) for row in model.predict_proba(validation_x)]
            if split == len(development_x) or len(set(labels[split:])) < 2:
                return validation_scores
            calibration_scores = [
                float(row[1]) for row in model.predict_proba(development_x[split:])
            ]
            # Platt calibration is trained only on the later development tail;
            # the base model never sees those rows during fitting.
            calibrator = LogisticRegression(C=1.0, max_iter=2_000)
            calibrator.fit(
                [
                    [math.log(max(1e-9, value) / max(1e-9, 1 - value))]
                    for value in calibration_scores
                ],
                labels[split:],
            )
            return [
                float(row[1])
                for row in calibrator.predict_proba(
                    [
                        [math.log(max(1e-9, value) / max(1e-9, 1 - value))]
                        for value in validation_scores
                    ]
                )
            ]

        return {
            "regularized_logistic": predict_calibrated(logistic),
            "hist_gradient_boosting": predict_calibrated(hist),
        }

    def hard_negative_run(
        self,
        development: Sequence[Mapping[str, Any]],
        validation: Sequence[Mapping[str, Any]],
        *,
        target: int,
        high_rank_key: str = "control_score",
    ) -> dict[str, Any]:
        ranked = [
            row
            for row in development
            if isinstance(row.get(high_rank_key), (int, float))
            and float(row[high_rank_key]) >= 0.75
        ]
        hard_negative = [
            row for row in ranked if float(row["peak_multiple_from_decision"]) < target
        ]
        winners = [row for row in ranked if float(row["peak_multiple_from_decision"]) >= target]
        if len(hard_negative) < 10 or len(winners) < 10:
            return {
                "status": "INSUFFICIENT_HARD_NEGATIVES",
                "target": target,
                "high_rank_winners": len(winners),
                "high_rank_false_positives": len(hard_negative),
                "approved": False,
            }
        names = _feature_names([*winners, *hard_negative, *validation])
        train = sorted([*winners, *hard_negative], key=lambda row: str(row["decision_at"]))
        train_x, _ = _matrix(train, names)
        valid_x, _ = _matrix(validation, names)
        labels = [int(float(row["peak_multiple_from_decision"]) >= target) for row in train]
        scores = self._fit_candidates(train_x, labels, valid_x)["hist_gradient_boosting"]
        return {
            "status": "OFFLINE_VALIDATION_COMPLETE",
            "target": target,
            "approved": False,
            "development_sample": len(train),
            "metrics": _top_fraction_metrics(validation, scores, target, 0.1),
        }

    def independent_failure_actionability_run(
        self,
        development: Sequence[Mapping[str, Any]],
        validation: Sequence[Mapping[str, Any]],
        universe: EvaluationUniverse,
    ) -> dict[str, Any]:
        """Fit failure and copyability as separate empirical targets.

        Neither label is derived from a runner score, and neither score is
        subtracted from the positive runner ranking here.
        """
        if len(development) < 30 or len(validation) < 20:
            return {
                "status": "INSUFFICIENT_MATURED_CHRONOLOGICAL_SAMPLE",
                "approved": False,
                "development_sample": len(development),
                "validation_sample": len(validation),
            }
        if max(str(row["decision_at"]) for row in development) >= min(
            str(row["decision_at"]) for row in validation
        ):
            raise ValueError("development must strictly precede validation")
        names = _feature_names([*development, *validation])
        tasks = {
            "failure": "terminal_failure",
            "actionability": "copyable",
        }
        results: dict[str, Any] = {}
        for task, label_name in tasks.items():
            task_development = [row for row in development if row.get(label_name) is not None]
            task_validation = [row for row in validation if row.get(label_name) is not None]
            if len(task_development) < 30 or len(task_validation) < 20:
                results[task] = {
                    "status": f"INSUFFICIENT_{label_name.upper()}_LABEL_COVERAGE",
                    "development_sample": len(task_development),
                    "validation_sample": len(task_validation),
                }
                continue
            task_names = _feature_names([*task_development, *task_validation])
            task_dev_x, _ = _matrix(task_development, task_names)
            task_valid_x, _ = _matrix(task_validation, task_names)
            labels = [int(bool(row[label_name])) for row in task_development]
            if len(set(labels)) < 2:
                results[task] = {"status": "ONE_CLASS_DEVELOPMENT"}
                continue
            candidates = self._fit_candidates(task_dev_x, labels, task_valid_x)
            validation_labels = [int(bool(row[label_name])) for row in task_validation]
            results[task] = {
                "status": "EVALUATED_INDEPENDENTLY",
                "positive_rate": sum(validation_labels) / len(validation_labels),
                "models": {
                    name: self._binary_metrics(validation_labels, probabilities)
                    for name, probabilities in candidates.items()
                },
            }
        return {
            "status": "OFFLINE_VALIDATION_COMPLETE",
            "approved": False,
            "approval_reason": "HUMAN_APPROVAL_AND_UNSEEN_OUTER_TEST_REQUIRED",
            "evaluation_universe_hash": evaluation_universe_hash(universe, validation),
            "feature_names": names,
            "targets_are_independent": True,
            "tasks": results,
        }

    @staticmethod
    def _binary_metrics(labels: Sequence[int], scores: Sequence[float]) -> dict[str, float]:
        try:
            from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
        except ImportError as exc:  # pragma: no cover - optional research dependency
            raise RuntimeError("install the research extra to evaluate models") from exc
        clipped = [min(1 - 1e-9, max(1e-9, float(value))) for value in scores]
        output = {
            "brier": float(brier_score_loss(labels, clipped)),
            "log_loss": float(log_loss(labels, clipped, labels=[0, 1])),
        }
        if len(set(labels)) > 1:
            output["roc_auc"] = float(roc_auc_score(labels, clipped))
        return output

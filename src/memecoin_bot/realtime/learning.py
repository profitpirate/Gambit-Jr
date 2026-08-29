from __future__ import annotations

import hashlib
import json
import math
import statistics
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

FIXED_FREQUENCIES = (0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.02, 0.05)


def _json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"), sort_keys=True)


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include timezone: {value}")
    return parsed.astimezone(UTC)


def _rate(values: list[bool]) -> float | None:
    return sum(values) / len(values) if values else None


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def wilson_interval(
    successes: int, sample: int, z: float = 1.959963984540054
) -> list[float] | None:
    if sample <= 0:
        return None
    proportion = successes / sample
    denominator = 1 + z * z / sample
    center = (proportion + z * z / (2 * sample)) / denominator
    margin = (
        z
        * math.sqrt(proportion * (1 - proportion) / sample + z * z / (4 * sample * sample))
        / denominator
    )
    return [max(0.0, center - margin), min(1.0, center + margin)]


def fixed_frequency_metrics(
    rows: list[dict[str, Any]], score_key: str, frequency: float
) -> dict[str, Any]:
    if not 0 < frequency <= 1:
        raise ValueError("frequency must be in (0, 1]")
    scored = [row for row in rows if isinstance(row.get(score_key), (int, float))]
    scored.sort(key=lambda row: (-float(row[score_key]), str(row.get("entity_key") or "")))
    count = min(len(scored), max(1, math.ceil(len(scored) * frequency))) if scored else 0
    selected = scored[:count]
    peaks = [float(row.get("peak_multiple") or 0) for row in selected]
    universe_peaks = [float(row.get("peak_multiple") or 0) for row in rows]
    metrics: dict[str, Any] = {
        "frequency": frequency,
        "signals": count,
        "universe": len(rows),
        "signals_per_day": None,
        "2x_precision": _rate([peak >= 2 for peak in peaks]),
        "5x_precision": _rate([peak >= 5 for peak in peaks]),
        "10x_precision": _rate([peak >= 10 for peak in peaks]),
        "20x_recall": _rate(
            [row in selected for row in rows if float(row.get("peak_multiple") or 0) >= 20]
        ),
        "50x_recall": _rate(
            [row in selected for row in rows if float(row.get("peak_multiple") or 0) >= 50]
        ),
        "terminal_failure": _rate([bool(row.get("terminal_failure")) for row in selected]),
        "median_mae": _median(
            [
                float(row["maximum_adverse_excursion"])
                for row in selected
                if row.get("maximum_adverse_excursion") is not None
            ]
        ),
        "median_entry_mc": _median(
            [
                float(row["entry_market_cap"])
                for row in selected
                if row.get("entry_market_cap") is not None
            ]
        ),
        "median_latency_seconds": _median(
            [
                float(row["latency_seconds"])
                for row in selected
                if row.get("latency_seconds") is not None
            ]
        ),
        "copyable_share": _rate([bool(row.get("copyable")) for row in selected]),
        "selected_entities": [str(row.get("entity_key")) for row in selected],
    }
    dates = sorted({_timestamp(str(row["decision_at"])).date() for row in rows}) if rows else []
    if dates:
        days = max(1, (dates[-1] - dates[0]).days + 1)
        metrics["signals_per_day"] = count / days
    wins = sum(peak >= 2 for peak in peaks)
    metrics["2x_wilson_95"] = wilson_interval(wins, count)
    metrics["natural_prevalence"] = {
        target: _rate([peak >= target for peak in universe_peaks]) for target in (2, 5, 10, 20, 50)
    }
    return metrics


def _numeric_features(row: dict[str, Any]) -> dict[str, float]:
    output: dict[str, float] = {}
    for key, value in (row.get("features") or {}).items():
        if (
            isinstance(value, bool)
            or isinstance(value, (int, float))
            and math.isfinite(float(value))
        ):
            output[key] = float(value)
    return output


def _flatten_numeric(value: Any, prefix: str = "") -> dict[str, float]:
    output: dict[str, float] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            output.update(_flatten_numeric(item, path))
    elif isinstance(value, bool) or isinstance(value, (int, float)) and math.isfinite(float(value)):
        output[prefix] = float(value)
    return output


def fit_development_ranker(rows: list[dict[str, Any]], target: int = 2) -> dict[str, Any]:
    """Fit standardized univariate effects only on the supplied development rows."""
    by_feature: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"winner": [], "other": []})
    for row in rows:
        cohort = "winner" if float(row.get("peak_multiple") or 0) >= target else "other"
        for name, value in _numeric_features(row).items():
            by_feature[name][cohort].append(value)
    weights: dict[str, dict[str, float]] = {}
    for name, cohorts in by_feature.items():
        if len(cohorts["winner"]) < 5 or len(cohorts["other"]) < 5:
            continue
        values = cohorts["winner"] + cohorts["other"]
        scale = statistics.pstdev(values)
        if not scale:
            continue
        effect = (statistics.mean(cohorts["winner"]) - statistics.mean(cohorts["other"])) / scale
        weights[name] = {
            "effect": max(-3.0, min(3.0, effect)),
            "center": statistics.mean(values),
            "scale": scale,
            "winner_sample": len(cohorts["winner"]),
            "other_sample": len(cohorts["other"]),
        }
    return {"target": target, "weights": weights, "development_sample": len(rows)}


def apply_ranker(rows: list[dict[str, Any]], model: dict[str, Any], score_key: str) -> None:
    weights = model["weights"]
    for row in rows:
        features = _numeric_features(row)
        contributions = [
            spec["effect"] * (features[name] - spec["center"]) / spec["scale"]
            for name, spec in weights.items()
            if name in features
        ]
        row[score_key] = statistics.mean(contributions) if contributions else None


class AdaptiveLearningLab:
    """Human-gated error autopsy, hypothesis, drift, and challenger research loop."""

    def __init__(self, store: Any):
        self.store = store

    def rows_from_store(self) -> list[dict[str, Any]]:
        """Build mature, point-in-time examples from the operational evidence ledger."""
        rows = self.store.conn.execute(
            "SELECT d.decision_id,d.token_id,d.decision_at,d.decision_market_cap,"
            "d.peak_multiple_from_decision,d.maximum_adverse_excursion,d.terminal_failure,"
            "d.copyability_at_decision,d.outcome_mature_at,"
            "r.stage,r.tier,r.route_state,r.controls_json,r.heuristic_scores_json,r.latency_json,"
            "c.id candidate_id,c.initial_liquidity_usd FROM decision_outcomes_v15 d "
            "JOIN runner_decisions_v15 r ON r.decision_id=d.decision_id "
            "LEFT JOIN candidates c ON c.id=r.candidate_id "
            "WHERE d.peak_multiple_from_decision IS NOT NULL "
            "AND d.outcome_state IN ('MATURE','SEALED') AND d.outcome_mature_at IS NOT NULL "
            "ORDER BY d.decision_at,d.decision_id"
        )
        output: list[dict[str, Any]] = []
        for row in rows:
            decision_at = str(row["decision_at"])
            v3 = self.store.conn.execute(
                "SELECT decision_timestamp,v3_decision_json,control_decision_json,latency_json "
                "FROM intelligence_v3_shadow_decisions WHERE token_id=? "
                "AND decision_timestamp<=? AND available_evidence_timestamp<=? "
                "ORDER BY decision_timestamp DESC LIMIT 1",
                (row["token_id"], decision_at, decision_at),
            ).fetchone()
            trajectory = self.store.conn.execute(
                "SELECT feature_json FROM trajectory_feature_snapshots_v15 WHERE token_id=? "
                "AND decision_timestamp<=? AND available_timestamp<=? "
                "ORDER BY decision_timestamp DESC LIMIT 1",
                (row["token_id"], decision_at, decision_at),
            ).fetchone()
            if not trajectory:
                continue
            feature = json.loads(trajectory[0])
            v3_payload = json.loads(v3["v3_decision_json"]) if v3 else {}
            control_payload = json.loads(row["controls_json"])
            latency = json.loads(row["latency_json"])
            peak = float(row["peak_multiple_from_decision"])
            legacy = control_payload.get("legacy") or {}
            output.append(
                {
                    "entity_key": f"decision:{row['decision_id']}",
                    "decision_at": decision_at,
                    "outcome_available_at": str(row["outcome_mature_at"]),
                    "peak_multiple_from_decision": peak,
                    "peak_multiple": peak,
                    "terminal_failure": (
                        bool(row["terminal_failure"])
                        if row["terminal_failure"] is not None
                        else None
                    ),
                    "copyable": (
                        bool(row["copyability_at_decision"])
                        if row["copyability_at_decision"] is not None
                        else None
                    ),
                    "maximum_adverse_excursion": row["maximum_adverse_excursion"],
                    "entry_market_cap": row["decision_market_cap"],
                    "latency_seconds": (
                        float(latency.get("source_to_decision_ms")) / 1000
                        if latency.get("source_to_decision_ms") is not None
                        else None
                    ),
                    "stage": row["stage"],
                    "features": _flatten_numeric(feature),
                    "control_score": legacy.get("normalized_score"),
                    "v3_score": v3_payload.get("expected_utility"),
                    "control_selected": str(legacy.get("classification") or "")
                    in {"WATCH", "STRONG", "HIGH_CONVICTION"},
                    "control_components": legacy.get("component_scores") or {},
                    "stage_a_selected": bool(v3_payload.get("primary_nominator"))
                    and v3_payload.get("primary_nominator") != "NONE",
                    "stage_b_selected": v3_payload.get("precision_gate") == "PASSED",
                }
            )
        return output

    def run_store(self, model_name: str = "REALTIME_TRAJECTORY_CHALLENGER_V1") -> dict[str, Any]:
        rows = self.rows_from_store()
        dates = sorted({_timestamp(str(row["decision_at"])) for row in rows})
        if len(dates) < 2:
            return {
                "status": "INSUFFICIENT_MATURED_CHRONOLOGICAL_SAMPLE",
                "sample": len(rows),
                "public_route": False,
            }
        split_index = max(1, min(len(dates) - 1, int(len(dates) * 0.7)))
        development_end = dates[split_index].isoformat()
        validation_end = (
            max(_timestamp(str(row["outcome_available_at"])) for row in rows) + datetime.resolution
        ).isoformat()
        return self.run(
            rows,
            development_end=development_end,
            validation_end=validation_end,
            model_name=model_name,
        )

    @staticmethod
    def chronological_split(
        rows: list[dict[str, Any]], development_end: str, validation_end: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        dev_end = _timestamp(development_end)
        valid_end = _timestamp(validation_end)
        if valid_end <= dev_end:
            raise ValueError("validation must follow development")
        ordered = sorted(rows, key=lambda row: _timestamp(str(row["decision_at"])))
        development = [row for row in ordered if _timestamp(str(row["decision_at"])) < dev_end]
        validation = [
            row for row in ordered if dev_end <= _timestamp(str(row["decision_at"])) < valid_end
        ]
        if {str(row.get("entity_key")) for row in development} & {
            str(row.get("entity_key")) for row in validation
        }:
            raise ValueError("entity leakage across chronological split")
        return development, validation

    def run(
        self,
        rows: list[dict[str, Any]],
        *,
        development_end: str,
        validation_end: str,
        model_name: str = "REALTIME_TRAJECTORY_CHALLENGER_V1",
    ) -> dict[str, Any]:
        development, validation = self.chronological_split(rows, development_end, validation_end)
        if len(development) < 20 or len(validation) < 10:
            return {
                "status": "INSUFFICIENT_MATURED_CHRONOLOGICAL_SAMPLE",
                "development_sample": len(development),
                "validation_sample": len(validation),
                "public_route": False,
            }
        model = fit_development_ranker(development, 2)
        apply_ranker(development, model, "realtime_candidate_score")
        apply_ranker(validation, model, "realtime_candidate_score")
        # Do not manufacture a hidden hybrid by averaging independent rankers
        # and subtracting a heuristic failure value. Each score remains an
        # explicit same-universe research comparator.
        frontiers: dict[str, dict[str, Any]] = {}
        for score_key in (
            "control_score",
            "v3_score",
            "realtime_candidate_score",
        ):
            frontiers[score_key] = {
                str(frequency): fixed_frequency_metrics(validation, score_key, frequency)
                for frequency in FIXED_FREQUENCIES
            }
        autopsy = self.low_performance_autopsy(validation, "realtime_candidate_score", 0.01)
        control = self.control_autopsy(validation)
        hypotheses = self.discover_hypotheses(development, validation, model)
        stage_b = self.stage_b_autopsy(validation)
        run_id = str(uuid.uuid4())
        advancement = self._advancement(frontiers, len(validation))
        with self.store._lock, self.store.conn:
            self.store.conn.execute(
                "INSERT INTO challenger_runs_v15(run_id,model_name,champion_name,created_at,"
                "development_window_json,validation_window_json,feature_version,metrics_json,"
                "fixed_frequency_json,advancement_state,public_route,evidence_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,0,?)",
                (
                    run_id,
                    model_name,
                    "CONTROL_V15",
                    datetime.now(UTC).isoformat(),
                    _json({"end": development_end, "sample": len(development)}),
                    _json(
                        {"start": development_end, "end": validation_end, "sample": len(validation)}
                    ),
                    "realtime-trajectory-v1",
                    _json({"autopsy": autopsy, "control": control, "stage_b": stage_b}),
                    _json(frontiers),
                    advancement,
                    _json({"model": model, "hypotheses": hypotheses, "outer_test_used": False}),
                ),
            )
            self.store.conn.execute(
                "INSERT INTO learning_autopsies_v15 VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    run_id,
                    model_name,
                    "LOW_PERFORMANCE_SELF_REFLECTION",
                    "2X",
                    datetime.now(UTC).isoformat(),
                    _json(autopsy["root_causes"]),
                    _json(autopsy["cohorts"]),
                    _json(autopsy["matched_differences"]),
                    "COMPLETE",
                ),
            )
        return {
            "status": "MEASURED_SHADOW_ONLY",
            "run_id": run_id,
            "model": model,
            "frontiers": frontiers,
            "autopsy": autopsy,
            "control_autopsy": control,
            "stage_b_autopsy": stage_b,
            "hypotheses": hypotheses,
            "advancement": advancement,
            "public_route": False,
        }

    @staticmethod
    def _advancement(frontiers: dict[str, dict[str, Any]], sample: int) -> str:
        if sample < 250:
            return "PROVISIONAL_SAMPLE_HUMAN_REVIEW_REQUIRED"
        candidate = frontiers["realtime_candidate_score"]["0.01"]
        control = frontiers["control_score"]["0.01"]
        better = (
            candidate["2x_precision"] is not None
            and control["2x_precision"] is not None
            and candidate["2x_precision"] > control["2x_precision"]
            and (candidate["terminal_failure"] or 0) <= (control["terminal_failure"] or 0)
        )
        return "CHALLENGER_HUMAN_REVIEW_REQUIRED" if better else "REJECTED_NO_STABLE_LIFT"

    @staticmethod
    def low_performance_autopsy(
        rows: list[dict[str, Any]], score_key: str, frequency: float
    ) -> dict[str, Any]:
        metrics = fixed_frequency_metrics(rows, score_key, frequency)
        selected_ids = set(metrics["selected_entities"])
        selected = [row for row in rows if str(row.get("entity_key")) in selected_ids]
        false_positives = [row for row in selected if float(row.get("peak_multiple") or 0) < 2]
        missed = [
            row
            for row in rows
            if float(row.get("peak_multiple") or 0) >= 5
            and str(row.get("entity_key")) not in selected_ids
        ]
        categories = {
            "WASH_OR_RECYCLE": lambda row: (
                float((row.get("features") or {}).get("wash_probability") or 0) >= 0.3
            ),
            "LINKED_WALLETS": lambda row: (
                float((row.get("features") or {}).get("linked_wallet_share") or 0) >= 0.3
            ),
            "CREATOR_LINKED_FLOW": lambda row: (
                float((row.get("features") or {}).get("creator_linked_share") or 0) >= 0.25
            ),
            "CAPITAL_REVERSAL": lambda row: bool(
                (row.get("features") or {}).get("capital_reversal")
            ),
            "BUYER_COLLAPSE": lambda row: bool((row.get("features") or {}).get("buyer_collapse")),
            "LATE_OR_UNCOPYABLE_ENTRY": lambda row: not bool(row.get("copyable", True)),
            "HIGH_LATENCY": lambda row: float(row.get("latency_seconds") or 0) >= 30,
            "LOW_EVIDENCE_COVERAGE": lambda row: (
                float((row.get("features") or {}).get("evidence_coverage") or 0) < 0.6
            ),
        }
        roots = []
        for name, predicate in categories.items():
            fp_hits = sum(predicate(row) for row in false_positives)
            missed_hits = sum(predicate(row) for row in missed)
            support = fp_hits + missed_hits
            if support:
                roots.append(
                    {
                        "root_cause": name,
                        "false_positive_hits": fp_hits,
                        "false_positive_rate": fp_hits / len(false_positives)
                        if false_positives
                        else None,
                        "missed_runner_hits": missed_hits,
                        "missed_runner_rate": missed_hits / len(missed) if missed else None,
                        "support": support,
                    }
                )
        roots.sort(key=lambda value: (-value["support"], value["root_cause"]))
        matched = AdaptiveLearningLab._matched_differences(missed, false_positives)
        return {
            "metrics": metrics,
            "cohorts": {
                "selected": len(selected),
                "false_positives": len(false_positives),
                "missed_5x_plus": len(missed),
                "false_positive_stages": Counter(str(row.get("stage")) for row in false_positives),
                "missed_stages": Counter(str(row.get("stage")) for row in missed),
            },
            "root_causes": roots,
            "matched_differences": matched,
            "questions_answered": {
                "ranking_vs_selection": "MEASURED_AT_FIXED_FREQUENCY",
                "stage_b_harm": "REPORTED_SEPARATELY",
                "latency": "QUANTIFIED_IN_ROOT_CAUSES",
                "historical_breadth": "CALLER_MUST_REPORT_DATE_RANGE",
                "missing_source": "LOW_EVIDENCE_COVERAGE_QUANTIFIED",
                "test_set_tuning": False,
            },
        }

    @staticmethod
    def _matched_differences(
        winners: list[dict[str, Any]], failures: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        differences: dict[str, list[float]] = defaultdict(list)
        for winner in winners:
            candidates = [
                row
                for row in failures
                if row.get("stage") == winner.get("stage")
                and abs(
                    float(row.get("entry_market_cap") or 0)
                    - float(winner.get("entry_market_cap") or 0)
                )
                <= max(5_000, float(winner.get("entry_market_cap") or 0) * 0.25)
            ]
            if not candidates:
                continue
            candidates.sort(
                key=lambda row: abs(
                    (
                        _timestamp(str(row["decision_at"])) - _timestamp(str(winner["decision_at"]))
                    ).total_seconds()
                )
            )
            other = candidates[0]
            winner_features, other_features = _numeric_features(winner), _numeric_features(other)
            for name in winner_features.keys() & other_features.keys():
                differences[name].append(winner_features[name] - other_features[name])
        return [
            {
                "feature": name,
                "matched_pairs": len(values),
                "median_difference": statistics.median(values),
            }
            for name, values in sorted(differences.items())
            if values
        ]

    @staticmethod
    def control_autopsy(rows: list[dict[str, Any]]) -> dict[str, Any]:
        selected = [row for row in rows if row.get("control_selected")]
        cohorts = {
            "TRUE_2X": [row for row in selected if float(row.get("peak_multiple") or 0) >= 2],
            "FALSE_POSITIVE": [
                row
                for row in selected
                if float(row.get("peak_multiple") or 0) < 2 and not row.get("terminal_failure")
            ],
            "TERMINAL_FAILURE": [row for row in selected if row.get("terminal_failure")],
            "RIGHT_TAIL_RUNNER": [
                row for row in selected if float(row.get("peak_multiple") or 0) >= 10
            ],
        }
        components: dict[str, dict[str, float | int | None]] = {}
        names = {
            name
            for row in selected
            for name, value in (row.get("control_components") or {}).items()
            if isinstance(value, (int, float))
        }
        for name in sorted(names):
            components[name] = {
                cohort: _median(
                    [
                        float(row["control_components"][name])
                        for row in values
                        if isinstance((row.get("control_components") or {}).get(name), (int, float))
                    ]
                )
                for cohort, values in cohorts.items()
            }
        return {
            "selected": len(selected),
            "cohorts": {name: len(values) for name, values in cohorts.items()},
            "component_medians": components,
        }

    @staticmethod
    def stage_b_autopsy(rows: list[dict[str, Any]]) -> dict[str, Any]:
        stage_a = [row for row in rows if row.get("stage_a_selected")]
        retained = [row for row in stage_a if row.get("stage_b_selected")]
        removed = [row for row in stage_a if not row.get("stage_b_selected")]
        before = _rate([float(row.get("peak_multiple") or 0) >= 2 for row in stage_a])
        after = _rate([float(row.get("peak_multiple") or 0) >= 2 for row in retained])
        return {
            "stage_a": len(stage_a),
            "retained": len(retained),
            "true_positives_removed": sum(
                float(row.get("peak_multiple") or 0) >= 2 for row in removed
            ),
            "false_positives_retained": sum(
                float(row.get("peak_multiple") or 0) < 2 for row in retained
            ),
            "2x_precision_before": before,
            "2x_precision_after": after,
            "decision": (
                "REJECT_STAGE_B"
                if before is not None and after is not None and after <= before
                else "VALIDATION_PENDING"
            ),
        }

    def discover_hypotheses(
        self,
        development: list[dict[str, Any]],
        validation: list[dict[str, Any]],
        model: dict[str, Any],
    ) -> list[dict[str, Any]]:
        findings = []
        for name, spec in sorted(
            model["weights"].items(), key=lambda item: abs(item[1]["effect"]), reverse=True
        )[:20]:
            hypothesis_id = (
                "HYP-"
                + hashlib.sha256(
                    f"{name}:{model['target']}:{len(development)}".encode()
                ).hexdigest()[:12]
            )
            validation_winner = [
                _numeric_features(row).get(name)
                for row in validation
                if float(row.get("peak_multiple") or 0) >= model["target"]
                and name in _numeric_features(row)
            ]
            validation_other = [
                _numeric_features(row).get(name)
                for row in validation
                if float(row.get("peak_multiple") or 0) < model["target"]
                and name in _numeric_features(row)
            ]
            validation_effect = None
            if validation_winner and validation_other:
                values = [*validation_winner, *validation_other]
                scale = statistics.pstdev(values)
                if scale:
                    validation_effect = (
                        statistics.mean(validation_winner) - statistics.mean(validation_other)
                    ) / scale
            same_direction = (
                validation_effect is not None and spec["effect"] * validation_effect > 0
            )
            status = "CHALLENGER" if same_direction else "REJECTED"
            record = {
                "hypothesis_id": hypothesis_id,
                "feature": name,
                "development_effect": spec["effect"],
                "validation_effect": validation_effect,
                "development_sample": spec["winner_sample"] + spec["other_sample"],
                "validation_sample": len(validation_winner) + len(validation_other),
                "status": status,
            }
            self.store.conn.execute(
                "INSERT INTO hypothesis_registry_v15 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(hypothesis_id) DO UPDATE SET validation_performance_json=excluded.validation_performance_json,"
                "status=excluded.status,evidence_json=excluded.evidence_json",
                (
                    hypothesis_id,
                    datetime.now(UTC).isoformat(),
                    "DEVELOPMENT_CHRONOLOGICAL",
                    f"{model['target']}X",
                    record["development_sample"],
                    spec["effect"],
                    None,
                    "MEDIUM",
                    _json([name]),
                    _json(
                        {
                            "development": record["development_sample"],
                            "validation": record["validation_sample"],
                        }
                    ),
                    _json({"effect": spec["effect"]}),
                    _json({"effect": validation_effect}),
                    status,
                    _json({"human_approval_required": True}),
                ),
            )
            findings.append(record)
        return findings

    def record_drift(
        self,
        *,
        observed_at: str,
        metric: str,
        baseline: list[float],
        current: list[float],
        drift_type: str,
        sustained_periods: int,
    ) -> dict[str, Any]:
        if drift_type not in {
            "DATA_DRIFT",
            "CONCEPT_DRIFT",
            "CALIBRATION_DRIFT",
            "PROVIDER_DRIFT",
        }:
            raise ValueError("unknown drift type")
        if not baseline or not current:
            state, distance = "INSUFFICIENT_SAMPLE", None
        else:
            scale = statistics.pstdev(baseline) or 1.0
            distance = (statistics.median(current) - statistics.median(baseline)) / scale
            state = "SUSTAINED" if abs(distance) >= 1 and sustained_periods >= 3 else "OBSERVE"
        action = "HUMAN_REVIEW" if state == "SUSTAINED" else "NO_RETRAIN"
        self.store.conn.execute(
            "INSERT OR REPLACE INTO drift_observations_v15 VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                observed_at,
                drift_type,
                metric,
                _median(baseline),
                _median(current),
                distance,
                len(current),
                sustained_periods,
                action,
                _json({"state": state, "automatic_retraining": False}),
            ),
        )
        self.store.conn.commit()
        return {"state": state, "distance": distance, "action": action}

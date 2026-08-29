from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from memecoin_bot.models import iso
from memecoin_bot.realtime.decision import derive_freshness, derive_latency

THESIS_VERSION = "runner-thesis-shadow-v1"
ANALOGUE_FEATURE_VERSION = "runner-analogue-vector-v1"


def _json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"), sort_keys=True)


def _loads(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include timezone: {value}")
    return parsed.astimezone(UTC)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(high, max(low, float(value)))


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, value))))


def _positive(value: Any, scale: float) -> float | None:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    return _clamp(1.0 - math.exp(-max(0.0, float(value)) / max(scale, 1e-9)))


def _signed_positive(value: Any, scale: float) -> float | None:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    return _clamp(0.5 + 0.5 * math.tanh(float(value) / max(scale, 1e-9)))


def _ratio(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    return _clamp(float(value))


def _path(value: dict[str, Any], *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


@dataclass(frozen=True, slots=True)
class RunnerThesis:
    thesis_id: str
    token_id: int
    prior_thesis_id: str | None
    trigger_event_id: str | None
    decision_timestamp: str
    available_at: str
    thesis_type: str
    formation_reason: str
    state: str
    stage: str
    expected_horizon: str
    supporting_evidence: list[dict[str, Any]]
    contradictory_evidence: list[dict[str, Any]]
    unresolved_risks: list[str]
    evidence_freshness: dict[str, Any]
    heuristic_runner_score: float
    heuristic_failure_score: float
    heuristic_actionability_score: float
    confidence: float
    uncertainty: float
    analogous_successes: list[dict[str, Any]]
    analogous_failures: list[dict[str, Any]]
    invalidation_conditions: list[str]
    next_observation_required: list[str]
    call_readiness: str
    feature_vector: dict[str, float]
    latency: dict[str, Any]
    score_semantics: str = "HEURISTIC_NOT_CALIBRATED"
    thesis_version: str = THESIS_VERSION
    public_route: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # Compatibility properties only. The authoritative RunnerDecision never
    # consumes these as calibrated probabilities.
    @property
    def runner_probability(self) -> float:
        return self.heuristic_runner_score

    @property
    def failure_probability(self) -> float:
        return self.heuristic_failure_score

    @property
    def actionable_probability(self) -> float:
        return self.heuristic_actionability_score


@dataclass(frozen=True, slots=True)
class _Component:
    name: str
    value: float
    weight: float
    raw_value: Any
    source: str
    rationale: str


@dataclass(frozen=True, slots=True)
class _Archetype:
    name: str
    score: float
    coverage: float
    components: tuple[_Component, ...]


class RunnerThesisEngine:
    """Persistent runner-first reasoning with a physically shadow-only route.

    Version one deliberately expresses measurable hypotheses rather than claiming
    a trained production model. Its frozen prospective decisions can later be
    evaluated chronologically without editing what was known at decision time.
    """

    def __init__(self, store: Any):
        self.store = store

    def latest(self, token_id: int, available_at: str) -> dict[str, Any] | None:
        row = self.store.conn.execute(
            "SELECT * FROM runner_theses_v15 WHERE token_id=? AND available_at<=? "
            "ORDER BY decision_timestamp DESC,created_at DESC LIMIT 1",
            (token_id, available_at),
        ).fetchone()
        return self._row(row) if row else None

    def evaluate(
        self,
        token_id: int,
        decision_timestamp: str,
        feature: dict[str, Any],
        *,
        trigger_event_id: str | None = None,
        entry_market_cap: float | None = None,
        entry_price: float | None = None,
        runtime_timestamps: dict[str, str] | None = None,
    ) -> RunnerThesis:
        model_started_at = iso()
        _timestamp(decision_timestamp)
        token = self.store.conn.execute(
            "SELECT chain,token_address FROM tokens WHERE id=?", (token_id,)
        ).fetchone()
        if not token:
            raise KeyError(f"token {token_id} does not exist")
        previous_row = self.store.conn.execute(
            "SELECT * FROM runner_theses_v15 WHERE token_id=? AND decision_timestamp<? "
            "ORDER BY decision_timestamp DESC LIMIT 1",
            (token_id, decision_timestamp),
        ).fetchone()
        previous = self._row(previous_row) if previous_row else None
        stage = self._stage(feature)
        vector = self._feature_vector(feature)
        archetypes = self._archetypes(feature, vector)
        best = max(archetypes, key=lambda row: (row.score, row.coverage, row.name))
        supporting, contradictory = self._evidence(best, feature, decision_timestamp)
        risk_score, risks, risk_evidence = self._risk(feature, decision_timestamp)
        contradictory.extend(risk_evidence)
        known_dimensions = len(vector)
        confidence = _clamp(0.12 + 0.055 * known_dimensions + 0.25 * best.coverage)
        runner_score = _sigmoid(-1.55 + 3.2 * best.score + 0.35 * best.coverage)
        failure_score = _sigmoid(-2.35 + 4.1 * risk_score)
        entry_score = self._entry_score(feature, entry_market_cap, entry_price)
        actionability_score = _sigmoid(
            -1.85
            + 2.15 * runner_score
            + 1.65 * entry_score
            + 0.55 * confidence
            - 1.25 * failure_score
        )
        analogues = self.analogues(
            str(token["chain"]), stage, decision_timestamp, vector, best.name
        )
        model_finished_at = iso()
        effective_decision_timestamp = (
            model_finished_at if runtime_timestamps else decision_timestamp
        )
        effective_decision = _timestamp(effective_decision_timestamp)
        state, readiness = self._state(
            previous,
            effective_decision,
            best.name,
            runner_score,
            failure_score,
            actionability_score,
            confidence,
        )
        next_observation = self._next_observation(
            feature, state, best.name, supporting, contradictory
        )
        thesis_id = hashlib.sha256(
            f"{token_id}|{effective_decision_timestamp}|{THESIS_VERSION}".encode()
        ).hexdigest()
        latency = (
            derive_latency(
                source_timestamp=runtime_timestamps["source"],
                received_timestamp=runtime_timestamps["received"],
                normalized_timestamp=runtime_timestamps["normalized"],
                feature_timestamp=runtime_timestamps["feature"],
                model_start_timestamp=model_started_at,
                model_finish_timestamp=model_finished_at,
                decision_timestamp=model_finished_at,
            )
            if runtime_timestamps
            else dict(feature.get("latency") or {"state": "NOT_MEASURED_BY_CALLER"})
        )
        latency["timestamps"] = {
            **dict(runtime_timestamps or {}),
            "model_start": model_started_at,
            "model_finish": model_finished_at,
            "decision": effective_decision_timestamp,
        }
        thesis = RunnerThesis(
            thesis_id=thesis_id,
            token_id=token_id,
            prior_thesis_id=previous["thesis_id"] if previous else None,
            trigger_event_id=trigger_event_id,
            decision_timestamp=effective_decision_timestamp,
            available_at=iso(),
            thesis_type=best.name,
            formation_reason=self._formation_reason(best),
            state=state,
            stage=stage,
            expected_horizon=self._horizon(best.name, stage),
            supporting_evidence=supporting,
            contradictory_evidence=contradictory,
            unresolved_risks=risks,
            evidence_freshness={
                **derive_freshness(
                    list(feature.get("provenance") or []), effective_decision_timestamp
                ),
                "decision_timestamp": effective_decision_timestamp,
                "token_age_seconds": feature.get("token_age_seconds"),
                "trigger_event_id": trigger_event_id,
            },
            heuristic_runner_score=round(runner_score, 6),
            heuristic_failure_score=round(failure_score, 6),
            heuristic_actionability_score=round(actionability_score, 6),
            confidence=round(confidence, 6),
            uncertainty=round(1.0 - confidence, 6),
            analogous_successes=analogues["successes"],
            analogous_failures=analogues["failures"],
            invalidation_conditions=self._invalidation(best.name),
            next_observation_required=next_observation,
            call_readiness=readiness,
            feature_vector=vector,
            latency=latency,
        )
        self._persist(thesis, previous)
        if state == "CALL_READY":
            self._freeze_shadow_call(
                thesis,
                str(token["token_address"]),
                entry_market_cap,
                entry_price,
            )
        return thesis

    def record_analogue(
        self,
        *,
        entity_key: str,
        chain: str,
        thesis_type: str,
        stage: str,
        regime: str,
        decision_timestamp: str,
        outcome_available_at: str,
        features: dict[str, Any],
        peak_multiple: float,
        terminal_failure: bool,
        actionable_at_decision: bool | None,
        entry_market_cap: float | None,
        maximum_adverse_excursion: float | None,
        time_to_2x_seconds: float | None,
        source_dataset: str,
        evidence: dict[str, Any],
    ) -> str:
        decision = _timestamp(decision_timestamp)
        outcome = _timestamp(outcome_available_at)
        if outcome <= decision:
            raise ValueError("outcome_available_at must follow the decision timestamp")
        if peak_multiple < 0 or not math.isfinite(float(peak_multiple)):
            raise ValueError("peak_multiple must be finite and non-negative")
        vector = (
            self._feature_vector(features)
            if "capital_trajectory" in features
            else {
                str(name): float(value)
                for name, value in features.items()
                if isinstance(value, (int, float)) and math.isfinite(float(value))
            }
        )
        analogue_id = hashlib.sha256(
            f"{entity_key}|{decision_timestamp}|{ANALOGUE_FEATURE_VERSION}".encode()
        ).hexdigest()
        with self.store._lock, self.store.conn:
            self.store.conn.execute(
                "INSERT OR IGNORE INTO runner_analogue_memory_v15(analogue_id,entity_key,chain,"
                "thesis_type,stage,regime,decision_timestamp,outcome_available_at,feature_version,"
                "feature_json,peak_multiple,terminal_failure,actionable_at_decision,entry_market_cap,"
                "maximum_adverse_excursion,time_to_2x_seconds,source_dataset,point_in_time_safe,"
                "evidence_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    analogue_id,
                    entity_key,
                    chain,
                    thesis_type,
                    stage,
                    regime,
                    decision_timestamp,
                    outcome_available_at,
                    ANALOGUE_FEATURE_VERSION,
                    _json(vector),
                    float(peak_multiple),
                    int(terminal_failure),
                    None if actionable_at_decision is None else int(actionable_at_decision),
                    entry_market_cap,
                    maximum_adverse_excursion,
                    time_to_2x_seconds,
                    source_dataset,
                    1,
                    _json({**evidence, "outcome_not_used_at_decision": True}),
                ),
            )
        return analogue_id

    def analogues(
        self,
        chain: str,
        stage: str,
        decision_timestamp: str,
        vector: dict[str, float],
        thesis_type: str,
        limit: int = 3,
    ) -> dict[str, list[dict[str, Any]]]:
        if not vector:
            return {"successes": [], "failures": []}
        candidate_limit = max(500, limit * 200)
        rows = self.store.conn.execute(
            "SELECT * FROM runner_analogue_memory_v15 WHERE chain=? AND stage=? "
            "AND outcome_available_at<=? AND point_in_time_safe=1 "
            "ORDER BY (thesis_type=?) DESC,outcome_available_at DESC LIMIT ?",
            (chain, stage, decision_timestamp, thesis_type, candidate_limit),
        )
        ranked: list[dict[str, Any]] = []
        for row in rows:
            other = _loads(row["feature_json"], {})
            common = sorted(vector.keys() & other.keys())
            if len(common) < min(4, len(vector)):
                continue
            distance = math.sqrt(
                sum((float(vector[name]) - float(other[name])) ** 2 for name in common)
                / len(common)
            )
            ranked.append(
                {
                    "analogue_id": row["analogue_id"],
                    "entity_key": row["entity_key"],
                    "thesis_type": row["thesis_type"],
                    "thesis_match": row["thesis_type"] == thesis_type,
                    "decision_timestamp": row["decision_timestamp"],
                    "outcome_available_at": row["outcome_available_at"],
                    "distance": round(distance, 6),
                    "common_dimensions": len(common),
                    "peak_multiple": row["peak_multiple"],
                    "terminal_failure": bool(row["terminal_failure"]),
                    "actionable_at_decision": (
                        bool(row["actionable_at_decision"])
                        if row["actionable_at_decision"] is not None
                        else None
                    ),
                    "source_dataset": row["source_dataset"],
                }
            )
        ranked.sort(
            key=lambda row: (
                not row["thesis_match"],
                row["distance"],
                row["entity_key"],
            )
        )
        return {
            "successes": [
                row for row in ranked if row["peak_multiple"] >= 2 and not row["terminal_failure"]
            ][:limit],
            "failures": [
                row for row in ranked if row["peak_multiple"] < 2 or row["terminal_failure"]
            ][:limit],
        }

    def settle_shadow_call(
        self,
        shadow_call_id: str,
        *,
        outcome_available_at: str,
        peak_multiple: float,
        maximum_adverse_excursion: float | None,
        terminal_failure: bool,
        time_to_2x_seconds: float | None,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """Freeze a matured outcome, reflection, and time-safe analogue exactly once."""

        call = self.store.conn.execute(
            "SELECT c.*,t.chain,t.token_address,h.feature_vector_json,h.supporting_evidence_json,"
            "h.contradictory_evidence_json FROM prospective_shadow_calls_v15 c "
            "JOIN tokens t ON t.id=c.token_id JOIN runner_theses_v15 h ON h.thesis_id=c.thesis_id "
            "WHERE c.shadow_call_id=?",
            (shadow_call_id,),
        ).fetchone()
        if not call:
            raise KeyError(f"shadow call does not exist: {shadow_call_id}")
        if _timestamp(outcome_available_at) <= _timestamp(call["frozen_at"]):
            raise ValueError("outcome_available_at must follow the frozen shadow call")
        if peak_multiple < 0 or not math.isfinite(float(peak_multiple)):
            raise ValueError("peak_multiple must be finite and non-negative")
        if maximum_adverse_excursion is not None and not math.isfinite(
            float(maximum_adverse_excursion)
        ):
            raise ValueError("maximum_adverse_excursion must be finite when supplied")
        existing = self.store.conn.execute(
            "SELECT * FROM prospective_shadow_outcomes_v15 WHERE shadow_call_id=?",
            (shadow_call_id,),
        ).fetchone()
        if existing:
            same_adverse = (
                existing["maximum_adverse_excursion"] is None and maximum_adverse_excursion is None
            ) or (
                existing["maximum_adverse_excursion"] is not None
                and maximum_adverse_excursion is not None
                and math.isclose(
                    float(existing["maximum_adverse_excursion"]),
                    float(maximum_adverse_excursion),
                )
            )
            same = (
                str(existing["outcome_available_at"]) == outcome_available_at
                and math.isclose(float(existing["peak_multiple"]), float(peak_multiple))
                and bool(existing["terminal_failure"]) == terminal_failure
                and same_adverse
            )
            if not same:
                raise ValueError("shadow outcome conflicts with the immutable recorded outcome")
        reached = {target: peak_multiple >= target for target in (2, 5, 10, 20, 50)}
        with self.store._lock, self.store.conn:
            self.store.conn.execute(
                "INSERT OR IGNORE INTO prospective_shadow_outcomes_v15 VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    shadow_call_id,
                    outcome_available_at,
                    float(peak_multiple),
                    int(reached[2]),
                    int(reached[5]),
                    int(reached[10]),
                    int(reached[20]),
                    int(reached[50]),
                    maximum_adverse_excursion,
                    int(terminal_failure),
                    _json({**evidence, "recorded_after_outcome_maturity": True}),
                ),
            )
        self.record_analogue(
            entity_key=str(call["token_address"]),
            chain=str(call["chain"]),
            thesis_type=str(call["thesis_type"]),
            stage=str(call["stage"]),
            regime=str(evidence.get("regime") or "UNKNOWN"),
            decision_timestamp=str(call["frozen_at"]),
            outcome_available_at=outcome_available_at,
            features=_loads(call["feature_vector_json"], {}),
            peak_multiple=float(peak_multiple),
            terminal_failure=terminal_failure,
            actionable_at_decision=str(call["entry_state"]) == "OPEN",
            entry_market_cap=call["entry_market_cap"],
            maximum_adverse_excursion=maximum_adverse_excursion,
            time_to_2x_seconds=time_to_2x_seconds,
            source_dataset="prospective-shadow-v15",
            evidence={"shadow_call_id": shadow_call_id, **evidence},
        )
        false_positive = bool(
            float(call["actionable_probability"]) >= 0.56 and (not reached[2] or terminal_failure)
        )
        brier = (float(call["runner_probability"]) - int(reached[2])) ** 2
        error_class = (
            "FALSE_POSITIVE"
            if false_positive
            else "TRUE_RUNNER"
            if reached[2] and not terminal_failure
            else "CORRECT_ABSTENTION_TIER"
        )
        root_cause = {
            "supporting_at_decision": _loads(call["supporting_evidence_json"], []),
            "contradictory_at_decision": _loads(call["contradictory_evidence_json"], []),
            "terminal_failure": terminal_failure,
            "maximum_adverse_excursion": maximum_adverse_excursion,
        }
        counterfactual = {
            "required_next_time": (
                "wait for stronger sell absorption or lower linked-flow risk"
                if false_positive
                else "preserve thesis and test earlier copyable confirmation"
                if reached[2]
                else "retain as a matched failure analogue"
            ),
            "automatic_weight_change": False,
            "human_approval_required_for_model_change": True,
        }
        reflection_id = hashlib.sha256(f"{shadow_call_id}|reflection-v1".encode()).hexdigest()
        with self.store._lock, self.store.conn:
            self.store.conn.execute(
                "INSERT OR IGNORE INTO runner_reflections_v15 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    reflection_id,
                    shadow_call_id,
                    call["token_id"],
                    call["thesis_type"],
                    call["runner_probability"],
                    call["failure_probability"],
                    call["actionable_probability"],
                    int(reached[2]),
                    float(peak_multiple),
                    brier,
                    error_class,
                    int(false_positive),
                    _json(root_cause),
                    _json(counterfactual),
                    outcome_available_at,
                    iso(),
                ),
            )
        return {
            "shadow_call_id": shadow_call_id,
            "reflection_id": reflection_id,
            "peak_multiple": float(peak_multiple),
            "reached": {f"{target}x": value for target, value in reached.items()},
            "terminal_failure": terminal_failure,
            "brier_2x": brier,
            "error_class": error_class,
            "false_positive": false_positive,
        }

    def shadow_scorecard(self) -> dict[str, Any]:
        row = self.store.conn.execute(
            "SELECT COUNT(*) matured,SUM(reached_2x) reached_2x,SUM(reached_5x) reached_5x,"
            "SUM(reached_10x) reached_10x,SUM(reached_20x) reached_20x,"
            "SUM(reached_50x) reached_50x,SUM(terminal_failure) terminal_failures,"
            "AVG(r.brier_2x) brier_2x FROM prospective_shadow_outcomes_v15 o "
            "JOIN runner_reflections_v15 r USING(shadow_call_id)"
        ).fetchone()
        matured = int(row["matured"] or 0)
        pending = int(
            self.store.conn.execute(
                "SELECT COUNT(*) FROM prospective_shadow_calls_v15 c LEFT JOIN "
                "prospective_shadow_outcomes_v15 o USING(shadow_call_id) "
                "WHERE o.shadow_call_id IS NULL"
            ).fetchone()[0]
        )
        return {
            "matured": matured,
            "pending": pending,
            **{
                f"{target}x_precision": (
                    int(row[f"reached_{target}x"] or 0) / matured if matured else None
                )
                for target in (2, 5, 10, 20, 50)
            },
            "terminal_failure_rate": (
                int(row["terminal_failures"] or 0) / matured if matured else None
            ),
            "brier_2x": row["brier_2x"],
            "production_gate_eligible": False,
            "reason": "prospective sample and duration require explicit external gate evaluation",
        }

    @staticmethod
    def _stage(feature: dict[str, Any]) -> str:
        migration = str(feature.get("migration_state") or "")
        age = float(feature.get("token_age_seconds") or 0)
        if migration in {"MIGRATION_STARTED", "MIGRATED", "POST_MIGRATION"}:
            return "MIGRATION" if migration == "MIGRATION_STARTED" else "POST_MIGRATION"
        if age <= 30:
            return "LAUNCH"
        if age <= 120:
            return "EARLY_CURVE"
        if age <= 600:
            return "MID_CURVE"
        if age <= 3_600:
            return "LATE_CURVE"
        return "REVIVAL"

    @staticmethod
    def _feature_vector(feature: dict[str, Any]) -> dict[str, float]:
        capital = feature.get("capital_trajectory") or {}
        buyer = feature.get("buyer_arrival") or {}
        selling = feature.get("first_sell") or {}
        activity = feature.get("activity_adjustment") or {}
        migration = feature.get("migration_continuity") or {}
        consensus = _path(feature, "actor_intelligence", "wallet_consensus") or {}
        funder = _path(feature, "actor_intelligence", "funder") or {}
        candidates: dict[str, tuple[Any, Callable[[Any], float | None]]] = {
            "capital_velocity": (
                capital.get("real_sol_velocity"),
                lambda value: _signed_positive(value, 0.04),
            ),
            "capital_acceleration": (
                capital.get("real_sol_acceleration"),
                lambda value: _signed_positive(value, 0.004),
            ),
            "capital_persistence": (capital.get("capital_persistence"), _ratio),
            "curve_progress_velocity": (
                capital.get("curve_progress_velocity"),
                lambda value: _signed_positive(value, 0.015),
            ),
            "buyer_velocity": (
                buyer.get("new_buyers_per_second"),
                lambda value: _positive(value, 0.12),
            ),
            "independent_buyer_velocity": (
                buyer.get("independent_new_buyers_per_second"),
                lambda value: _positive(value, 0.08),
            ),
            "buyer_retention": (buyer.get("buyer_retention"), _ratio),
            "buyer_replacement": (
                buyer.get("buyer_replacement"),
                lambda value: _positive(value, 4.0),
            ),
            "sell_absorption": (
                selling.get("sell_absorption_rate"),
                lambda value: _positive(value, 1.5),
            ),
            "buyers_after_sell": (
                selling.get("buyers_after_first_meaningful_sell"),
                lambda value: _positive(value, 5.0),
            ),
            "wash_cleanliness": (
                activity.get("wash_probability"),
                lambda value: 1 - _ratio(value) if _ratio(value) is not None else None,
            ),
            "wallet_independence": (
                consensus.get("linked_wallet_share"),
                lambda value: 1 - _ratio(value) if _ratio(value) is not None else None,
            ),
            "smart_consensus": (
                consensus.get("independent_smart_wallet_count"),
                lambda value: _positive(value, 2.0),
            ),
            "funder_independence": (funder.get("funder_independence"), _ratio),
            "migration_flow": (migration.get("flow_survival"), lambda value: _positive(value, 1.0)),
            "migration_buyers": (migration.get("buyer_retention"), _ratio),
            "migration_liquidity": (
                migration.get("liquidity_continuity"),
                lambda value: _positive(value, 1.0),
            ),
        }
        output: dict[str, float] = {}
        for name, (raw, transform) in candidates.items():
            normalized = transform(raw)
            if normalized is not None and math.isfinite(float(normalized)):
                output[name] = round(_clamp(normalized), 8)
        return output

    @staticmethod
    def _archetypes(feature: dict[str, Any], vector: dict[str, float]) -> list[_Archetype]:
        def build(name: str, specs: tuple[tuple[str, float, str, str], ...]) -> _Archetype:
            components = tuple(
                _Component(key, vector[key], weight, vector[key], source, rationale)
                for key, weight, source, rationale in specs
                if key in vector
            )
            possible = sum(spec[1] for spec in specs)
            observed = sum(row.weight for row in components)
            score = (
                sum(row.value * row.weight for row in components) / observed if observed else 0.0
            )
            return _Archetype(name, score, observed / possible if possible else 0.0, components)

        archetypes = [
            build(
                "ORGANIC_ACCELERATION",
                (
                    ("capital_velocity", 1.3, "native_curve", "real capital is arriving"),
                    (
                        "capital_acceleration",
                        1.0,
                        "native_curve",
                        "capital arrival is accelerating",
                    ),
                    (
                        "independent_buyer_velocity",
                        1.2,
                        "wallet_graph",
                        "independent buyers are arriving",
                    ),
                    (
                        "buyer_replacement",
                        0.8,
                        "trade_sequence",
                        "new cohorts replace early buyers",
                    ),
                    (
                        "wash_cleanliness",
                        0.7,
                        "activity_adjustment",
                        "flow is not explained by wash evidence",
                    ),
                ),
            ),
            build(
                "SMART_MONEY_CONSENSUS",
                (
                    ("smart_consensus", 1.4, "wallet_strategy", "multiple skilled wallets entered"),
                    (
                        "wallet_independence",
                        1.3,
                        "wallet_graph",
                        "wallet confirmations are independent",
                    ),
                    ("funder_independence", 1.0, "funder_graph", "funding sources are independent"),
                    (
                        "independent_buyer_velocity",
                        0.8,
                        "wallet_graph",
                        "broader demand confirms the wallets",
                    ),
                ),
            ),
            build(
                "MIGRATION_CONTINUATION",
                (
                    ("migration_liquidity", 1.2, "migration", "liquidity survived migration"),
                    ("migration_flow", 1.2, "migration", "buying flow survived migration"),
                    ("migration_buyers", 1.0, "migration", "buyers remained after migration"),
                    (
                        "sell_absorption",
                        0.8,
                        "trade_sequence",
                        "post-migration selling was absorbed",
                    ),
                ),
            ),
            build(
                "SECOND_LEG_SELL_ABSORPTION",
                (
                    ("sell_absorption", 1.4, "trade_sequence", "fresh buying absorbed sellers"),
                    (
                        "buyers_after_sell",
                        1.1,
                        "trade_sequence",
                        "new buyers arrived after first sell",
                    ),
                    ("buyer_replacement", 0.9, "trade_sequence", "a replacement cohort formed"),
                    ("capital_persistence", 0.8, "native_curve", "capital remained persistent"),
                ),
            ),
            build(
                "EARLY_CURVE_ACCELERATION",
                (
                    (
                        "curve_progress_velocity",
                        1.2,
                        "native_curve",
                        "curve progress is moving quickly",
                    ),
                    ("capital_velocity", 1.2, "native_curve", "real SOL is arriving"),
                    ("buyer_velocity", 1.0, "trade_sequence", "early buyer arrival is strong"),
                    (
                        "wash_cleanliness",
                        0.7,
                        "activity_adjustment",
                        "early flow is not wash-dominated",
                    ),
                ),
            ),
            build(
                "REVIVAL_REACCELERATION",
                (
                    ("capital_acceleration", 1.2, "native_curve", "capital has reaccelerated"),
                    ("buyer_replacement", 1.0, "trade_sequence", "a fresh buyer cohort arrived"),
                    ("sell_absorption", 0.9, "trade_sequence", "returning demand absorbed supply"),
                    (
                        "wallet_independence",
                        0.7,
                        "wallet_graph",
                        "the revival is independently funded",
                    ),
                ),
            ),
        ]
        migration_state = str(feature.get("migration_state") or "")
        age = float(feature.get("token_age_seconds") or 0)
        adjusted = []
        for archetype in archetypes:
            score = archetype.score
            if archetype.name == "MIGRATION_CONTINUATION" and migration_state == "PRE_MIGRATION":
                score *= 0.35
            if archetype.name == "REVIVAL_REACCELERATION" and age < 1_800:
                score *= 0.45
            adjusted.append(
                _Archetype(archetype.name, score, archetype.coverage, archetype.components)
            )
        return adjusted

    @staticmethod
    def _evidence(
        archetype: _Archetype,
        feature: dict[str, Any],
        decision_timestamp: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        supporting = [
            {
                "evidence": row.name,
                "strength": round(row.value, 6),
                "weight": row.weight,
                "value": row.raw_value,
                "source": row.source,
                "available_at": decision_timestamp,
                "rationale": row.rationale,
            }
            for row in sorted(archetype.components, key=lambda value: -value.value * value.weight)
            if row.value >= 0.55
        ]
        contradictory = []
        capital = feature.get("capital_trajectory") or {}
        buyer = feature.get("buyer_arrival") or {}
        selling = feature.get("first_sell") or {}
        facts = (
            ("capital_reversal", capital.get("capital_reversal"), "native_curve", 0.85),
            (
                "buyer_deceleration",
                buyer.get("buyer_deceleration_observed"),
                "trade_sequence",
                0.55,
            ),
            (
                "first_sell_not_absorbed",
                selling.get("first_sell_absorbed") is False,
                "trade_sequence",
                0.8,
            ),
        )
        for name, active, source, strength in facts:
            if active:
                contradictory.append(
                    {
                        "evidence": name,
                        "strength": strength,
                        "source": source,
                        "available_at": decision_timestamp,
                    }
                )
        return supporting, contradictory

    @staticmethod
    def _risk(
        feature: dict[str, Any], decision_timestamp: str
    ) -> tuple[float, list[str], list[dict[str, Any]]]:
        activity = feature.get("activity_adjustment") or {}
        buyer = feature.get("buyer_arrival") or {}
        capital = feature.get("capital_trajectory") or {}
        selling = feature.get("first_sell") or {}
        funder = _path(feature, "actor_intelligence", "funder") or {}
        risks: list[tuple[str, float, Any, str]] = []

        def add(name: str, value: Any, weight: float, source: str) -> None:
            normalized = _ratio(value)
            if normalized is not None:
                risks.append((name, normalized * weight, value, source))

        add("wash_probability", activity.get("wash_probability"), 1.0, "activity_adjustment")
        add("linked_wallet_share", activity.get("linked_wallet_share"), 0.8, "wallet_graph")
        add("bundle_linked_share", activity.get("bundle_linked_share"), 0.55, "bundle_evidence")
        add("creator_buyer_share", buyer.get("creator_buyer_share"), 0.75, "trade_sequence")
        add("creator_funder_link", funder.get("creator_link_score"), 0.9, "funder_graph")
        if capital.get("capital_reversal"):
            risks.append(("capital_reversal", 0.7, True, "native_curve"))
        if selling.get("first_sell_absorbed") is False:
            risks.append(("unabsorbed_first_sell", 0.65, False, "trade_sequence"))
        score = (
            _clamp(sum(row[1] for row in risks) / max(1.0, sum(min(1.0, row[1]) for row in risks)))
            if risks
            else 0.15
        )
        unresolved = []
        coverage = feature.get("coverage") or {}
        for name in ("wallet_linkage", "funder", "bundle", "migration"):
            if not coverage.get(name):
                unresolved.append(f"UNKNOWN_{name.upper()}")
        evidence = [
            {
                "evidence": name,
                "strength": round(weighted, 6),
                "value": raw,
                "source": source,
                "available_at": decision_timestamp,
            }
            for name, weighted, raw, source in risks
            if weighted >= 0.25
        ]
        return score, unresolved, evidence

    @staticmethod
    def _entry_score(
        feature: dict[str, Any], entry_market_cap: float | None, entry_price: float | None
    ) -> float:
        age = float(feature.get("token_age_seconds") or 0)
        age_score = math.exp(-max(0.0, age - 120.0) / 1_800.0)
        monitoring = str(_path(feature, "monitoring", "state") or "")
        activity = _path(feature, "activity_adjustment", "adjusted_volume_sol")
        liquidity_proxy = _positive(activity, 5.0) if activity is not None else None
        state_score = {"GENESIS": 0.9, "HOT": 0.85, "WARM": 0.65, "COLD": 0.35}.get(monitoring, 0.5)
        market_cap_score = 0.6
        if entry_market_cap is not None and entry_market_cap > 0:
            market_cap_score = math.exp(-max(0.0, math.log10(entry_market_cap) - 6.0) / 2.0)
        observed = [age_score, state_score, market_cap_score]
        if liquidity_proxy is not None:
            observed.append(liquidity_proxy)
        if entry_price is not None and entry_price <= 0:
            return 0.0
        return _clamp(sum(observed) / len(observed))

    def _state(
        self,
        previous: dict[str, Any] | None,
        decision: datetime,
        thesis_type: str,
        runner: float,
        failure: float,
        actionable: float,
        confidence: float,
    ) -> tuple[str, str]:
        if previous:
            frozen = self.store.conn.execute(
                "SELECT 1 FROM prospective_shadow_calls_v15 WHERE token_id=? AND model_version=?",
                (previous["token_id"], THESIS_VERSION),
            ).fetchone()
            if frozen and runner >= 0.55 and failure <= 0.5:
                return "CALLED", "FROZEN_SHADOW_CALL"
        if failure >= 0.78 or (previous and runner <= 0.2):
            return "INVALIDATED", "INVALID"
        if runner >= 0.52 and actionable < 0.28:
            return "ENTRY_NOT_COPYABLE", "ENTRY_CLOSED"
        high = runner >= 0.67 and failure <= 0.38 and actionable >= 0.56 and confidence >= 0.42
        if high:
            reconfirmed = False
            if previous and previous["thesis_type"] == thesis_type:
                elapsed = (decision - _timestamp(previous["decision_timestamp"])).total_seconds()
                reconfirmed = (
                    0 < elapsed <= 90
                    and float(previous["runner_probability"]) >= 0.6
                    and float(previous["failure_probability"]) <= 0.45
                )
            return (
                ("CALL_READY", "SHADOW_CALL_READY") if reconfirmed else ("CONFIRMED", "RECONFIRM")
            )
        if previous:
            delta = runner - float(previous["runner_probability"])
            if delta <= -0.08 or failure - float(previous["failure_probability"]) >= 0.12:
                return "WEAKENING", "NOT_READY"
            if runner >= 0.52 and delta >= 0.025:
                return "STRENGTHENING", "NOT_READY"
        if runner >= 0.48:
            return "THESIS_FORMING", "NOT_READY"
        return ("OBSERVING", "NOT_READY") if previous else ("DISCOVERED", "NOT_READY")

    @staticmethod
    def _formation_reason(archetype: _Archetype) -> str:
        strongest = sorted(
            archetype.components, key=lambda row: (-row.value * row.weight, row.name)
        )[:3]
        return (
            "; ".join(row.rationale for row in strongest)
            or "insufficient evidence to form a thesis"
        )

    @staticmethod
    def _horizon(thesis_type: str, stage: str) -> str:
        if thesis_type == "MIGRATION_CONTINUATION":
            return "15m-2h"
        if thesis_type == "REVIVAL_REACCELERATION" or stage == "REVIVAL":
            return "30m-24h"
        if stage in {"LAUNCH", "EARLY_CURVE"}:
            return "2m-60m"
        return "10m-6h"

    @staticmethod
    def _invalidation(thesis_type: str) -> list[str]:
        common = [
            "real capital reverses and does not recover",
            "independent buyer arrival collapses",
            "creator-linked or coordinated flow explains the apparent demand",
            "entry ceases to be liquid or copyable",
        ]
        if thesis_type == "MIGRATION_CONTINUATION":
            common.append("post-migration liquidity or buyer continuity fails")
        if thesis_type == "SECOND_LEG_SELL_ABSORPTION":
            common.append("the next meaningful sell is not absorbed")
        return common

    @staticmethod
    def _next_observation(
        feature: dict[str, Any],
        state: str,
        thesis_type: str,
        supporting: list[dict[str, Any]],
        contradictory: list[dict[str, Any]],
    ) -> list[str]:
        coverage = feature.get("coverage") or {}
        required = []
        if not coverage.get("first_sell"):
            required.append("observe first meaningful sell and immediate absorption")
        if not coverage.get("wallet_linkage"):
            required.append("resolve whether leading buyers are independently funded")
        if not coverage.get("real_sol_reserve"):
            required.append("obtain a native real-reserve observation")
        if thesis_type == "MIGRATION_CONTINUATION" and not coverage.get("migration"):
            required.append("observe the migration and measure post-migration continuity")
        if state == "CONFIRMED":
            required.insert(0, "rapidly reconfirm the same thesis before freezing a shadow call")
        if state == "WEAKENING":
            required.insert(0, "determine whether the contradiction persists or recovers")
        if not supporting:
            required.append("collect positive runner evidence rather than relying on risk absence")
        if contradictory:
            required.append("test whether current contradictory evidence invalidates the thesis")
        return list(dict.fromkeys(required))[:6]

    def _persist(self, thesis: RunnerThesis, previous: dict[str, Any] | None) -> None:
        with self.store._lock, self.store.conn:
            self.store.conn.execute(
                "INSERT OR IGNORE INTO runner_theses_v15(thesis_id,token_id,prior_thesis_id,"
                "trigger_event_id,decision_timestamp,available_at,thesis_type,formation_reason,state,"
                "stage,expected_horizon,supporting_evidence_json,contradictory_evidence_json,"
                "unresolved_risks_json,evidence_freshness_json,runner_probability,failure_probability,"
                "actionable_probability,confidence,uncertainty,analogous_successes_json,"
                "analogous_failures_json,invalidation_conditions_json,next_observation_required_json,"
                "call_readiness,feature_vector_json,thesis_version,public_route,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    thesis.thesis_id,
                    thesis.token_id,
                    thesis.prior_thesis_id,
                    thesis.trigger_event_id,
                    thesis.decision_timestamp,
                    thesis.available_at,
                    thesis.thesis_type,
                    thesis.formation_reason,
                    thesis.state,
                    thesis.stage,
                    thesis.expected_horizon,
                    _json(thesis.supporting_evidence),
                    _json(thesis.contradictory_evidence),
                    _json(thesis.unresolved_risks),
                    _json(thesis.evidence_freshness),
                    thesis.heuristic_runner_score,
                    thesis.heuristic_failure_score,
                    thesis.heuristic_actionability_score,
                    thesis.confidence,
                    thesis.uncertainty,
                    _json(thesis.analogous_successes),
                    _json(thesis.analogous_failures),
                    _json(thesis.invalidation_conditions),
                    _json(thesis.next_observation_required),
                    thesis.call_readiness,
                    _json(thesis.feature_vector),
                    thesis.thesis_version,
                    0,
                    iso(),
                ),
            )
            self.store.conn.execute(
                "INSERT OR IGNORE INTO runner_thesis_transitions_v15(token_id,thesis_id,prior_thesis_id,"
                "transitioned_at,prior_state,new_state,runner_probability_delta,failure_probability_delta,"
                "actionability_probability_delta,reason_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    thesis.token_id,
                    thesis.thesis_id,
                    thesis.prior_thesis_id,
                    thesis.decision_timestamp,
                    previous["state"] if previous else None,
                    thesis.state,
                    thesis.heuristic_runner_score - float(previous["runner_probability"])
                    if previous
                    else None,
                    thesis.heuristic_failure_score - float(previous["failure_probability"])
                    if previous
                    else None,
                    thesis.heuristic_actionability_score - float(previous["actionable_probability"])
                    if previous
                    else None,
                    _json(
                        {
                            "formation_reason": thesis.formation_reason,
                            "supporting": [row["evidence"] for row in thesis.supporting_evidence],
                            "contradictory": [
                                row["evidence"] for row in thesis.contradictory_evidence
                            ],
                        }
                    ),
                ),
            )

    def _freeze_shadow_call(
        self,
        thesis: RunnerThesis,
        token_address: str,
        entry_market_cap: float | None,
        entry_price: float | None,
    ) -> None:
        shadow_call_id = hashlib.sha256(
            f"{thesis.token_id}|{THESIS_VERSION}|prospective-shadow".encode()
        ).hexdigest()
        with self.store._lock, self.store.conn:
            self.store.conn.execute(
                "INSERT OR IGNORE INTO prospective_shadow_calls_v15 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    shadow_call_id,
                    thesis.token_id,
                    thesis.thesis_id,
                    thesis.decision_timestamp,
                    thesis.thesis_type,
                    thesis.stage,
                    "RESEARCH_SHADOW_CALL",
                    "OPEN" if thesis.heuristic_actionability_score >= 0.56 else "LIMITED",
                    entry_market_cap,
                    entry_price,
                    thesis.heuristic_runner_score,
                    thesis.heuristic_failure_score,
                    thesis.heuristic_actionability_score,
                    thesis.confidence,
                    _json(
                        {
                            "token_address": token_address,
                            "supporting": thesis.supporting_evidence,
                            "contradictory": thesis.contradictory_evidence,
                            "next_observation_required": thesis.next_observation_required,
                            "frozen_at_decision_time": True,
                        }
                    ),
                    _json(thesis.latency),
                    THESIS_VERSION,
                    0,
                    iso(),
                ),
            )

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        value = dict(row)
        for source, target, fallback in (
            ("supporting_evidence_json", "supporting_evidence", []),
            ("contradictory_evidence_json", "contradictory_evidence", []),
            ("unresolved_risks_json", "unresolved_risks", []),
            ("evidence_freshness_json", "evidence_freshness", {}),
            ("analogous_successes_json", "analogous_successes", []),
            ("analogous_failures_json", "analogous_failures", []),
            ("invalidation_conditions_json", "invalidation_conditions", []),
            ("next_observation_required_json", "next_observation_required", []),
            ("feature_vector_json", "feature_vector", {}),
        ):
            value[target] = _loads(value.pop(source, None), fallback)
        value["public_route"] = bool(value.get("public_route"))
        return value

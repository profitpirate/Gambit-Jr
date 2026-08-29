from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from itertools import pairwise
from typing import Any

from memecoin_bot.models import iso

TARGETS = (2, 5, 10, 20, 50)
DECISION_VERSION = "runner-decision-v1"
CHAMPION = "CONTROL_V15"
CHALLENGERS = (
    "SELL_ABSORPTION_V2",
    "SEQUENCE_GBM_2X",
    "HARD_NEGATIVE_2X",
    "MID_5X",
    "RIGHT_TAIL_10X",
)


def _json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"), sort_keys=True)


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include timezone: {value}")
    return parsed.astimezone(UTC)


def _probability(value: Any, name: str) -> float | None:
    if value is None:
        return None
    result = float(value)
    if not 0 <= result <= 1:
        raise ValueError(f"{name} must be between zero and one")
    return result


class RouteState(StrEnum):
    HOLD = "HOLD"
    REJECTED = "REJECTED"
    RESEARCH_SHADOW_CALL = "RESEARCH_SHADOW_CALL"
    OPERATOR_SHADOW_ALERT = "OPERATOR_SHADOW_ALERT"
    PUBLIC_ALERT = "PUBLIC_ALERT"


@dataclass(frozen=True, slots=True)
class RunnerDecision:
    decision_id: str
    token_id: int
    token_address: str
    chain: str
    decision_at: str
    available_at: str
    stage: str
    thesis_type: str
    runner_probabilities: dict[str, float | None]
    failure_probability: float | None
    actionability_probability: float | None
    confidence: float
    uncertainty: float
    entry_state: dict[str, Any]
    supporting_evidence: list[dict[str, Any]]
    contradicting_evidence: list[dict[str, Any]]
    critical_unknowns: list[str]
    provider_health: dict[str, Any]
    evidence_freshness: dict[str, Any]
    latency: dict[str, Any]
    model_versions: dict[str, str]
    tier: str
    route_state: RouteState
    decision_reason: str
    champion: str
    controls: dict[str, Any]
    heuristic_scores: dict[str, float | None]
    evaluation_universe_hash: str | None = None

    def __post_init__(self) -> None:
        _timestamp(self.decision_at)
        _timestamp(self.available_at)
        if not 0 <= self.confidence <= 1 or not 0 <= self.uncertainty <= 1:
            raise ValueError("confidence and uncertainty must be probabilities")
        values = []
        for target in TARGETS:
            key = f"p_{target}x"
            value = _probability(self.runner_probabilities.get(key), key)
            if value is not None:
                values.append((target, value))
        if any(right > left for (_, left), (_, right) in pairwise(values)):
            raise ValueError("runner target probabilities must be nested monotonically")
        _probability(self.failure_probability, "failure_probability")
        _probability(self.actionability_probability, "actionability_probability")

    @property
    def routes_alert(self) -> bool:
        return self.route_state in {
            RouteState.OPERATOR_SHADOW_ALERT,
            RouteState.PUBLIC_ALERT,
        }

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["route_state"] = str(self.route_state)
        return value


def derive_freshness(
    evidence: list[Mapping[str, Any]],
    decision_at: str,
    *,
    default_sla_seconds: float = 120,
) -> dict[str, Any]:
    """Derive decision-time availability and staleness from provenance timestamps."""
    decision = _timestamp(decision_at)
    rows: list[dict[str, Any]] = []
    unavailable: list[str] = []
    stale: list[str] = []
    for index, item in enumerate(evidence):
        name = str(item.get("field_name") or item.get("source") or f"evidence_{index}")
        source_at = item.get("source_timestamp") or item.get("observed_at")
        received_at = item.get("received_timestamp")
        available_at = item.get("available_timestamp") or item.get("retrieved_at")
        sla = float(item.get("freshness_sla_seconds") or default_sla_seconds)
        if not available_at:
            unavailable.append(name)
            rows.append({"name": name, "state": "UNKNOWN_AVAILABILITY", "sla_seconds": sla})
            continue
        available = _timestamp(str(available_at))
        if available > decision:
            unavailable.append(name)
        observed = _timestamp(str(source_at)) if source_at else available
        age = max(0.0, (decision - observed).total_seconds())
        if age > sla:
            stale.append(name)
        rows.append(
            {
                "name": name,
                "source_timestamp": observed.isoformat(),
                "received_timestamp": str(received_at) if received_at else None,
                "available_timestamp": available.isoformat(),
                "available_by_decision": available <= decision,
                "age_at_decision_seconds": age,
                "sla_seconds": sla,
                "state": "STALE" if age > sla else "CURRENT",
            }
        )
    return {
        "all_evidence_available_by_decision": not unavailable,
        "unavailable_evidence": unavailable,
        "stale_evidence_count": len(stale),
        "stale_evidence": stale,
        "evidence": rows,
        "derived": True,
    }


def derive_latency(
    *,
    source_timestamp: str,
    received_timestamp: str,
    normalized_timestamp: str,
    feature_timestamp: str,
    model_start_timestamp: str,
    model_finish_timestamp: str,
    decision_timestamp: str,
    enqueue_timestamp: str | None = None,
    discord_timestamp: str | None = None,
) -> dict[str, float | dict[str, str]]:
    stamps = {
        name: _timestamp(value)
        for name, value in {
            "source": source_timestamp,
            "received": received_timestamp,
            "normalized": normalized_timestamp,
            "feature": feature_timestamp,
            "model_start": model_start_timestamp,
            "model_finish": model_finish_timestamp,
            "decision": decision_timestamp,
        }.items()
    }

    def milliseconds(start: str, end: str) -> float:
        return round(max(0.0, (stamps[end] - stamps[start]).total_seconds() * 1000), 3)

    value: dict[str, float | dict[str, str]] = {
        "source_to_receive_ms": milliseconds("source", "received"),
        "receive_to_normalize_ms": milliseconds("received", "normalized"),
        "normalize_to_feature_ms": milliseconds("normalized", "feature"),
        "feature_to_model_ms": milliseconds("feature", "model_start"),
        "model_to_decision_ms": milliseconds("model_start", "decision"),
        "model_compute_ms": milliseconds("model_start", "model_finish"),
        "source_to_decision_ms": milliseconds("source", "decision"),
    }
    if enqueue_timestamp:
        enqueue = _timestamp(enqueue_timestamp)
        value["decision_to_enqueue_ms"] = round(
            max(0.0, (enqueue - stamps["decision"]).total_seconds() * 1000), 3
        )
    else:
        value["decision_to_enqueue"] = {"state": "NOT_ROUTED"}
    if enqueue_timestamp and discord_timestamp:
        enqueue = _timestamp(enqueue_timestamp)
        discord = _timestamp(discord_timestamp)
        value["enqueue_to_discord_ms"] = round(
            max(0.0, (discord - enqueue).total_seconds() * 1000), 3
        )
        value["source_to_discord_ms"] = round(
            max(0.0, (discord - stamps["source"]).total_seconds() * 1000), 3
        )
    else:
        value["discord_delivery"] = {"state": "NOT_ROUTED"}
    return value


class RunnerDecisionEngine:
    """The sole current decision authority; earlier engines are explicit controls."""

    champion = CHAMPION
    challengers = CHALLENGERS

    def __init__(self, store: Any):
        self.store = store

    def decide(
        self,
        *,
        token_id: int,
        token_address: str,
        chain: str,
        decision_at: str,
        stage: str,
        thesis: Mapping[str, Any] | None,
        v15_control: Mapping[str, Any],
        legacy_control: Mapping[str, Any],
        waiting_reasons: list[str],
        hard_rejections: list[str],
        entry_state: Mapping[str, Any],
        provider_health: Mapping[str, Any],
        provenance: list[Mapping[str, Any]],
        latency: Mapping[str, Any],
        calibrated_models: Mapping[str, Any] | None = None,
        candidate_id: int | None = None,
        trigger_event_id: str | None = None,
        public_alerts_enabled: bool = False,
        operator_shadow_alerts_enabled: bool = False,
        research_only: bool = False,
    ) -> RunnerDecision:
        available_at = iso()
        calibrated = dict(calibrated_models or {})
        calibration_ok = (
            calibrated.get("calibration_state") == "VALIDATED_CHRONOLOGICAL"
            and calibrated.get("approval_state") == "APPROVED_HUMAN_GATED"
            and bool(calibrated.get("evaluation_universe_hash"))
        )
        probabilities = {
            f"p_{target}x": (
                _probability(calibrated.get(f"p_{target}x"), f"p_{target}x")
                if calibration_ok
                else None
            )
            for target in TARGETS
        }
        known = [probabilities[f"p_{target}x"] for target in TARGETS]
        prior = 1.0
        for target, value in zip(TARGETS, known, strict=True):
            if value is not None:
                probabilities[f"p_{target}x"] = min(prior, value)
                prior = probabilities[f"p_{target}x"] or 0.0
        freshness = derive_freshness(provenance, decision_at)
        v15_tier = str(v15_control.get("signal_tier") or "NO_SIGNAL")
        eligible_tiers = {"PREMIUM", "STRONG", "HIGH_RISK_MOMENTUM", "CATALYST_REVIVAL"}
        eligible = v15_tier in eligible_tiers and not waiting_reasons and not hard_rejections
        if hard_rejections:
            route = RouteState.REJECTED
            reason = hard_rejections[0]
        elif research_only:
            route = RouteState.RESEARCH_SHADOW_CALL
            reason = "RESEARCH_EVIDENCE_ONLY"
        elif not eligible:
            route = RouteState.HOLD
            reason = (waiting_reasons or [f"CONTROL_V15_{v15_tier}"])[0]
        elif public_alerts_enabled:
            route = RouteState.PUBLIC_ALERT
            reason = f"{self.champion}_{v15_tier}"
        elif operator_shadow_alerts_enabled:
            route = RouteState.OPERATOR_SHADOW_ALERT
            reason = f"{self.champion}_{v15_tier}_OPERATOR_SHADOW"
        else:
            route = RouteState.HOLD
            reason = "ALERT_ROUTES_DISABLED"
        confidence = float(v15_control.get("evidence_coverage") or 0)
        if confidence > 1:
            confidence /= 100
        confidence = min(1.0, max(0.0, confidence))
        thesis_value = dict(thesis or {})
        decision_id = hashlib.sha256(
            f"{token_id}|{decision_at}|{self.champion}|{DECISION_VERSION}".encode()
        ).hexdigest()
        decision = RunnerDecision(
            decision_id=decision_id,
            token_id=token_id,
            token_address=token_address,
            chain=chain,
            decision_at=decision_at,
            available_at=available_at,
            stage=str(stage),
            thesis_type=str(thesis_value.get("thesis_type") or "CONTROL_V15_NOMINATION"),
            runner_probabilities=probabilities,
            failure_probability=(
                _probability(calibrated.get("failure_probability"), "failure_probability")
                if calibration_ok
                else None
            ),
            actionability_probability=(
                _probability(
                    calibrated.get("actionability_probability"),
                    "actionability_probability",
                )
                if calibration_ok
                else None
            ),
            confidence=confidence,
            uncertainty=round(1.0 - confidence, 6),
            entry_state=dict(entry_state),
            supporting_evidence=list(thesis_value.get("supporting_evidence") or []),
            contradicting_evidence=list(thesis_value.get("contradictory_evidence") or []),
            critical_unknowns=list(
                dict.fromkeys(
                    [
                        *waiting_reasons,
                        *list(v15_control.get("critical_unknowns") or []),
                        *freshness["unavailable_evidence"],
                    ]
                )
            ),
            provider_health=dict(provider_health),
            evidence_freshness=freshness,
            latency=dict(latency),
            model_versions={
                "decision": DECISION_VERSION,
                "champion": self.champion,
                "runner": str(calibrated.get("runner_model_version") or "UNAVAILABLE"),
                "failure": str(calibrated.get("failure_model_version") or "UNAVAILABLE"),
                "actionability": str(
                    calibrated.get("actionability_model_version") or "UNAVAILABLE"
                ),
            },
            tier=v15_tier,
            route_state=route,
            decision_reason=reason,
            champion=self.champion,
            controls={"legacy": dict(legacy_control), "v15": dict(v15_control)},
            heuristic_scores={
                "runner_thesis_score": (
                    thesis_value["heuristic_runner_score"]
                    if thesis_value.get("heuristic_runner_score") is not None
                    else thesis_value.get("runner_probability")
                ),
                "v15_runner_score": v15_control.get("runner_score"),
                "v15_failure_score": v15_control.get("failure_score"),
            },
            evaluation_universe_hash=(
                str(calibrated["evaluation_universe_hash"])
                if calibration_ok and calibrated.get("evaluation_universe_hash")
                else None
            ),
        )
        self._persist(decision, candidate_id, trigger_event_id)
        return decision

    def latest(self, token_id: int, at: str | None = None) -> dict[str, Any] | None:
        where = " AND decision_at<=?" if at else ""
        params: tuple[Any, ...] = (token_id, at) if at else (token_id,)
        row = self.store.conn.execute(
            "SELECT * FROM runner_decisions_v15 WHERE token_id=?"
            + where
            + " ORDER BY decision_at DESC,created_at DESC LIMIT 1",
            params,
        ).fetchone()
        if not row:
            return None
        value = dict(row)
        for column in (
            "runner_probabilities_json",
            "entry_state_json",
            "supporting_evidence_json",
            "contradicting_evidence_json",
            "critical_unknowns_json",
            "provider_health_json",
            "evidence_freshness_json",
            "latency_json",
            "model_versions_json",
            "controls_json",
            "heuristic_scores_json",
        ):
            value[column.removesuffix("_json")] = json.loads(value.pop(column))
        return value

    def mark_enqueued(self, decision_id: str, enqueue_at: str) -> None:
        """Attach a measured decision-to-outbox duration to a routed decision."""
        self._update_delivery_latency(decision_id, enqueue_at=enqueue_at)

    def mark_discord_delivered(self, decision_id: str, delivered_at: str) -> None:
        """Attach measured outbox-to-Discord and source-to-Discord durations."""
        row = self.store.conn.execute(
            "SELECT latency_json FROM runner_decisions_v15 WHERE decision_id=?",
            (decision_id,),
        ).fetchone()
        if not row:
            return
        latency = json.loads(row[0])
        enqueue_at = (latency.get("timestamps") or {}).get("enqueue")
        if not enqueue_at:
            latency["discord_delivery"] = {"state": "MISSING_ENQUEUE_TIMESTAMP"}
            self._write_latency(decision_id, latency)
            return
        self._update_delivery_latency(
            decision_id,
            enqueue_at=str(enqueue_at),
            discord_at=delivered_at,
        )

    def mark_delivery_suppressed(self, decision_id: str, reason: str) -> None:
        row = self.store.conn.execute(
            "SELECT latency_json FROM runner_decisions_v15 WHERE decision_id=?",
            (decision_id,),
        ).fetchone()
        if not row:
            return
        latency = json.loads(row[0])
        latency["discord_delivery"] = {"state": "SUPPRESSED", "reason": reason}
        self._write_latency(decision_id, latency)

    def _update_delivery_latency(
        self,
        decision_id: str,
        *,
        enqueue_at: str,
        discord_at: str | None = None,
    ) -> None:
        row = self.store.conn.execute(
            "SELECT decision_at,latency_json FROM runner_decisions_v15 WHERE decision_id=?",
            (decision_id,),
        ).fetchone()
        if not row:
            return
        latency = json.loads(row["latency_json"])
        timestamps = dict(latency.get("timestamps") or {})
        timestamps["decision"] = str(row["decision_at"])
        timestamps["enqueue"] = enqueue_at
        decision = _timestamp(str(row["decision_at"]))
        enqueue = _timestamp(enqueue_at)
        latency["decision_to_enqueue_ms"] = round(
            max(0.0, (enqueue - decision).total_seconds() * 1000), 3
        )
        latency.pop("decision_to_enqueue", None)
        if discord_at:
            discord = _timestamp(discord_at)
            timestamps["discord"] = discord_at
            latency["enqueue_to_discord_ms"] = round(
                max(0.0, (discord - enqueue).total_seconds() * 1000), 3
            )
            source_at = timestamps.get("source")
            if source_at:
                latency["source_to_discord_ms"] = round(
                    max(0.0, (discord - _timestamp(str(source_at))).total_seconds() * 1000),
                    3,
                )
            else:
                latency["source_to_discord"] = {"state": "SOURCE_TIMESTAMP_UNAVAILABLE"}
            latency["discord_delivery"] = {"state": "DELIVERED"}
        else:
            latency["discord_delivery"] = {"state": "ENQUEUED"}
        latency["timestamps"] = timestamps
        self._write_latency(decision_id, latency)

    def _write_latency(self, decision_id: str, latency: Mapping[str, Any]) -> None:
        with self.store._lock, self.store.conn:
            self.store.conn.execute(
                "UPDATE runner_decisions_v15 SET latency_json=? WHERE decision_id=?",
                (_json(latency), decision_id),
            )

    def _persist(
        self,
        decision: RunnerDecision,
        candidate_id: int | None,
        trigger_event_id: str | None,
    ) -> None:
        with self.store._lock, self.store.conn:
            self.store.conn.execute(
                "INSERT INTO runner_decisions_v15 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                "?,?,?,?,?,?,?,?,?) ON CONFLICT(token_id,decision_at,champion) DO UPDATE SET "
                "available_at=excluded.available_at,runner_probabilities_json="
                "excluded.runner_probabilities_json,failure_probability="
                "excluded.failure_probability,actionability_probability="
                "excluded.actionability_probability,confidence=excluded.confidence,"
                "uncertainty=excluded.uncertainty,provider_health_json="
                "excluded.provider_health_json,evidence_freshness_json="
                "excluded.evidence_freshness_json,latency_json=excluded.latency_json,"
                "tier=excluded.tier,route_state=excluded.route_state,"
                "decision_reason=excluded.decision_reason",
                (
                    decision.decision_id,
                    decision.token_id,
                    candidate_id,
                    trigger_event_id,
                    decision.decision_at,
                    decision.available_at,
                    decision.stage,
                    decision.thesis_type,
                    decision.champion,
                    _json(decision.runner_probabilities),
                    decision.failure_probability,
                    decision.actionability_probability,
                    decision.confidence,
                    decision.uncertainty,
                    _json(decision.entry_state),
                    _json(decision.supporting_evidence),
                    _json(decision.contradicting_evidence),
                    _json(decision.critical_unknowns),
                    _json(decision.provider_health),
                    _json(decision.evidence_freshness),
                    _json(decision.latency),
                    _json(decision.model_versions),
                    _json(decision.controls),
                    _json(decision.heuristic_scores),
                    decision.tier,
                    str(decision.route_state),
                    decision.decision_reason,
                    decision.evaluation_universe_hash,
                    iso(),
                ),
            )
            self.store.conn.execute(
                "INSERT OR IGNORE INTO decision_outcomes_v15(decision_id,token_id,decision_at,"
                "decision_price,decision_market_cap,copyability_at_decision,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    decision.decision_id,
                    decision.token_id,
                    decision.decision_at,
                    decision.entry_state.get("decision_price"),
                    decision.entry_state.get("decision_market_cap"),
                    int(bool(decision.entry_state.get("tradeability", {}).get("tradeable")))
                    if isinstance(decision.entry_state.get("tradeability"), Mapping)
                    and decision.entry_state.get("tradeability", {}).get("tradeable") is not None
                    else None,
                    iso(),
                ),
            )

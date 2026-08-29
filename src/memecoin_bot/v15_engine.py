from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class Stage(StrEnum):
    NEW = "NEW"
    BONDING = "BONDING"
    MIGRATED = "MIGRATED"
    REVIVAL = "REVIVAL"


class EvidenceState(StrEnum):
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"
    STALE = "STALE"
    DATA_CONFLICT = "DATA_CONFLICT"


class SignalTier(StrEnum):
    PREMIUM = "PREMIUM"
    STRONG = "STRONG"
    HIGH_RISK_MOMENTUM = "HIGH_RISK_MOMENTUM"
    CATALYST_REVIVAL = "CATALYST_REVIVAL"
    SILENT_WATCH = "SILENT_WATCH"
    REJECT = "REJECT"


class EntryStatus(StrEnum):
    OPEN = "OPEN"
    EXTENDED = "EXTENDED"
    CHASING = "CHASING"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ProvenanceValue:
    value: Any
    provider: str
    retrieved_at: str
    confidence: float = 1.0
    state: EvidenceState = EvidenceState.KNOWN
    observed_at: str | None = None

    def age_seconds(self, now: str | None = None) -> float:
        end = datetime.fromisoformat(now) if now else datetime.now(UTC)
        return max(0.0, (end - datetime.fromisoformat(self.retrieved_at)).total_seconds())


@dataclass(slots=True)
class V15Decision:
    stage: Stage
    runner_score: float
    runner_grade: str
    failure_score: float
    failure_grade: str
    survival_grade: str
    setup_conviction: float
    evidence_coverage: float
    entry_status: EntryStatus
    signal_tier: SignalTier
    critical_unknowns: list[str] = field(default_factory=list)
    failure_reasons: list[str] = field(default_factory=list)
    why_now: list[str] = field(default_factory=list)
    provider_conflicts: list[str] = field(default_factory=list)
    feature_vector: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


STAGE_FEATURES = {
    Stage.NEW: (
        "launch_verified",
        "early_demand",
        "buyer_independence",
        "creator_quality",
        "early_liquidity",
        "survival_quality",
        "payoff_quality",
    ),
    Stage.BONDING: (
        "curve_progress",
        "momentum_acceleration",
        "buyer_retention",
        "buyer_replacement",
        "concentration_trend",
        "survival_quality",
        "payoff_quality",
    ),
    Stage.MIGRATED: (
        "amm_liquidity",
        "tradeability",
        "migration_continuity",
        "buyer_quality",
        "buyer_replacement",
        "actor_independence",
        "post_migration_momentum",
        "survival_quality",
        "payoff_quality",
    ),
    Stage.REVIVAL: (
        "abnormal_volume",
        "new_wallet_cohort",
        "fresh_catalyst",
        "renewed_liquidity",
        "narrative_relevance",
        "survival_quality",
        "payoff_quality",
    ),
}


def operator_model_status(settings: Any) -> dict[str, Any]:
    """One operator-facing declaration of active and research-only model state."""
    fingerprint = getattr(settings, "config_fingerprint", None)
    return {
        "active_model": "runner-decision-v1",
        "champion": "CONTROL_V15",
        "control_systems": ["SCORING_LEGACY", "ALPHA_V14", "V15_DETERMINISTIC"],
        "research_systems": ["V3_SHADOW", "RUNNER_THESIS_HEURISTIC"],
        "candidate_state": "RESEARCH_ONLY_NOT_ACTIVE",
        "scoring_version": settings.scoring_version,
        "config_fingerprint": fingerprint() if callable(fingerprint) else "UNKNOWN",
        "signal_truth": "runner_decisions_v15.route_state",
    }


def grade(score: float) -> str:
    if score >= 80:
        return "HIGH"
    if score >= 60:
        return "MEDIUM"
    return "LOW"


def entry_status(
    call_market_cap: float | None,
    current_market_cap: float | None,
    age_minutes: float | None,
    vertical_acceleration: float | None = None,
) -> EntryStatus:
    if call_market_cap is None or current_market_cap is None or age_minutes is None:
        return EntryStatus.UNKNOWN
    if call_market_cap <= 0 or current_market_cap <= 0:
        return EntryStatus.CLOSED
    multiple = current_market_cap / call_market_cap
    if multiple >= 3 or (vertical_acceleration is not None and vertical_acceleration >= 2.5):
        return EntryStatus.CHASING
    if multiple >= 1.7 or age_minutes >= 90:
        return EntryStatus.EXTENDED
    return EntryStatus.OPEN


def provider_truth(
    values: list[ProvenanceValue], stale_after_seconds: float, now: str | None = None
) -> dict[str, Any]:
    usable = [item for item in values if item.value is not None]
    if not usable:
        return {"state": EvidenceState.UNKNOWN, "value": None, "providers": []}
    normalized = {str(item.value).strip().lower() for item in usable}
    if len(normalized) > 1 or any(item.state == EvidenceState.DATA_CONFLICT for item in usable):
        return {
            "state": EvidenceState.DATA_CONFLICT,
            "value": None,
            "providers": [item.provider for item in usable],
        }
    stale = all(item.age_seconds(now) > stale_after_seconds for item in usable)
    return {
        "state": EvidenceState.STALE if stale else EvidenceState.KNOWN,
        "value": usable[0].value,
        "providers": [item.provider for item in usable],
        "confidence": min(item.confidence for item in usable),
    }


def tradeability(
    liquidity_usd: float | None, notionals: tuple[int, ...] = (50, 100, 250, 500, 1000)
) -> dict[str, Any]:
    if liquidity_usd is None or liquidity_usd <= 0:
        return {"grade": "UNKNOWN", "estimates": {}, "method": "constant_product_estimate"}
    estimates = {}
    for notional in notionals:
        # Conservative symmetric reserve estimate. It is explicitly an estimate,
        # not an execution promise or quote.
        reserve = liquidity_usd / 2
        impact = notional / (reserve + notional)
        estimates[str(notional)] = {
            "buy_impact_percent": round(impact * 100, 3),
            "sell_impact_percent": round(impact * 100, 3),
            "source": "ESTIMATE",
        }
    worst = estimates[str(max(notionals))]["sell_impact_percent"]
    return {
        "grade": "GOOD" if worst <= 5 else "LIMITED" if worst <= 15 else "POOR",
        "estimates": estimates,
        "method": "constant_product_estimate",
    }


def economic_concentration(holders: list[dict[str, Any]]) -> dict[str, Any]:
    economic = [row for row in holders if not row.get("excluded_non_economic")]
    total = sum(float(row.get("percent") or 0) for row in economic)
    clusters: dict[str, float] = {}
    independent = 0
    deployer_related = 0.0
    for row in economic:
        percent = float(row.get("percent") or 0)
        cluster = row.get("cluster_id")
        if cluster:
            clusters[str(cluster)] = clusters.get(str(cluster), 0) + percent
        else:
            independent += 1
        if row.get("deployer_related"):
            deployer_related += percent
    connected = max(clusters.values(), default=0.0)
    return {
        "raw_top10_percent": round(total, 4),
        "effective_actor_concentration": round(max(connected, deployer_related), 4),
        "independent_actor_count": independent + len(clusters),
        "connected_cluster_percent": round(connected, 4),
        "deployer_related_percent": round(deployer_related, 4),
        "bundle_state": "CONNECTED" if clusters else "NO_LINKS_OBSERVED",
    }


def buyer_trajectory(cohorts: list[dict[str, Any]]) -> dict[str, Any]:
    if not cohorts:
        return {"state": "UNKNOWN", "score": None}
    latest = cohorts[-1]
    early = max(1, int(latest.get("cohort_size") or 0))
    retained = int(latest.get("retained") or 0)
    replacements = int(latest.get("replacement_buyers") or 0)
    independent_replacements = int(latest.get("independent_replacements") or 0)
    exited = int(latest.get("fully_exited") or 0)
    healthy = retained / early >= 0.35 and independent_replacements >= exited * 0.6
    collapsed = retained / early < 0.2 and replacements < exited * 0.35
    return {
        "state": "HEALTHY_REPLACEMENT" if healthy else "BUYER_COLLAPSE" if collapsed else "MIXED",
        "score": 85 if healthy else 20 if collapsed else 55,
        "net_buyer_growth": replacements - exited,
    }


def independent_alpha_count(wallets: list[dict[str, Any]]) -> int:
    families = {
        str(row.get("family_id") or row.get("wallet"))
        for row in wallets
        if row.get("empirical_alpha") is True and not row.get("bot_or_mayhem")
    }
    return len(families)


def evaluate_v15(stage: Stage | str, features: dict[str, Any]) -> V15Decision:
    stage = Stage(stage)
    required = STAGE_FEATURES[stage]
    known = [name for name in required if features.get(name) is not None]
    coverage = len(known) / len(required) * 100
    conflicts = list(features.get("provider_conflicts") or [])
    stale = list(features.get("stale_evidence") or [])

    values = [float(features[name]) for name in known if isinstance(features[name], (int, float))]
    runner = sum(values) / len(values) if values else 0.0
    failure_reasons: list[str] = []
    failure = 0.0
    risks = {
        "terminal_safety_failure": 100,
        "sell_restriction_unknown": 25,
        "concentration_unknown": 20,
        "buyer_collapse": 35,
        "toxic_creator": 35,
        "poor_tradeability": 30,
        "connected_concentration": 30,
        "liquidity_deterioration": 35,
    }
    for name, weight in risks.items():
        if features.get(name) is True:
            failure += weight
            failure_reasons.append(name.upper())
    failure = min(100.0, failure)
    setup = runner  # attractiveness is deliberately not reduced merely for youth/unknowns
    entry = entry_status(
        features.get("call_market_cap"),
        features.get("current_market_cap"),
        features.get("age_minutes"),
        features.get("vertical_acceleration"),
    )

    critical = list(features.get("critical_unknowns") or [])
    if stage == Stage.MIGRATED and features.get("tradeability") is None:
        critical.append("TRADEABILITY_UNKNOWN")
    if stage == Stage.REVIVAL and features.get("fresh_catalyst") is None:
        critical.append("FRESH_CATALYST_UNKNOWN")
    if features.get("sell_restriction_unknown"):
        critical.append("SELL_RESTRICTIONS_UNKNOWN")
    if features.get("concentration_unknown"):
        critical.append("CONCENTRATION_UNKNOWN")

    high_runner = runner >= 75
    high_failure = failure >= 50
    if features.get("terminal_safety_failure"):
        tier = SignalTier.REJECT
    elif high_runner and high_failure:
        tier = SignalTier.HIGH_RISK_MOMENTUM
    elif stage == Stage.REVIVAL and high_runner and features.get("fresh_catalyst") is not None:
        tier = SignalTier.CATALYST_REVIVAL
    elif high_runner and failure < 30:
        tier = SignalTier.PREMIUM
    elif runner >= 60 and failure < 40:
        tier = SignalTier.STRONG
    else:
        tier = SignalTier.SILENT_WATCH

    if tier == SignalTier.PREMIUM and (
        coverage < 75 or critical or conflicts or stale or entry != EntryStatus.OPEN
    ):
        tier = (
            SignalTier.STRONG
            if runner >= 60 and entry != EntryStatus.CHASING
            else SignalTier.SILENT_WATCH
        )
    if entry in {EntryStatus.CHASING, EntryStatus.CLOSED, EntryStatus.UNKNOWN} and tier in {
        SignalTier.PREMIUM,
        SignalTier.STRONG,
    }:
        tier = SignalTier.SILENT_WATCH

    return V15Decision(
        stage=stage,
        runner_score=round(runner, 2),
        runner_grade=grade(runner),
        failure_score=round(failure, 2),
        failure_grade=grade(failure),
        survival_grade="LOW" if failure >= 60 else "MEDIUM" if failure >= 30 else "HIGH",
        setup_conviction=round(setup, 2),
        evidence_coverage=round(coverage, 2),
        entry_status=entry,
        signal_tier=tier,
        critical_unknowns=sorted(set(critical)),
        failure_reasons=failure_reasons,
        why_now=list(features.get("why_now") or [])[:2],
        provider_conflicts=conflicts,
        feature_vector=dict(features),
    )

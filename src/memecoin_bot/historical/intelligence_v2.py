from __future__ import annotations

import itertools
import math
import statistics
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

INTELLIGENCE_V2_VERSION = "intelligence-v2-research-1"
IDENTIFIER_REGISTRY_VERSION = "identifier-registry-v2.1"
OUTCOME_VERSION = "pit-dimensionless-pregrad-postgrad-v2"
CONTROL_FREEZE_SHA = "a0cd254fc599a31958052465c5cba06c98acbecc"


class IdentifierFamily(StrEnum):
    MARKET_CAP_STAGE = "MARKET_CAP_STAGE"
    BUYER_FLOW = "BUYER_FLOW"
    WALLET_QUALITY = "WALLET_QUALITY"
    CREATOR = "CREATOR"
    FUNDER_CLUSTER = "FUNDER_CLUSTER"
    LIQUIDITY = "LIQUIDITY"
    MOMENTUM = "MOMENTUM"
    CONCENTRATION = "CONCENTRATION"
    TRADEABILITY = "TRADEABILITY"
    MIGRATION = "MIGRATION"
    NARRATIVE_SOCIAL = "NARRATIVE_SOCIAL"
    REGIME = "REGIME"
    ENTRY_QUALITY = "ENTRY_QUALITY"
    SURVIVAL = "SURVIVAL"
    FAILURE_RUG = "FAILURE_RUG"
    REVIVAL_CATALYST = "REVIVAL_CATALYST"
    MANIPULATION = "MANIPULATION"


class IdentifierState(StrEnum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    UNKNOWN = "UNKNOWN"


class Objective(StrEnum):
    QUICK_2X = "QUICK_2X"
    MID_5X = "MID_5X"
    RIGHT_TAIL = "RIGHT_TAIL"
    SURVIVAL = "SURVIVAL"
    FAILURE = "FAILURE"
    ENTRY = "ENTRY"
    REVIVAL = "REVIVAL"


@dataclass(frozen=True, slots=True)
class IdentifierDefinition:
    identifier_id: str
    name: str
    family: IdentifierFamily
    stage: tuple[str, ...]
    required_inputs: tuple[str, ...]
    pit_availability: str
    expected_coverage: str
    direction: str
    strength: str
    confidence: str
    applicable_objectives: tuple[Objective, ...]
    decay_profile: str
    gameability: str
    provider_provenance: tuple[str, ...]
    known_failure_modes: tuple[str, ...]
    quantitative_definition: str
    status: str = "RESEARCH_ONLY"
    version: str = IDENTIFIER_REGISTRY_VERSION


@dataclass(frozen=True, slots=True)
class IdentifierSignal:
    identifier_id: str
    state: IdentifierState
    value: float | None
    confidence: float
    coverage: float
    observed_at_seconds: int | None
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ObjectiveResult:
    objective: Objective
    score: float
    confidence: float
    coverage: float
    positive_identifiers: tuple[str, ...]
    negative_identifiers: tuple[str, ...]
    calibration_state: str = "UNCALIBRATED_RESEARCH"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class V2ResearchDecision:
    version: str
    control_freeze_sha: str
    objectives: dict[str, ObjectiveResult]
    entry: dict[str, Any]
    survival: dict[str, Any]
    failure: dict[str, Any]
    migration: dict[str, Any]
    market_cap_regime: str
    identifiers: list[IdentifierSignal]
    signal_policy: str
    why_now: list[str]
    risks: list[str]
    evidence_confidence: float
    evidence_coverage: float
    latency_ms: float
    public_alert_routed: bool = False
    production_eligible: bool = False

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["objectives"] = {name: value.to_dict() for name, value in self.objectives.items()}
        result["identifiers"] = [value.to_dict() for value in self.identifiers]
        return result


class IdentifierRegistry:
    def __init__(self, definitions: Iterable[IdentifierDefinition]):
        self._definitions: dict[str, IdentifierDefinition] = {}
        semantics: set[tuple[str, str]] = set()
        for definition in definitions:
            if definition.identifier_id in self._definitions:
                raise ValueError(f"duplicate identifier id: {definition.identifier_id}")
            semantic_key = (definition.family, definition.quantitative_definition.casefold())
            if semantic_key in semantics:
                raise ValueError(f"duplicate identifier semantics: {definition.identifier_id}")
            self._definitions[definition.identifier_id] = definition
            semantics.add(semantic_key)

    def get(self, identifier_id: str) -> IdentifierDefinition:
        return self._definitions[identifier_id]

    def all(self) -> tuple[IdentifierDefinition, ...]:
        return tuple(self._definitions.values())

    def to_dict(self) -> list[dict[str, Any]]:
        return [asdict(definition) for definition in self.all()]


def _definition(
    identifier_id: str,
    family: IdentifierFamily,
    required: tuple[str, ...],
    direction: str,
    objectives: tuple[Objective, ...],
    formula: str,
    *,
    stage: tuple[str, ...] = ("NEW", "BONDING", "MIGRATED", "REVIVAL"),
    coverage: str = "DATA_DEPENDENT",
    decay: str = "RECOMPUTE_EACH_OBSERVATION",
    gameability: str = "MEDIUM",
    providers: tuple[str, ...] = ("PIT_NORMALIZED_PROVIDER",),
    failures: tuple[str, ...] = ("missing or stale provider evidence",),
) -> IdentifierDefinition:
    return IdentifierDefinition(
        identifier_id=identifier_id,
        name=identifier_id.replace("_", " ").title(),
        family=family,
        stage=stage,
        required_inputs=required,
        pit_availability="PIT_ONLY_NO_FORWARD_FILL",
        expected_coverage=coverage,
        direction=direction,
        strength="UNVALIDATED",
        confidence="MEASURED_FROM_INPUT_COVERAGE",
        applicable_objectives=objectives,
        decay_profile=decay,
        gameability=gameability,
        provider_provenance=providers,
        known_failure_modes=failures,
        quantitative_definition=formula,
    )


IDENTIFIER_REGISTRY = IdentifierRegistry(
    (
        _definition(
            "EXTREME_EARLY_MC",
            IdentifierFamily.MARKET_CAP_STAGE,
            ("current_market_cap", "market_cap_unit"),
            "POSITIVE",
            (Objective.RIGHT_TAIL, Objective.REVIVAL),
            "SOL market cap < 5",
        ),
        _definition(
            "EARLY_MC_POSITION",
            IdentifierFamily.MARKET_CAP_STAGE,
            ("current_market_cap", "market_cap_unit", "age_seconds"),
            "POSITIVE",
            (Objective.QUICK_2X, Objective.MID_5X),
            "SOL market cap in [5,20) before T+10m",
        ),
        _definition(
            "DEAD_ZONE_MC",
            IdentifierFamily.MARKET_CAP_STAGE,
            ("current_market_cap", "market_cap_unit"),
            "NEGATIVE",
            (Objective.QUICK_2X, Objective.MID_5X),
            "SOL market cap in [20,30)",
        ),
        _definition(
            "OVEREXTENDED_MC",
            IdentifierFamily.MARKET_CAP_STAGE,
            ("current_market_cap", "market_cap_unit", "age_seconds"),
            "NEGATIVE",
            (Objective.ENTRY, Objective.FAILURE),
            "SOL market cap >=250 before T+10m",
            gameability="LOW",
        ),
        _definition(
            "MC_VELOCITY_STRONG",
            IdentifierFamily.MARKET_CAP_STAGE,
            ("market_cap_velocity",),
            "POSITIVE",
            (Objective.QUICK_2X, Objective.MID_5X, Objective.RIGHT_TAIL),
            "market_cap_velocity > 0.003 per second",
        ),
        _definition(
            "MC_ACCELERATION_STRONG",
            IdentifierFamily.MARKET_CAP_STAGE,
            ("market_cap_acceleration",),
            "POSITIVE",
            (Objective.RIGHT_TAIL,),
            "market_cap_acceleration > 0.00002 per second squared",
        ),
        _definition(
            "BUYER_LEVEL_STRONG",
            IdentifierFamily.BUYER_FLOW,
            ("buyer_count",),
            "POSITIVE",
            (Objective.QUICK_2X, Objective.MID_5X),
            "buyer_count >= 20",
            gameability="HIGH",
        ),
        _definition(
            "BUYER_GROWTH_STRONG",
            IdentifierFamily.BUYER_FLOW,
            ("buyer_growth",),
            "POSITIVE",
            (Objective.MID_5X, Objective.RIGHT_TAIL),
            "buyer_growth >= 5",
            gameability="HIGH",
        ),
        _definition(
            "BUYER_ACCELERATION_STRONG",
            IdentifierFamily.BUYER_FLOW,
            ("buyer_acceleration",),
            "POSITIVE",
            (Objective.RIGHT_TAIL,),
            "buyer_acceleration >= 3",
            gameability="HIGH",
        ),
        _definition(
            "BUYER_PERSISTENCE",
            IdentifierFamily.BUYER_FLOW,
            ("buyer_count_persistence",),
            "POSITIVE",
            (Objective.MID_5X, Objective.RIGHT_TAIL),
            "positive buyer changes in >=67% of observed intervals",
        ),
        _definition(
            "BUYER_EXHAUSTION",
            IdentifierFamily.BUYER_FLOW,
            ("buyer_growth", "buyer_acceleration"),
            "NEGATIVE",
            (Objective.QUICK_2X, Objective.MID_5X, Objective.RIGHT_TAIL),
            "buyer_growth <=0 and buyer_acceleration <0",
            gameability="MEDIUM",
        ),
        _definition(
            "NET_BUY_FLOW_POSITIVE",
            IdentifierFamily.BUYER_FLOW,
            ("buy_count", "sell_count"),
            "POSITIVE",
            (Objective.QUICK_2X, Objective.MID_5X),
            "buy_count - sell_count > 0",
        ),
        _definition(
            "SELLER_DOMINANCE",
            IdentifierFamily.BUYER_FLOW,
            ("buy_count", "sell_count"),
            "NEGATIVE",
            (Objective.FAILURE, Objective.SURVIVAL),
            "sell_count > 1.25 * buy_count",
        ),
        _definition(
            "INDEPENDENT_BUYER_EXPANSION",
            IdentifierFamily.WALLET_QUALITY,
            ("independent_buyer_ratio", "buyer_growth"),
            "POSITIVE",
            (Objective.MID_5X, Objective.RIGHT_TAIL),
            "independent_buyer_ratio >=0.7 and buyer_growth >=3",
            coverage="SPARSE",
            gameability="LOW",
            providers=("WALLET_CLUSTER_ARCHIVE",),
        ),
        _definition(
            "REPEAT_RUNNER_WALLET",
            IdentifierFamily.WALLET_QUALITY,
            ("high_quality_buyer_count",),
            "POSITIVE",
            (Objective.RIGHT_TAIL,),
            "PIT high-quality buyer count >=2",
            coverage="UNAVAILABLE_IN_CORPUS",
            gameability="LOW",
            providers=("PIT_WALLET_HISTORY",),
        ),
        _definition(
            "FAST_FLIPPER_DOMINANCE",
            IdentifierFamily.WALLET_QUALITY,
            ("fast_flipper_share",),
            "NEGATIVE",
            (Objective.FAILURE, Objective.SURVIVAL),
            "fast_flipper_share >=0.5",
            coverage="UNAVAILABLE_IN_CORPUS",
            providers=("PIT_WALLET_HISTORY",),
        ),
        _definition(
            "CREATOR_SURVIVAL_HISTORY",
            IdentifierFamily.CREATOR,
            ("creator_past_tokens", "creator_past_rugs"),
            "POSITIVE",
            (Objective.SURVIVAL,),
            "creator has >=3 launches and rug rate <=10%",
            gameability="LOW",
        ),
        _definition(
            "TOXIC_CREATOR_HISTORY",
            IdentifierFamily.CREATOR,
            ("creator_past_tokens", "creator_past_rugs"),
            "NEGATIVE",
            (Objective.FAILURE, Objective.SURVIVAL),
            "creator has >=3 launches and rug rate >=50%",
            gameability="LOW",
        ),
        _definition(
            "CREATOR_LINKED_DEMAND",
            IdentifierFamily.CREATOR,
            ("creator_linked_buyer_share",),
            "NEGATIVE",
            (Objective.FAILURE, Objective.RIGHT_TAIL),
            "creator-linked buyer share >=30%",
            coverage="UNAVAILABLE_IN_CORPUS",
            providers=("FUND_FLOW_GRAPH",),
        ),
        _definition(
            "SHARED_FUNDER_CLUSTER",
            IdentifierFamily.FUNDER_CLUSTER,
            ("shared_funder_confidence",),
            "NEGATIVE",
            (Objective.FAILURE,),
            "proven/likely shared upstream funder confidence >=0.7",
            coverage="UNAVAILABLE_IN_CORPUS",
            gameability="LOW",
            providers=("FUND_FLOW_GRAPH",),
        ),
        _definition(
            "LIQUIDITY_EXPANSION",
            IdentifierFamily.LIQUIDITY,
            ("liquidity_velocity",),
            "POSITIVE",
            (Objective.MID_5X, Objective.RIGHT_TAIL, Objective.SURVIVAL),
            "liquidity velocity >0",
            stage=("MIGRATED", "REVIVAL"),
            gameability="MEDIUM",
        ),
        _definition(
            "LIQUIDITY_ACCELERATION",
            IdentifierFamily.LIQUIDITY,
            ("liquidity_acceleration",),
            "POSITIVE",
            (Objective.RIGHT_TAIL,),
            "liquidity acceleration >0",
            stage=("MIGRATED", "REVIVAL"),
        ),
        _definition(
            "LIQUIDITY_WITHDRAWAL",
            IdentifierFamily.LIQUIDITY,
            ("liquidity_drawdown",),
            "NEGATIVE",
            (Objective.FAILURE, Objective.SURVIVAL, Objective.ENTRY),
            "liquidity drawdown <=-30%",
            stage=("MIGRATED", "REVIVAL"),
            gameability="LOW",
        ),
        _definition(
            "LOW_LIQUIDITY_TO_MC",
            IdentifierFamily.LIQUIDITY,
            ("liquidity_to_market_cap",),
            "NEGATIVE",
            (Objective.FAILURE, Objective.ENTRY),
            "liquidity/market-cap ratio <3%",
            stage=("MIGRATED", "REVIVAL"),
        ),
        _definition(
            "PERSISTENT_MOMENTUM",
            IdentifierFamily.MOMENTUM,
            ("market_cap_persistence", "buyer_count_persistence"),
            "POSITIVE",
            (Objective.MID_5X, Objective.RIGHT_TAIL),
            "market-cap and buyer persistence both >=67%",
        ),
        _definition(
            "PRICE_UP_BUYERS_DOWN",
            IdentifierFamily.MOMENTUM,
            ("price_return_pct", "buyer_growth"),
            "NEGATIVE",
            (Objective.FAILURE, Objective.RIGHT_TAIL),
            "price return >20% while buyer growth <=0",
        ),
        _definition(
            "PRICE_UP_LIQUIDITY_DOWN",
            IdentifierFamily.MOMENTUM,
            ("price_return_pct", "liquidity_velocity"),
            "NEGATIVE",
            (Objective.FAILURE, Objective.SURVIVAL),
            "price return >20% while liquidity velocity <0",
            stage=("MIGRATED", "REVIVAL"),
        ),
        _definition(
            "CONCENTRATION_RELEASE",
            IdentifierFamily.CONCENTRATION,
            ("concentration_change",),
            "POSITIVE",
            (Objective.MID_5X, Objective.SURVIVAL),
            "top-holder concentration change <=-5pp",
            coverage="SPARSE",
        ),
        _definition(
            "CONCENTRATION_INCREASE",
            IdentifierFamily.CONCENTRATION,
            ("concentration_change",),
            "NEGATIVE",
            (Objective.FAILURE,),
            "top-holder concentration change >=5pp",
            coverage="SPARSE",
        ),
        _definition(
            "TRADEABILITY_GOOD",
            IdentifierFamily.TRADEABILITY,
            ("tradeability_score",),
            "POSITIVE",
            (Objective.QUICK_2X, Objective.ENTRY, Objective.SURVIVAL),
            "tradeability score >=70",
            stage=("MIGRATED", "REVIVAL"),
        ),
        _definition(
            "TRADEABILITY_POOR",
            IdentifierFamily.TRADEABILITY,
            ("tradeability_score",),
            "NEGATIVE",
            (Objective.FAILURE, Objective.ENTRY),
            "tradeability score <40",
            stage=("MIGRATED", "REVIVAL"),
        ),
        _definition(
            "HEALTHY_MIGRATION",
            IdentifierFamily.MIGRATION,
            ("migration_continuity_state",),
            "POSITIVE",
            (Objective.MID_5X, Objective.RIGHT_TAIL, Objective.SURVIVAL),
            "migration continuity state is HEALTHY",
            stage=("MIGRATED",),
        ),
        _definition(
            "UNHEALTHY_MIGRATION",
            IdentifierFamily.MIGRATION,
            ("migration_continuity_state",),
            "NEGATIVE",
            (Objective.FAILURE, Objective.SURVIVAL),
            "migration continuity state is WEAK/DISRUPTED/SUSPICIOUS",
            stage=("MIGRATED",),
        ),
        _definition(
            "PREPARED_LAUNCH",
            IdentifierFamily.NARRATIVE_SOCIAL,
            ("prepared_launch_score",),
            "POSITIVE",
            (Objective.RIGHT_TAIL,),
            "PIT prepared-launch score >=70",
            coverage="UNAVAILABLE_IN_CORPUS",
            gameability="HIGH",
            providers=("PIT_SOCIAL_ARCHIVE",),
        ),
        _definition(
            "SOCIAL_BOT_ACTIVITY",
            IdentifierFamily.NARRATIVE_SOCIAL,
            ("social_bot_share",),
            "NEGATIVE",
            (Objective.FAILURE,),
            "social bot share >=50%",
            coverage="UNAVAILABLE_IN_CORPUS",
            gameability="HIGH",
            providers=("PIT_SOCIAL_ARCHIVE",),
        ),
        _definition(
            "HIGH_LAUNCH_INTENSITY",
            IdentifierFamily.REGIME,
            ("launch_intensity_percentile",),
            "CONTEXT",
            (Objective.QUICK_2X, Objective.MID_5X, Objective.RIGHT_TAIL),
            "launch intensity percentile >=75",
            gameability="LOW",
        ),
        _definition(
            "RISK_OFF_REGIME",
            IdentifierFamily.REGIME,
            ("risk_regime",),
            "NEGATIVE",
            (Objective.QUICK_2X, Objective.MID_5X, Objective.RIGHT_TAIL),
            "PIT market regime is RISK_OFF",
            coverage="SPARSE",
            gameability="LOW",
        ),
        _definition(
            "ENTRY_VALID",
            IdentifierFamily.ENTRY_QUALITY,
            ("entry_quality_state",),
            "POSITIVE",
            (Objective.QUICK_2X, Objective.MID_5X, Objective.RIGHT_TAIL, Objective.ENTRY),
            "entry state is EARLY_VALID/CONFIRMED_EARLY/ACCELERATING_BUT_ENTRY_VALID",
        ),
        _definition(
            "OVEREXTENDED_ENTRY",
            IdentifierFamily.ENTRY_QUALITY,
            ("entry_quality_state",),
            "NEGATIVE",
            (Objective.QUICK_2X, Objective.MID_5X, Objective.ENTRY),
            "entry state is OVERHEATED/CHASE/LATE",
        ),
        _definition(
            "SURVIVAL_HIGH_CONFIDENCE",
            IdentifierFamily.SURVIVAL,
            ("survival_score", "survival_confidence"),
            "POSITIVE",
            (Objective.SURVIVAL, Objective.QUICK_2X, Objective.MID_5X),
            "survival score >=70 and confidence >=70",
        ),
        _definition(
            "SURVIVAL_SPARSE",
            IdentifierFamily.SURVIVAL,
            ("survival_confidence",),
            "NEGATIVE",
            (Objective.SURVIVAL,),
            "survival confidence <50",
        ),
        _definition(
            "HARD_FAILURE_PRESENT",
            IdentifierFamily.FAILURE_RUG,
            ("hard_failure_score",),
            "NEGATIVE",
            (Objective.FAILURE, Objective.SURVIVAL),
            "hard failure score >=50",
            gameability="LOW",
        ),
        _definition(
            "SOFT_FAILURE_ELEVATED",
            IdentifierFamily.FAILURE_RUG,
            ("soft_failure_score",),
            "NEGATIVE",
            (Objective.FAILURE, Objective.QUICK_2X, Objective.MID_5X),
            "soft failure score >=40",
        ),
        _definition(
            "REVIVAL_CATALYST_PRESENT",
            IdentifierFamily.REVIVAL_CATALYST,
            ("fresh_catalyst_score",),
            "POSITIVE",
            (Objective.REVIVAL, Objective.RIGHT_TAIL),
            "PIT fresh catalyst score >=70",
            stage=("REVIVAL",),
            coverage="SPARSE",
        ),
        _definition(
            "REVIVAL_WITHOUT_CATALYST",
            IdentifierFamily.REVIVAL_CATALYST,
            ("stage", "fresh_catalyst_score"),
            "NEGATIVE",
            (Objective.REVIVAL, Objective.FAILURE),
            "stage is REVIVAL and catalyst is missing/below40",
            stage=("REVIVAL",),
        ),
        _definition(
            "WASH_VOLUME_RISK",
            IdentifierFamily.MANIPULATION,
            ("trades_per_unique_buyer", "median_trade_size"),
            "NEGATIVE",
            (Objective.FAILURE, Objective.RIGHT_TAIL),
            "trades/buyer >=20 with low median trade size",
            coverage="SPARSE",
            gameability="MEDIUM",
        ),
        _definition(
            "SYBIL_BUYER_RISK",
            IdentifierFamily.MANIPULATION,
            ("sybil_adjusted_buyer_ratio",),
            "NEGATIVE",
            (Objective.FAILURE, Objective.RIGHT_TAIL),
            "Sybil-adjusted/raw buyer ratio <50%",
            coverage="UNAVAILABLE_IN_CORPUS",
            gameability="LOW",
            providers=("WALLET_CLUSTER_ARCHIVE",),
        ),
    )
)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _ratio(numerator: Any, denominator: Any) -> float | None:
    top, bottom = _number(numerator), _number(denominator)
    return None if top is None or bottom is None or bottom == 0 else top / bottom


def _series_features(
    observations: Sequence[Mapping[str, Any]], name: str
) -> dict[str, float | None]:
    points = [
        (int(row["timestamp_seconds"]), value)
        for row in observations
        if (value := _number(row.get(name))) is not None
    ]
    if not points:
        return {
            "level": None,
            "delta": None,
            "velocity": None,
            "acceleration": None,
            "persistence": None,
            "drawdown": None,
        }
    points.sort()
    level = points[-1][1]
    peak = max(value for _, value in points)
    deltas = [
        (right_value - left_value, right_seconds - left_seconds)
        for (left_seconds, left_value), (right_seconds, right_value) in itertools.pairwise(points)
        if right_seconds > left_seconds
    ]
    velocities = [delta / seconds for delta, seconds in deltas]
    acceleration = None
    if len(velocities) >= 2:
        time_delta = max(1, points[-1][0] - points[-2][0])
        acceleration = (velocities[-1] - velocities[-2]) / time_delta
    return {
        "level": level,
        "delta": deltas[-1][0] if deltas else None,
        "velocity": velocities[-1] if velocities else None,
        "acceleration": acceleration,
        "persistence": (sum(delta > 0 for delta, _ in deltas) / len(deltas) if deltas else None),
        "drawdown": level / peak - 1 if peak > 0 else None,
    }


def build_trajectory_features(observations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Preserve raw levels and derive change/acceleration without forward filling."""
    if not observations:
        return {}
    ordered = sorted(observations, key=lambda row: int(row["timestamp_seconds"]))
    latest = dict(ordered[-1])
    result = dict(latest)
    aliases = {
        "buyer_count": "buyer_count",
        "current_market_cap": "market_cap",
        "liquidity_usd": "liquidity",
        "trade_count": "trade_count",
        "buy_volume": "buy_volume",
        "initial_top10_pct_corrected": "concentration",
    }
    for source, target in aliases.items():
        values = _series_features(ordered, source)
        for suffix, value in values.items():
            result[f"{target}_{suffix}"] = value
    result["buyer_growth"] = _number(latest.get("buyer_growth"))
    result["buyer_acceleration"] = _number(latest.get("buyer_acceleration"))
    result["net_buyers"] = (
        None
        if _number(latest.get("buy_count")) is None or _number(latest.get("sell_count")) is None
        else float(latest["buy_count"]) - float(latest["sell_count"])
    )
    result["liquidity_to_market_cap"] = _ratio(
        latest.get("liquidity_usd"), latest.get("current_market_cap")
    )
    result["trades_per_unique_buyer"] = _ratio(latest.get("trade_count"), latest.get("buyer_count"))
    result["age_seconds"] = int(latest.get("timestamp_seconds") or 0)
    return result


def classify_market_cap_regime(features: Mapping[str, Any]) -> str:
    market_cap = _number(features.get("current_market_cap"))
    if market_cap is None or features.get("market_cap_unit") != "SOL":
        return "UNKNOWN"
    age = _number(features.get("age_seconds")) or 0
    if features.get("stage") == "REVIVAL":
        return "REVIVAL"
    if market_cap < 5:
        return "EXTREME_EARLY"
    if market_cap < 20:
        return "EARLY"
    if market_cap < 30:
        return "VALIDATING"
    if market_cap < 100:
        return "EXPANDING"
    if market_cap < 250 or age >= 3600:
        return "MATURE"
    return "OVEREXTENDED"


def migration_continuity_v2(features: Mapping[str, Any]) -> dict[str, Any]:
    required = (
        "pre_migration_liquidity",
        "post_migration_liquidity",
        "pre_migration_price",
        "post_migration_price",
        "migration_gap_seconds",
    )
    known = [name for name in required if _number(features.get(name)) is not None]
    coverage = len(known) / len(required) * 100
    if len(known) < 4:
        return {
            "state": "UNKNOWN",
            "score": None,
            "confidence": coverage,
            "coverage": coverage,
            "known_inputs": known,
            "unknown_inputs": sorted(set(required) - set(known)),
        }
    liquidity_ratio = _ratio(
        features.get("post_migration_liquidity"), features.get("pre_migration_liquidity")
    )
    price_ratio = _ratio(features.get("post_migration_price"), features.get("pre_migration_price"))
    gap = _number(features.get("migration_gap_seconds")) or 0
    sell_pressure = _number(features.get("post_migration_sell_pressure"))
    suspicious = bool(features.get("pool_identity_conflict"))
    if suspicious:
        state, score = "SUSPICIOUS", 10
    elif liquidity_ratio is None or price_ratio is None:
        state, score = "UNKNOWN", None
    elif liquidity_ratio < 0.4 or price_ratio < 0.45 or gap > 600:
        state, score = "DISRUPTED", 20
    elif liquidity_ratio < 0.75 or price_ratio < 0.75 or (sell_pressure or 0) > 0.7:
        state, score = "WEAK", 45
    else:
        state, score = "HEALTHY", 85
    return {
        "state": state,
        "score": score,
        "confidence": coverage,
        "coverage": coverage,
        "known_inputs": known,
        "unknown_inputs": sorted(set(required) - set(known)),
        "liquidity_ratio": liquidity_ratio,
        "price_ratio": price_ratio,
    }


def failure_v2(features: Mapping[str, Any]) -> dict[str, Any]:
    hard_weights = {
        "sell_restriction": 100,
        "transfer_restriction": 90,
        "liquidity_removal": 90,
        "creator_dump": 80,
        "connected_cluster_dump": 80,
        "lp_collapse": 90,
        "mint_authority_risk": 70,
        "freeze_risk": 90,
        "unsellable": 100,
        "fake_liquidity": 80,
    }
    soft_weights = {
        "toxic_creator": 35,
        "buyer_exhaustion": 25,
        "seller_dominance": 25,
        "concentration_increase": 20,
        "poor_tradeability": 25,
        "unhealthy_migration": 35,
    }
    hard = max(
        (weight for name, weight in hard_weights.items() if features.get(name) is True), default=0
    )
    soft = min(
        100, sum(weight for name, weight in soft_weights.items() if features.get(name) is True)
    )
    volatility = min(100, max(0, (_number(features.get("price_volatility")) or 0) * 100))
    hard_reasons = [name.upper() for name in hard_weights if features.get(name) is True]
    soft_reasons = [name.upper() for name in soft_weights if features.get(name) is True]
    return {
        "hard_failure_score": float(hard),
        "soft_failure_score": float(soft),
        "volatility_risk_score": float(volatility),
        "hard_reasons": hard_reasons,
        "soft_reasons": soft_reasons,
        "terminal_block": hard >= 70,
    }


def survival_v2(features: Mapping[str, Any], failure: Mapping[str, Any]) -> dict[str, Any]:
    inputs = {
        "hard_failure": 100 - float(failure["hard_failure_score"]),
        "soft_failure": 100 - float(failure["soft_failure_score"]),
        "liquidity": _number(features.get("liquidity_score")),
        "tradeability": _number(features.get("tradeability_score")),
        "creator": _number(features.get("creator_score")),
        "concentration": _number(features.get("concentration_score")),
        "buyer_persistence": (
            None
            if _number(features.get("buyer_count_persistence")) is None
            else float(features["buyer_count_persistence"]) * 100
        ),
    }
    known = {name: value for name, value in inputs.items() if value is not None}
    unknown = sorted(set(inputs) - set(known))
    coverage = len(known) / len(inputs) * 100
    score = statistics.fmean(known.values()) if known else 0.0
    if failure["terminal_block"]:
        score = min(score, 10)
    # Sparse evidence can support a high estimate, never high certainty or a synthetic 100.
    score = min(95.0, score)
    confidence = min(coverage, 100 - len(unknown) * 3)
    grade = "HIGH" if score >= 70 else "MEDIUM" if score >= 40 else "LOW"
    return {
        "score": round(score, 3),
        "confidence": round(max(0, confidence), 3),
        "coverage": round(coverage, 3),
        "grade": grade,
        "known_inputs": sorted(known),
        "unknown_inputs": unknown,
    }


def entry_quality_v2(features: Mapping[str, Any], migration: Mapping[str, Any]) -> dict[str, Any]:
    market_cap = _number(features.get("current_market_cap"))
    initial = _number(features.get("initial_market_cap_sol"))
    age = _number(features.get("age_seconds"))
    buyer_growth = _number(features.get("buyer_growth"))
    buyer_acceleration = _number(features.get("buyer_acceleration"))
    velocity = _number(features.get("market_cap_velocity"))
    liquidity_drawdown = _number(features.get("liquidity_drawdown"))
    known = [value is not None for value in (market_cap, age, buyer_growth, velocity)]
    coverage = sum(known) / len(known) * 100
    if market_cap is None or age is None:
        state, score = "UNKNOWN", None
    elif features.get("stage") == "REVIVAL":
        state, score = "REVIVAL_ENTRY", 65
    elif features.get("stage") == "MIGRATED" and migration.get("state") == "HEALTHY":
        state, score = "POST_MIGRATION_ENTRY", 70
    elif initial is None or initial <= 0 or features.get("market_cap_unit") != "SOL":
        state, score = "UNKNOWN", None
    else:
        multiple = market_cap / initial
        real_acceleration = (buyer_growth or 0) > 0 and (buyer_acceleration or 0) >= 0
        liquid = liquidity_drawdown is None or liquidity_drawdown > -0.3
        if multiple >= 5 or age >= 5400:
            state, score = "LATE", 10
        elif multiple >= 3 and real_acceleration and liquid:
            state, score = "ACCELERATING_BUT_ENTRY_VALID", 68
        elif multiple >= 3:
            state, score = "CHASE", 20
        elif multiple >= 1.7 and real_acceleration:
            state, score = "CONFIRMED_EARLY", 78
        elif multiple >= 1.7:
            state, score = "EXTENDED", 45
        elif age <= 180 and (buyer_growth or 0) >= 0:
            state, score = "EARLY_VALID", 85
        elif age <= 600:
            state, score = "EARLY_HIGH_RISK", 55
        else:
            state, score = "OVERHEATED" if (velocity or 0) > 0.01 else "EXTENDED"
            score = 35 if state == "OVERHEATED" else 45
    return {
        "state": state,
        "score": score,
        "coverage": coverage,
        "confidence": coverage if score is not None else min(coverage, 40),
    }


def _signal(
    identifier_id: str,
    features: Mapping[str, Any],
    present: bool | None,
    value: float | None,
    evidence: tuple[str, ...] = (),
) -> IdentifierSignal:
    definition = IDENTIFIER_REGISTRY.get(identifier_id)
    known = sum(features.get(name) is not None for name in definition.required_inputs)
    coverage = known / len(definition.required_inputs) * 100 if definition.required_inputs else 100
    if present is None or known < len(definition.required_inputs):
        state = IdentifierState.UNKNOWN
        value = None
    else:
        state = IdentifierState.PRESENT if present else IdentifierState.ABSENT
    return IdentifierSignal(
        identifier_id,
        state,
        value,
        coverage,
        coverage,
        int(features.get("age_seconds") or 0) or None,
        evidence,
    )


def evaluate_identifiers(features: Mapping[str, Any]) -> list[IdentifierSignal]:
    n = lambda name: _number(features.get(name))
    mc, age = n("current_market_cap"), n("age_seconds")
    sol = features.get("market_cap_unit") == "SOL"
    entry = str(features.get("entry_quality_state") or "UNKNOWN")
    migration = str(features.get("migration_continuity_state") or "UNKNOWN")
    creator_rate = _ratio(features.get("creator_past_rugs"), features.get("creator_past_tokens"))
    rules: dict[str, tuple[bool | None, float | None]] = {
        "EXTREME_EARLY_MC": (None if mc is None or not sol else mc < 5, mc),
        "EARLY_MC_POSITION": (
            None if mc is None or age is None or not sol else 5 <= mc < 20 and age < 600,
            mc,
        ),
        "DEAD_ZONE_MC": (None if mc is None or not sol else 20 <= mc < 30, mc),
        "OVEREXTENDED_MC": (
            None if mc is None or age is None or not sol else mc >= 250 and age < 600,
            mc,
        ),
        "MC_VELOCITY_STRONG": (
            None if n("market_cap_velocity") is None else n("market_cap_velocity") > 0.003,
            n("market_cap_velocity"),
        ),
        "MC_ACCELERATION_STRONG": (
            None
            if n("market_cap_acceleration") is None
            else n("market_cap_acceleration") > 0.00002,
            n("market_cap_acceleration"),
        ),
        "BUYER_LEVEL_STRONG": (
            None if n("buyer_count") is None else n("buyer_count") >= 20,
            n("buyer_count"),
        ),
        "BUYER_GROWTH_STRONG": (
            None if n("buyer_growth") is None else n("buyer_growth") >= 5,
            n("buyer_growth"),
        ),
        "BUYER_ACCELERATION_STRONG": (
            None if n("buyer_acceleration") is None else n("buyer_acceleration") >= 3,
            n("buyer_acceleration"),
        ),
        "BUYER_PERSISTENCE": (
            None if n("buyer_count_persistence") is None else n("buyer_count_persistence") >= 2 / 3,
            n("buyer_count_persistence"),
        ),
        "BUYER_EXHAUSTION": (
            None
            if n("buyer_growth") is None or n("buyer_acceleration") is None
            else n("buyer_growth") <= 0 and n("buyer_acceleration") < 0,
            n("buyer_growth"),
        ),
        "NET_BUY_FLOW_POSITIVE": (
            None
            if n("buy_count") is None or n("sell_count") is None
            else n("buy_count") > n("sell_count"),
            n("net_buyers"),
        ),
        "SELLER_DOMINANCE": (
            None
            if n("buy_count") is None or n("sell_count") is None
            else n("sell_count") > 1.25 * n("buy_count"),
            _ratio(features.get("sell_count"), features.get("buy_count")),
        ),
        "INDEPENDENT_BUYER_EXPANSION": (
            None
            if n("independent_buyer_ratio") is None or n("buyer_growth") is None
            else n("independent_buyer_ratio") >= 0.7 and n("buyer_growth") >= 3,
            n("independent_buyer_ratio"),
        ),
        "REPEAT_RUNNER_WALLET": (
            None if n("high_quality_buyer_count") is None else n("high_quality_buyer_count") >= 2,
            n("high_quality_buyer_count"),
        ),
        "FAST_FLIPPER_DOMINANCE": (
            None if n("fast_flipper_share") is None else n("fast_flipper_share") >= 0.5,
            n("fast_flipper_share"),
        ),
        "CREATOR_SURVIVAL_HISTORY": (
            None
            if n("creator_past_tokens") is None or creator_rate is None
            else n("creator_past_tokens") >= 3 and creator_rate <= 0.1,
            creator_rate,
        ),
        "TOXIC_CREATOR_HISTORY": (
            None
            if n("creator_past_tokens") is None or creator_rate is None
            else n("creator_past_tokens") >= 3 and creator_rate >= 0.5,
            creator_rate,
        ),
        "CREATOR_LINKED_DEMAND": (
            None
            if n("creator_linked_buyer_share") is None
            else n("creator_linked_buyer_share") >= 0.3,
            n("creator_linked_buyer_share"),
        ),
        "SHARED_FUNDER_CLUSTER": (
            None if n("shared_funder_confidence") is None else n("shared_funder_confidence") >= 0.7,
            n("shared_funder_confidence"),
        ),
        "LIQUIDITY_EXPANSION": (
            None if n("liquidity_velocity") is None else n("liquidity_velocity") > 0,
            n("liquidity_velocity"),
        ),
        "LIQUIDITY_ACCELERATION": (
            None if n("liquidity_acceleration") is None else n("liquidity_acceleration") > 0,
            n("liquidity_acceleration"),
        ),
        "LIQUIDITY_WITHDRAWAL": (
            None if n("liquidity_drawdown") is None else n("liquidity_drawdown") <= -0.3,
            n("liquidity_drawdown"),
        ),
        "LOW_LIQUIDITY_TO_MC": (
            None if n("liquidity_to_market_cap") is None else n("liquidity_to_market_cap") < 0.03,
            n("liquidity_to_market_cap"),
        ),
        "PERSISTENT_MOMENTUM": (
            None
            if n("market_cap_persistence") is None or n("buyer_count_persistence") is None
            else n("market_cap_persistence") >= 2 / 3 and n("buyer_count_persistence") >= 2 / 3,
            min(n("market_cap_persistence"), n("buyer_count_persistence"))
            if n("market_cap_persistence") is not None and n("buyer_count_persistence") is not None
            else None,
        ),
        "PRICE_UP_BUYERS_DOWN": (
            None
            if n("price_return_pct") is None or n("buyer_growth") is None
            else n("price_return_pct") > 20 and n("buyer_growth") <= 0,
            n("price_return_pct"),
        ),
        "PRICE_UP_LIQUIDITY_DOWN": (
            None
            if n("price_return_pct") is None or n("liquidity_velocity") is None
            else n("price_return_pct") > 20 and n("liquidity_velocity") < 0,
            n("liquidity_velocity"),
        ),
        "CONCENTRATION_RELEASE": (
            None if n("concentration_change") is None else n("concentration_change") <= -5,
            n("concentration_change"),
        ),
        "CONCENTRATION_INCREASE": (
            None if n("concentration_change") is None else n("concentration_change") >= 5,
            n("concentration_change"),
        ),
        "TRADEABILITY_GOOD": (
            None if n("tradeability_score") is None else n("tradeability_score") >= 70,
            n("tradeability_score"),
        ),
        "TRADEABILITY_POOR": (
            None if n("tradeability_score") is None else n("tradeability_score") < 40,
            n("tradeability_score"),
        ),
        "HEALTHY_MIGRATION": (None if migration == "UNKNOWN" else migration == "HEALTHY", None),
        "UNHEALTHY_MIGRATION": (
            None if migration == "UNKNOWN" else migration in {"WEAK", "DISRUPTED", "SUSPICIOUS"},
            None,
        ),
        "PREPARED_LAUNCH": (
            None if n("prepared_launch_score") is None else n("prepared_launch_score") >= 70,
            n("prepared_launch_score"),
        ),
        "SOCIAL_BOT_ACTIVITY": (
            None if n("social_bot_share") is None else n("social_bot_share") >= 0.5,
            n("social_bot_share"),
        ),
        "HIGH_LAUNCH_INTENSITY": (
            None
            if n("launch_intensity_percentile") is None
            else n("launch_intensity_percentile") >= 75,
            n("launch_intensity_percentile"),
        ),
        "RISK_OFF_REGIME": (
            None
            if features.get("risk_regime") is None
            else features.get("risk_regime") == "RISK_OFF",
            None,
        ),
        "ENTRY_VALID": (
            None
            if entry == "UNKNOWN"
            else entry
            in {
                "EARLY_VALID",
                "CONFIRMED_EARLY",
                "ACCELERATING_BUT_ENTRY_VALID",
                "POST_MIGRATION_ENTRY",
                "REVIVAL_ENTRY",
            },
            n("entry_quality_score"),
        ),
        "OVEREXTENDED_ENTRY": (
            None if entry == "UNKNOWN" else entry in {"OVERHEATED", "CHASE", "LATE"},
            n("entry_quality_score"),
        ),
        "SURVIVAL_HIGH_CONFIDENCE": (
            None
            if n("survival_score") is None or n("survival_confidence") is None
            else n("survival_score") >= 70 and n("survival_confidence") >= 70,
            n("survival_score"),
        ),
        "SURVIVAL_SPARSE": (
            None if n("survival_confidence") is None else n("survival_confidence") < 50,
            n("survival_confidence"),
        ),
        "HARD_FAILURE_PRESENT": (
            None if n("hard_failure_score") is None else n("hard_failure_score") >= 50,
            n("hard_failure_score"),
        ),
        "SOFT_FAILURE_ELEVATED": (
            None if n("soft_failure_score") is None else n("soft_failure_score") >= 40,
            n("soft_failure_score"),
        ),
        "REVIVAL_CATALYST_PRESENT": (
            None if n("fresh_catalyst_score") is None else n("fresh_catalyst_score") >= 70,
            n("fresh_catalyst_score"),
        ),
        "REVIVAL_WITHOUT_CATALYST": (
            None
            if features.get("stage") is None
            else features.get("stage") == "REVIVAL" and (n("fresh_catalyst_score") or 0) < 40,
            n("fresh_catalyst_score"),
        ),
        "WASH_VOLUME_RISK": (
            None
            if n("trades_per_unique_buyer") is None or n("median_trade_size") is None
            else n("trades_per_unique_buyer") >= 20 and n("median_trade_size") < 0.05,
            n("trades_per_unique_buyer"),
        ),
        "SYBIL_BUYER_RISK": (
            None
            if n("sybil_adjusted_buyer_ratio") is None
            else n("sybil_adjusted_buyer_ratio") < 0.5,
            n("sybil_adjusted_buyer_ratio"),
        ),
    }
    return [
        _signal(identifier_id, features, *rules[identifier_id])
        for identifier_id in (definition.identifier_id for definition in IDENTIFIER_REGISTRY.all())
    ]


OBJECTIVE_WEIGHTS: dict[Objective, dict[str, float]] = {
    Objective.QUICK_2X: {
        "EARLY_MC_POSITION": 12,
        "BUYER_LEVEL_STRONG": 15,
        "NET_BUY_FLOW_POSITIVE": 12,
        "ENTRY_VALID": 18,
        "TRADEABILITY_GOOD": 10,
        "SURVIVAL_HIGH_CONFIDENCE": 12,
        "DEAD_ZONE_MC": -10,
        "BUYER_EXHAUSTION": -18,
        "OVEREXTENDED_ENTRY": -20,
    },
    Objective.MID_5X: {
        "EARLY_MC_POSITION": 8,
        "BUYER_GROWTH_STRONG": 18,
        "BUYER_PERSISTENCE": 15,
        "PERSISTENT_MOMENTUM": 14,
        "LIQUIDITY_EXPANSION": 10,
        "ENTRY_VALID": 12,
        "BUYER_EXHAUSTION": -20,
        "DEAD_ZONE_MC": -10,
        "OVEREXTENDED_ENTRY": -18,
    },
    Objective.RIGHT_TAIL: {
        "EXTREME_EARLY_MC": 15,
        "BUYER_GROWTH_STRONG": 14,
        "BUYER_ACCELERATION_STRONG": 18,
        "INDEPENDENT_BUYER_EXPANSION": 18,
        "MC_ACCELERATION_STRONG": 12,
        "PERSISTENT_MOMENTUM": 12,
        "LIQUIDITY_ACCELERATION": 10,
        "HEALTHY_MIGRATION": 10,
        "PRICE_UP_BUYERS_DOWN": -18,
        "CREATOR_LINKED_DEMAND": -20,
        "SYBIL_BUYER_RISK": -22,
        "LIQUIDITY_WITHDRAWAL": -25,
    },
}


def _objective_result(objective: Objective, signals: Sequence[IdentifierSignal]) -> ObjectiveResult:
    by_id = {signal.identifier_id: signal for signal in signals}
    weights = OBJECTIVE_WEIGHTS[objective]
    known = [by_id[name] for name in weights if by_id[name].state != IdentifierState.UNKNOWN]
    positives = [
        name
        for name, weight in weights.items()
        if weight > 0 and by_id[name].state == IdentifierState.PRESENT
    ]
    negatives = [
        name
        for name, weight in weights.items()
        if weight < 0 and by_id[name].state == IdentifierState.PRESENT
    ]
    adjustment = sum(weights[name] for name in positives + negatives)
    coverage = len(known) / len(weights) * 100
    confidence = statistics.fmean(signal.confidence for signal in known) if known else 0
    score = min(100, max(0, 50 + adjustment))
    return ObjectiveResult(
        objective,
        round(score, 3),
        round(confidence, 3),
        round(coverage, 3),
        tuple(positives),
        tuple(negatives),
    )


def research_signal_policy(
    objectives: Mapping[str, ObjectiveResult],
    entry: Mapping[str, Any],
    survival: Mapping[str, Any],
    failure: Mapping[str, Any],
    features: Mapping[str, Any],
) -> str:
    quick, mid, right = (
        objectives[name].score
        for name in (Objective.QUICK_2X, Objective.MID_5X, Objective.RIGHT_TAIL)
    )
    valid_entry = entry.get("state") in {
        "EARLY_VALID",
        "CONFIRMED_EARLY",
        "ACCELERATING_BUT_ENTRY_VALID",
        "POST_MIGRATION_ENTRY",
        "REVIVAL_ENTRY",
    }
    if failure["terminal_block"]:
        return "REJECT"
    if features.get("stage") == "REVIVAL" and right >= 65 and valid_entry:
        return "CATALYST_REVIVAL_RESEARCH"
    if right >= 85 and valid_entry and failure["soft_failure_score"] >= 35:
        return "HIGH_RISK_MOMENTUM_RESEARCH"
    if right >= 85 and valid_entry and survival["confidence"] >= 60:
        return "RIGHT_TAIL_ALERT_RESEARCH"
    if quick >= 70 and mid >= 65 and valid_entry and survival["score"] >= 60:
        return "PREMIUM_RESEARCH"
    if (quick >= 65 or mid >= 65) and valid_entry:
        return "STRONG_RESEARCH"
    return "SILENT_WATCH_RESEARCH"


class IntelligenceV2Research:
    """Research-only multi-objective decision layer; never routes public alerts."""

    def evaluate(self, observations: Sequence[Mapping[str, Any]]) -> V2ResearchDecision:
        started = time.perf_counter()
        features = build_trajectory_features(observations)
        migration = migration_continuity_v2(features)
        failure = failure_v2(features)
        survival = survival_v2(features, failure)
        entry = entry_quality_v2(features, migration)
        features.update(
            {
                "migration_continuity_state": migration["state"],
                "hard_failure_score": failure["hard_failure_score"],
                "soft_failure_score": failure["soft_failure_score"],
                "survival_score": survival["score"],
                "survival_confidence": survival["confidence"],
                "entry_quality_state": entry["state"],
                "entry_quality_score": entry["score"],
            }
        )
        signals = evaluate_identifiers(features)
        objectives = {
            objective: _objective_result(objective, signals)
            for objective in (Objective.QUICK_2X, Objective.MID_5X, Objective.RIGHT_TAIL)
        }
        known_signals = [signal for signal in signals if signal.state != IdentifierState.UNKNOWN]
        coverage = len(known_signals) / len(signals) * 100
        confidence = (
            statistics.fmean(signal.confidence for signal in known_signals) if known_signals else 0
        )
        policy = research_signal_policy(objectives, entry, survival, failure, features)
        positives = [
            signal.identifier_id
            for signal in signals
            if signal.state == IdentifierState.PRESENT
            and IDENTIFIER_REGISTRY.get(signal.identifier_id).direction == "POSITIVE"
        ]
        negatives = [
            signal.identifier_id
            for signal in signals
            if signal.state == IdentifierState.PRESENT
            and IDENTIFIER_REGISTRY.get(signal.identifier_id).direction == "NEGATIVE"
        ]
        return V2ResearchDecision(
            version=INTELLIGENCE_V2_VERSION,
            control_freeze_sha=CONTROL_FREEZE_SHA,
            objectives={str(name): result for name, result in objectives.items()},
            entry=entry,
            survival=survival,
            failure=failure,
            migration=migration,
            market_cap_regime=classify_market_cap_regime(features),
            identifiers=signals,
            signal_policy=policy,
            why_now=positives[:4],
            risks=negatives[:4],
            evidence_confidence=round(confidence, 3),
            evidence_coverage=round(coverage, 3),
            latency_ms=round((time.perf_counter() - started) * 1000, 6),
        )

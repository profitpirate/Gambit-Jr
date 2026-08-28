from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

V3_MODEL_NAME = "INTELLIGENCE_V3_RESEARCH"
CONTROL_MODEL_NAME = "CONTROL_V15"
V2_MODEL_NAME = "INTELLIGENCE_V2"


class V3Tier(StrEnum):
    PREMIUM = "PREMIUM"
    STRONG = "STRONG"
    HIGH_RISK_MOMENTUM = "HIGH_RISK_MOMENTUM"
    RIGHT_TAIL_WATCH = "RIGHT_TAIL_WATCH"
    SILENT_WATCH = "SILENT_WATCH"
    REJECT = "REJECT"


class CompetingOutcome(StrEnum):
    HIT_2X_BEFORE_STOP = "HIT_2X_BEFORE_STOP"
    HIT_5X_BEFORE_STOP = "HIT_5X_BEFORE_STOP"
    HIT_10X_BEFORE_STOP = "HIT_10X_BEFORE_STOP"
    HIT_20X_BEFORE_FAILURE = "HIT_20X_BEFORE_FAILURE"
    TERMINAL_SAFETY_FAILURE = "TERMINAL_SAFETY_FAILURE"
    LIQUIDITY_COLLAPSE = "LIQUIDITY_COLLAPSE"
    UNSELLABLE = "UNSELLABLE"
    SEVERE_DRAWDOWN = "SEVERE_DRAWDOWN"
    CENSORED = "CENSORED"


@dataclass(frozen=True, slots=True)
class TimedValue:
    value: Any
    observed_at: str
    available_at: str
    provider: str
    state: str = "KNOWN"

    def validate_at(self, decision_timestamp: str) -> None:
        decision = _timestamp(decision_timestamp)
        observed = _timestamp(self.observed_at)
        available = _timestamp(self.available_at)
        if available < observed:
            raise ValueError("available_at cannot precede observed_at")
        if available > decision:
            raise ValueError("future evidence cannot enter a V3 decision")


@dataclass(frozen=True, slots=True)
class HazardForecast:
    quick_2x: float | None = None
    mid_5x: float | None = None
    right_tail: float | None = None
    terminal_failure: float | None = None
    liquidity_failure: float | None = None
    expected_time_to_target_seconds: float | None = None
    expected_time_to_failure_seconds: float | None = None
    calibrated: bool = False
    validation_state: str = "UNVALIDATED"

    def validate(self) -> None:
        for value in (
            self.quick_2x,
            self.mid_5x,
            self.right_tail,
            self.terminal_failure,
            self.liquidity_failure,
        ):
            if value is not None and not 0 <= value <= 1:
                raise ValueError("hazard probabilities must be between zero and one")


@dataclass(frozen=True, slots=True)
class EntryActionability:
    valid: bool
    score: float | None
    sellable: bool | None
    delay_seconds: float
    estimated_fees_percent: float | None = None
    estimated_impact_percent: float | None = None
    reason: str | None = None

    def validate(self) -> None:
        if self.score is not None and not 0 <= self.score <= 1:
            raise ValueError("entry actionability must be between zero and one")
        if self.delay_seconds < 0:
            raise ValueError("entry delay cannot be negative")


@dataclass(slots=True)
class V3DecisionEnvelope:
    candidate_generation: str
    quick_2x_hazard: float | None
    mid_5x_hazard: float | None
    right_tail_hazard: float | None
    terminal_failure_hazard: float | None
    liquidity_failure_hazard: float | None
    entry_actionability: float | None
    expected_utility: float | None
    confidence: float
    coverage: float
    uncertainty: float
    abstain_reason: str | None
    positive_evidence: list[str]
    negative_evidence: list[str]
    hard_risk_evidence: list[str]
    model_versions: dict[str, str]
    feature_versions: dict[str, str]
    decision_timestamp: str
    available_evidence_timestamp: str
    research_tier: V3Tier = V3Tier.SILENT_WATCH
    nominated_by: str = "NONE"
    risk_cap: str | None = None
    precision_gate: str = "REJECTED"
    legacy_result: dict[str, Any] = field(default_factory=dict)
    v15_result: dict[str, Any] = field(default_factory=dict)
    expected_time_to_target_seconds: float | None = None
    expected_time_to_failure_seconds: float | None = None
    public_route: bool = False

    def validate(self) -> None:
        if self.public_route:
            raise ValueError("Intelligence V3 is research-only and cannot use a public route")
        decision = _timestamp(self.decision_timestamp)
        available = _timestamp(self.available_evidence_timestamp)
        if available > decision:
            raise ValueError("available evidence timestamp cannot be after the decision")
        for value in (
            self.quick_2x_hazard,
            self.mid_5x_hazard,
            self.right_tail_hazard,
            self.terminal_failure_hazard,
            self.liquidity_failure_hazard,
            self.entry_actionability,
            self.confidence,
            self.coverage,
            self.uncertainty,
        ):
            if value is not None and not 0 <= value <= 1:
                raise ValueError("probabilities, confidence, coverage and uncertainty must be [0, 1]")
        if self.model_versions.get("v3") != V3_MODEL_NAME:
            raise ValueError("V3 model version must explicitly identify the research model")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SelectiveGatePolicy:
    version: str
    validated: bool = False
    premium_probability: float = 0.70
    strong_probability: float = 0.55
    minimum_coverage: float = 0.80
    maximum_uncertainty: float = 0.15
    maximum_terminal_failure: float = 0.05
    minimum_actionability: float = 0.65


class V3ShadowEngine:
    """One research-only decision truth. It has no notifier or outbox dependency."""

    def __init__(self, policy: SelectiveGatePolicy | None = None):
        self.policy = policy or SelectiveGatePolicy(version="v3-gate-unvalidated")

    def evaluate(
        self,
        *,
        decision_timestamp: str,
        evidence: Mapping[str, TimedValue],
        forecast: HazardForecast | None,
        actionability: EntryActionability,
        legacy_result: Mapping[str, Any],
        v15_result: Mapping[str, Any],
        positive_evidence: Iterable[str] = (),
        negative_evidence: Iterable[str] = (),
        hard_risk_evidence: Iterable[str] = (),
        feature_versions: Mapping[str, str] | None = None,
    ) -> V3DecisionEnvelope:
        actionability.validate()
        usable: list[TimedValue] = []
        for item in evidence.values():
            item.validate_at(decision_timestamp)
            if item.state == "KNOWN" and item.value is not None:
                usable.append(item)
        coverage = len(usable) / len(evidence) if evidence else 0.0
        available_at = max(
            (item.available_at for item in usable),
            default=decision_timestamp,
            key=_timestamp,
        )
        hazards = forecast or HazardForecast()
        hazards.validate()
        known_hazards = [
            value
            for value in (
                hazards.quick_2x,
                hazards.mid_5x,
                hazards.right_tail,
                hazards.terminal_failure,
                hazards.liquidity_failure,
            )
            if value is not None
        ]
        uncertainty = 1.0 if not known_hazards else max(0.0, 1.0 - coverage)
        confidence = max(0.0, min(1.0, coverage * (1.0 - uncertainty)))
        hard_risks = sorted(set(hard_risk_evidence))
        nominated = "QUICK_2X" if hazards.quick_2x is not None else "NONE"
        expected_utility = _expected_utility(hazards, actionability)

        tier = V3Tier.SILENT_WATCH
        abstain: str | None = None
        risk_cap: str | None = None
        gate = "REJECTED"
        terminal = hazards.terminal_failure
        if hard_risks:
            tier = V3Tier.REJECT
            risk_cap = "HARD_RISK"
            abstain = "HARD_RISK_EVIDENCE"
        elif forecast is None:
            abstain = "NO_V3_MODEL_FORECAST"
        elif not self.policy.validated or hazards.validation_state != "SEALED_VALIDATED":
            abstain = "UNVALIDATED_RESEARCH_MODEL"
        elif not hazards.calibrated:
            abstain = "UNCALIBRATED_PROBABILITIES"
        elif coverage < self.policy.minimum_coverage:
            abstain = "INSUFFICIENT_COVERAGE"
        elif uncertainty > self.policy.maximum_uncertainty:
            abstain = "UNCERTAINTY_TOO_HIGH"
        elif not actionability.valid or actionability.score is None:
            abstain = actionability.reason or "ENTRY_NOT_ACTIONABLE"
        elif actionability.score < self.policy.minimum_actionability:
            abstain = "ENTRY_ACTIONABILITY_TOO_LOW"
        elif terminal is None:
            abstain = "TERMINAL_FAILURE_UNKNOWN"
        elif terminal > self.policy.maximum_terminal_failure:
            tier = V3Tier.REJECT
            risk_cap = "TERMINAL_FAILURE"
            abstain = "TERMINAL_FAILURE_TOO_HIGH"
        elif hazards.quick_2x is not None and hazards.quick_2x >= self.policy.premium_probability:
            tier = V3Tier.PREMIUM
            gate = "ACCEPTED_PREMIUM"
        elif hazards.quick_2x is not None and hazards.quick_2x >= self.policy.strong_probability:
            tier = V3Tier.STRONG
            gate = "ACCEPTED_STRONG"
        elif hazards.right_tail is not None and hazards.right_tail > 0:
            tier = V3Tier.RIGHT_TAIL_WATCH
            gate = "ACCEPTED_WATCH_ONLY"
        else:
            tier = V3Tier.REJECT
            abstain = "SELECTIVE_GATE_REJECTED"

        envelope = V3DecisionEnvelope(
            candidate_generation="NOMINATED" if nominated != "NONE" else "NOT_NOMINATED",
            quick_2x_hazard=hazards.quick_2x,
            mid_5x_hazard=hazards.mid_5x,
            right_tail_hazard=hazards.right_tail,
            terminal_failure_hazard=hazards.terminal_failure,
            liquidity_failure_hazard=hazards.liquidity_failure,
            entry_actionability=actionability.score,
            expected_utility=expected_utility,
            confidence=confidence,
            coverage=coverage,
            uncertainty=uncertainty,
            abstain_reason=abstain,
            positive_evidence=sorted(set(positive_evidence)),
            negative_evidence=sorted(set(negative_evidence)),
            hard_risk_evidence=hard_risks,
            model_versions={
                "control": CONTROL_MODEL_NAME,
                "v2": V2_MODEL_NAME,
                "v3": V3_MODEL_NAME,
                "precision_gate": self.policy.version,
            },
            feature_versions=dict(feature_versions or {"core": "v3.0.0"}),
            decision_timestamp=decision_timestamp,
            available_evidence_timestamp=available_at,
            research_tier=tier,
            nominated_by=nominated,
            risk_cap=risk_cap,
            precision_gate=gate,
            legacy_result=dict(legacy_result),
            v15_result=dict(v15_result),
            expected_time_to_target_seconds=hazards.expected_time_to_target_seconds,
            expected_time_to_failure_seconds=hazards.expected_time_to_failure_seconds,
            public_route=False,
        )
        envelope.validate()
        return envelope


HAZARD_EVENTS = (
    "2X",
    "5X",
    "10X",
    "20X",
    "TERMINAL_FAILURE",
    "LIQUIDITY_COLLAPSE",
)


@dataclass(frozen=True, slots=True)
class DiscreteHazardRow:
    interval: int
    features: Mapping[str, float]
    event: str | None = None
    censored: bool = False
    sample_weight: float = 1.0


class DiscreteCompetingRiskModel:
    """Regularized one-vs-rest discrete hazards with explicit competing normalization."""

    def __init__(
        self,
        *,
        feature_names: Sequence[str],
        model_version: str,
        regularization: float = 0.01,
    ):
        self.feature_names = tuple(feature_names)
        self.model_version = model_version
        self.regularization = regularization
        self.coefficients: dict[str, list[float]] = {}
        self.fitted = False

    def fit(
        self,
        rows: Sequence[DiscreteHazardRow],
        *,
        iterations: int = 300,
        learning_rate: float = 0.05,
    ) -> None:
        if not rows:
            raise ValueError("hazard training rows are required")
        if any(row.event not in (*HAZARD_EVENTS, None) for row in rows):
            raise ValueError("unknown competing event")
        matrix = [self._vector(row) for row in rows]
        for event in HAZARD_EVENTS:
            weights = [0.0] * (len(self.feature_names) + 2)
            for _ in range(iterations):
                gradient = [0.0] * len(weights)
                total_weight = 0.0
                for row, vector in zip(rows, matrix, strict=True):
                    target = float(row.event == event)
                    prediction = _sigmoid(sum(a * b for a, b in zip(weights, vector, strict=True)))
                    importance = max(0.0, row.sample_weight)
                    total_weight += importance
                    for index, value in enumerate(vector):
                        gradient[index] += importance * (prediction - target) * value
                scale = max(1.0, total_weight)
                for index in range(len(weights)):
                    penalty = 0.0 if index == 0 else self.regularization * weights[index]
                    weights[index] -= learning_rate * (gradient[index] / scale + penalty)
            self.coefficients[event] = weights
        self.fitted = True

    def interval_hazards(self, interval: int, features: Mapping[str, float]) -> dict[str, float]:
        if not self.fitted:
            raise ValueError("hazard model is not fitted")
        row = DiscreteHazardRow(interval=interval, features=features)
        vector = self._vector(row)
        raw = {
            event: _sigmoid(
                sum(
                    coefficient * value
                    for coefficient, value in zip(
                        self.coefficients[event], vector, strict=True
                    )
                )
            )
            for event in HAZARD_EVENTS
        }
        total = sum(raw.values())
        if total >= 1:
            raw = {event: value / (total + 1e-12) * 0.999999 for event, value in raw.items()}
        return raw

    def forecast(self, intervals: Sequence[Mapping[str, float]]) -> HazardForecast:
        if not intervals:
            raise ValueError("time-varying interval features are required")
        survival = 1.0
        cumulative = {event: 0.0 for event in HAZARD_EVENTS}
        target_time = 0.0
        failure_time = 0.0
        target_mass = 0.0
        failure_mass = 0.0
        for interval, features in enumerate(intervals):
            hazards = self.interval_hazards(interval, features)
            for event, hazard in hazards.items():
                mass = survival * hazard
                cumulative[event] += mass
                if event in {"2X", "5X", "10X", "20X"}:
                    target_time += interval * mass
                    target_mass += mass
                else:
                    failure_time += interval * mass
                    failure_mass += mass
            survival *= max(0.0, 1.0 - sum(hazards.values()))
        return HazardForecast(
            quick_2x=min(1.0, cumulative["2X"]),
            mid_5x=min(1.0, cumulative["5X"]),
            right_tail=min(1.0, cumulative["10X"] + cumulative["20X"]),
            terminal_failure=min(1.0, cumulative["TERMINAL_FAILURE"]),
            liquidity_failure=min(1.0, cumulative["LIQUIDITY_COLLAPSE"]),
            expected_time_to_target_seconds=(
                target_time / target_mass if target_mass else None
            ),
            expected_time_to_failure_seconds=(
                failure_time / failure_mass if failure_mass else None
            ),
            calibrated=False,
            validation_state="UNVALIDATED",
        )

    def _vector(self, row: DiscreteHazardRow) -> list[float]:
        unknown = set(row.features) - set(self.feature_names)
        if unknown:
            raise ValueError(f"unknown hazard features: {', '.join(sorted(unknown))}")
        values = [float(row.features.get(name, 0.0)) for name in self.feature_names]
        if any(not math.isfinite(value) for value in values):
            raise ValueError("hazard features must be finite")
        return [1.0, math.log1p(row.interval), *values]


@dataclass(frozen=True, slots=True)
class TradeEvent:
    timestamp: str
    wallet: str
    side: str
    sol_amount: float
    token_amount: float | None = None
    cluster_id: str | None = None
    creator_linked: bool = False
    jito_bundle_id: str | None = None
    wash: bool = False


@dataclass(frozen=True, slots=True)
class CurveState:
    timestamp: str
    real_sol_reserve: float | None
    virtual_sol_reserve: float | None
    curve_progress: float | None
    market_cap: float | None
    liquidity: float | None
    price: float | None


def liquidity_order_flow_features(
    *,
    decision_timestamp: str,
    launched_at: str,
    trades: Sequence[TradeEvent],
    curve_states: Sequence[CurveState],
    windows_seconds: Sequence[int] = (15, 30, 60, 90, 180, 300, 600, 1800),
) -> dict[str, Any]:
    """Derive native-window features without inventing observations between events."""
    decision = _timestamp(decision_timestamp)
    launch = _timestamp(launched_at)
    if launch > decision:
        raise ValueError("launch cannot occur after the decision")
    for event in trades:
        if _timestamp(event.timestamp) > decision:
            raise ValueError("future trade supplied to PIT feature builder")
        if event.side not in {"buy", "sell"}:
            raise ValueError("trade side must be buy or sell")
    for state in curve_states:
        if _timestamp(state.timestamp) > decision:
            raise ValueError("future curve state supplied to PIT feature builder")

    ordered_states = sorted(curve_states, key=lambda row: _timestamp(row.timestamp))
    latest = ordered_states[-1] if ordered_states else None
    output: dict[str, Any] = {
        "decision_timestamp": decision_timestamp,
        "market_cap": latest.market_cap if latest else None,
        "real_sol_reserve": latest.real_sol_reserve if latest else None,
        "virtual_sol_reserve": latest.virtual_sol_reserve if latest else None,
        "liquidity": latest.liquidity if latest else None,
        "price": latest.price if latest else None,
        "curve_progress": latest.curve_progress if latest else None,
        "windows": {},
    }
    for seconds in windows_seconds:
        boundary = min(decision, launch + timedelta(seconds=seconds))
        window_trades = [event for event in trades if launch <= _timestamp(event.timestamp) <= boundary]
        window_states = [state for state in ordered_states if launch <= _timestamp(state.timestamp) <= boundary]
        buys = [event for event in window_trades if event.side == "buy"]
        sells = [event for event in window_trades if event.side == "sell"]
        buy_sol = sum(max(0.0, event.sol_amount) for event in buys)
        sell_sol = sum(max(0.0, event.sol_amount) for event in sells)
        duration = max(1.0, (boundary - launch).total_seconds())
        independent_buyers = {
            event.cluster_id or event.wallet for event in buys if not event.creator_linked
        }
        raw_buyers = {event.wallet for event in buys}
        wash_volume = sum(event.sol_amount for event in window_trades if event.wash)
        creator_buy = sum(event.sol_amount for event in buys if event.creator_linked)
        first_state = window_states[0] if window_states else None
        last_state = window_states[-1] if window_states else None
        reserve_delta = _delta(first_state, last_state, "real_sol_reserve")
        progress_delta = _delta(first_state, last_state, "curve_progress")
        output["windows"][str(seconds)] = {
            "native_observation": bool(window_trades or window_states),
            "elapsed_seconds": duration,
            "trade_count": len(window_trades),
            "unique_buyers": len(raw_buyers),
            "independent_buyers": len(independent_buyers),
            "buy_volume_sol": buy_sol,
            "sell_volume_sol": sell_sol,
            "net_sol_flow": buy_sol - sell_sol,
            "net_sol_flow_velocity": (buy_sol - sell_sol) / duration,
            "order_flow_imbalance": _safe_ratio(buy_sol - sell_sol, buy_sol + sell_sol),
            "buy_arrival_intensity": len(buys) / duration,
            "sell_arrival_intensity": len(sells) / duration,
            "median_trade_size_sol": _median([event.sol_amount for event in window_trades]),
            "trade_size_dispersion_sol": _population_std(
                [event.sol_amount for event in window_trades]
            ),
            "repeat_wallet_share": _repeat_share(window_trades),
            "creator_linked_buy_share": _safe_ratio(creator_buy, buy_sol),
            "wash_adjusted_volume_sol": max(0.0, buy_sol + sell_sol - wash_volume),
            "real_reserve_delta": reserve_delta,
            "real_reserve_velocity_per_second": (
                reserve_delta / duration if reserve_delta is not None else None
            ),
            "real_reserve_velocity_per_trade": (
                reserve_delta / len(window_trades)
                if reserve_delta is not None and window_trades
                else None
            ),
            "curve_progress_delta": progress_delta,
            "curve_progress_velocity": (
                progress_delta / duration if progress_delta is not None else None
            ),
        }
    return output


def activity_evidence(
    *,
    raw_buyers: int,
    independent_buyers: int,
    raw_volume: float,
    wash_adjusted_volume: float,
    creator_linked_share: float | None,
    bundle_linked_share: float | None,
    whale_share: float | None,
    tiny_buy_share: float | None,
    recycled_share: float | None,
) -> dict[str, float | bool | None]:
    """Preserve continuous evidence and derive non-exclusive manipulation flags."""
    if min(raw_buyers, independent_buyers) < 0 or min(raw_volume, wash_adjusted_volume) < 0:
        raise ValueError("activity counts and volumes cannot be negative")
    for value in (
        creator_linked_share,
        bundle_linked_share,
        whale_share,
        tiny_buy_share,
        recycled_share,
    ):
        if value is not None and not 0 <= value <= 1:
            raise ValueError("activity shares must be between zero and one")
    independence = independent_buyers / raw_buyers if raw_buyers else None
    authentic_volume = wash_adjusted_volume / raw_volume if raw_volume else None
    return {
        "buyer_independence_ratio": independence,
        "authentic_volume_ratio": authentic_volume,
        "real_distributed_demand": bool(
            independence is not None
            and independence >= 0.7
            and authentic_volume is not None
            and authentic_volume >= 0.8
            and (whale_share is None or whale_share < 0.5)
        ),
        "whale_driven_demand": whale_share is not None and whale_share >= 0.5,
        "wash_volume": authentic_volume is not None and authentic_volume < 0.7,
        "sybil_buyer_growth": independence is not None and independence < 0.5,
        "creator_linked_flow": (
            creator_linked_share is not None and creator_linked_share >= 0.25
        ),
        "bundle_driven_flow": bundle_linked_share is not None and bundle_linked_share >= 0.4,
        "repeated_tiny_buys": tiny_buy_share is not None and tiny_buy_share >= 0.5,
        "buy_sell_recycle": recycled_share is not None and recycled_share >= 0.4,
    }


@dataclass(frozen=True, slots=True)
class MarketPathPoint:
    timestamp: str
    price: float | None
    market_cap: float | None
    liquidity: float | None
    sellable: bool | None = None
    terminal_failure: bool = False
    liquidity_collapse: bool = False


@dataclass(frozen=True, slots=True)
class OutcomeAssessment:
    delay_seconds: int
    raw_outcome: CompetingOutcome
    actionable_outcome: CompetingOutcome
    raw_peak_multiple: float | None
    actionable_peak_multiple: float | None
    entry_timestamp: str
    entry_price: float | None
    estimated_cost_percent: float
    censored_at: str


def assess_actionable_outcome(
    *,
    decision_timestamp: str,
    path: Sequence[MarketPathPoint],
    delay_seconds: int,
    provider_latency_seconds: float = 0,
    model_latency_seconds: float = 0,
    discord_latency_seconds: float = 0,
    fee_percent: float = 1.25,
    trade_notional_usd: float = 100,
    severe_drawdown: float = 0.70,
) -> OutcomeAssessment:
    if delay_seconds < 0 or min(
        provider_latency_seconds, model_latency_seconds, discord_latency_seconds
    ) < 0:
        raise ValueError("latencies and delay must be non-negative")
    ordered = sorted(path, key=lambda row: _timestamp(row.timestamp))
    if not ordered:
        raise ValueError("an observed market path is required")
    decision = _timestamp(decision_timestamp)
    total_delay = (
        delay_seconds
        + provider_latency_seconds
        + model_latency_seconds
        + discord_latency_seconds
    )
    entry_at = decision + timedelta(seconds=total_delay)
    raw_entry = next((point for point in ordered if _timestamp(point.timestamp) >= decision), None)
    delayed_entry = next((point for point in ordered if _timestamp(point.timestamp) >= entry_at), None)
    raw_outcome, raw_peak = _path_outcome(ordered, raw_entry, severe_drawdown, 0.0)
    impact = (
        1.0
        if delayed_entry is None or not delayed_entry.liquidity
        else trade_notional_usd / (delayed_entry.liquidity / 2 + trade_notional_usd)
    )
    cost = fee_percent / 100 + impact
    actionable_outcome, actionable_peak = _path_outcome(
        ordered, delayed_entry, severe_drawdown, cost
    )
    return OutcomeAssessment(
        delay_seconds=delay_seconds,
        raw_outcome=raw_outcome,
        actionable_outcome=actionable_outcome,
        raw_peak_multiple=raw_peak,
        actionable_peak_multiple=actionable_peak,
        entry_timestamp=entry_at.isoformat(),
        entry_price=delayed_entry.price if delayed_entry else None,
        estimated_cost_percent=round(cost * 100, 6),
        censored_at=ordered[-1].timestamp,
    )


def _path_outcome(
    path: Sequence[MarketPathPoint],
    entry: MarketPathPoint | None,
    severe_drawdown: float,
    cost_fraction: float,
) -> tuple[CompetingOutcome, float | None]:
    if entry is None or entry.price is None or entry.price <= 0:
        return CompetingOutcome.CENSORED, None
    if entry.sellable is False:
        return CompetingOutcome.UNSELLABLE, None
    usable = [point for point in path if _timestamp(point.timestamp) >= _timestamp(entry.timestamp)]
    peak = 0.0
    for point in usable:
        if point.terminal_failure:
            return CompetingOutcome.TERMINAL_SAFETY_FAILURE, peak or None
        if point.liquidity_collapse:
            return CompetingOutcome.LIQUIDITY_COLLAPSE, peak or None
        if point.sellable is False:
            return CompetingOutcome.UNSELLABLE, peak or None
        if point.price is None:
            continue
        multiple = point.price / entry.price * (1.0 - cost_fraction)
        peak = max(peak, multiple)
        if multiple <= 1.0 - severe_drawdown:
            return CompetingOutcome.SEVERE_DRAWDOWN, peak
        if multiple >= 20:
            return CompetingOutcome.HIT_20X_BEFORE_FAILURE, peak
        if multiple >= 10:
            return CompetingOutcome.HIT_10X_BEFORE_STOP, peak
        if multiple >= 5:
            return CompetingOutcome.HIT_5X_BEFORE_STOP, peak
        if multiple >= 2:
            return CompetingOutcome.HIT_2X_BEFORE_STOP, peak
    return CompetingOutcome.CENSORED, peak or None


def _expected_utility(
    forecast: HazardForecast, actionability: EntryActionability
) -> float | None:
    if forecast.quick_2x is None or actionability.score is None:
        return None
    terminal = forecast.terminal_failure or 0.0
    liquidity = forecast.liquidity_failure or 0.0
    tail = forecast.right_tail or 0.0
    return round(
        actionability.score * (forecast.quick_2x + 2 * (forecast.mid_5x or 0) + 5 * tail)
        - 2 * terminal
        - liquidity,
        8,
    )


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include timezone: {value}")
    return parsed.astimezone(UTC)


def _delta(first: CurveState | None, last: CurveState | None, name: str) -> float | None:
    if first is None or last is None:
        return None
    start = getattr(first, name)
    end = getattr(last, name)
    if start is None or end is None:
        return None
    return float(end) - float(start)


def _safe_ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _median(values: Sequence[float]) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2


def _population_std(values: Sequence[float]) -> float | None:
    if not values:
        return None
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))


def _repeat_share(events: Sequence[TradeEvent]) -> float | None:
    if not events:
        return None
    counts: dict[str, int] = {}
    for event in events:
        counts[event.wallet] = counts.get(event.wallet, 0) + 1
    return sum(count > 1 for count in counts.values()) / len(counts)


def _sigmoid(value: float) -> float:
    if value >= 0:
        inverse = math.exp(-value)
        return 1 / (1 + inverse)
    exponent = math.exp(value)
    return exponent / (1 + exponent)

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    return (value or utcnow()).astimezone(timezone.utc).isoformat()


class Availability(StrEnum):
    REAL = "REAL"
    ESTIMATED = "ESTIMATED"
    UNKNOWN = "UNKNOWN"
    UNAVAILABLE = "UNAVAILABLE"


class SignalClass(StrEnum):
    IGNORE = "IGNORE"
    WATCH = "WATCH"
    STRONG = "STRONG"
    HIGH_CONVICTION = "HIGH_CONVICTION"
    REJECT = "REJECT"


class CandidateState(StrEnum):
    DISCOVERED = "DISCOVERED"
    SCREENING = "SCREENING"
    CANDIDATE = "CANDIDATE"
    PENDING_EVIDENCE = "PENDING_EVIDENCE"
    FAILED_PROVIDER = "FAILED_PROVIDER"
    WATCH = "WATCH"
    STRONG = "STRONG"
    HIGH_CONVICTION = "HIGH_CONVICTION"
    REJECTED_UNSAFE = "REJECTED_UNSAFE"
    EXPIRED = "EXPIRED"
    SIGNALLED = "SIGNALLED"


class DeveloperClass(StrEnum):
    KNOWN_GOOD = "KNOWN_GOOD"
    KNOWN_OF = "KNOWN_OF"
    UNKNOWN = "UNKNOWN"
    SUSPICIOUS = "SUSPICIOUS"
    KNOWN_BAD = "KNOWN_BAD"


@dataclass(slots=True)
class Metric:
    value: float | int | str | bool | None
    availability: Availability
    source: str
    retrieved_at: str
    freshness_seconds: float | None = None


@dataclass(slots=True)
class DiscoveryEvent:
    token_address: str
    chain: str = "solana"
    symbol: str | None = None
    name: str | None = None
    source: str = "unknown"
    discovered_at: str = field(default_factory=iso)
    estimated_creation_timestamp: str | None = None
    pair_address: str | None = None
    deployer: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MarketSnapshot:
    token_address: str
    captured_at: str
    source: str
    pair_address: str | None = None
    symbol: str | None = None
    name: str | None = None
    dex: str | None = None
    launchpad: str | None = None
    pair_created_at: str | None = None
    price_usd: float | None = None
    market_cap_usd: float | None = None
    fdv_usd: float | None = None
    liquidity_usd: float | None = None
    volume_5m_usd: float | None = None
    volume_1h_usd: float | None = None
    buys_5m: int | None = None
    sells_5m: int | None = None
    price_change_5m: float | None = None
    websites: list[str] = field(default_factory=list)
    socials: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SafetyAssessment:
    checked_at: str
    source: str
    mint_authority: str | None = None
    freeze_authority: str | None = None
    supply_raw: int | None = None
    decimals: int | None = None
    top10_percent: float | None = None
    holder_count: int | None = None
    deployer_percent: float | None = None
    bundled_percent: float | None = None
    rejection_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Intelligence:
    developer_class: DeveloperClass = DeveloperClass.UNKNOWN
    developer_score: float | None = None
    narrative_score: float | None = None
    narrative_label: str | None = None
    social_score: float | None = None
    onchain_score: float | None = None
    momentum_score: float | None = None
    thesis_points: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ScoreResult:
    total: float
    component_scores: dict[str, float]
    component_maxima: dict[str, float]
    classification: SignalClass
    confidence: float
    scoring_version: str
    hard_rejections: list[str] = field(default_factory=list)
    normalized_score: float | None = None
    available_weight: float = 0.0


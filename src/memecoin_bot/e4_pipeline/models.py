from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class CreatorTier(StrEnum):
    NEGATIVE = "NEGATIVE"
    UNKNOWN = "UNKNOWN"
    WATCH = "WATCH"
    APPROVED = "APPROVED"
    ELITE = "ELITE"


@dataclass(frozen=True, slots=True)
class CreatorProfile:
    wallet: str
    tier: CreatorTier
    source: str
    wins: int = 0
    losses: int = 0
    trades: int = 0
    gross_win_rate: float = 0.0
    gross_pnl_sol: float = 0.0
    confidence: float = 0.0
    typical_entry_sol: float | None = None
    max_entry_sol: float | None = None
    runner_count: int = 0
    launch_count: int = 0
    social_handles: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def can_snipe(self) -> bool:
        return self.tier in {CreatorTier.APPROVED, CreatorTier.ELITE}

    @property
    def is_negative(self) -> bool:
        return self.tier == CreatorTier.NEGATIVE


@dataclass(frozen=True, slots=True)
class SocialPost:
    post_id: str
    author_id: str
    author_handle: str
    text: str
    created_ns: int
    received_ns: int
    authority: float
    followers: int = 0
    engagement: float = 0.0
    platform: str = "x"


@dataclass(frozen=True, slots=True)
class NarrativeMatch:
    matched: bool
    score: float
    key: str | None = None
    source_count: int = 0
    authority: float = 0.0
    age_ms: float | None = None
    exact: bool = False
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CopySignal:
    mint: str
    creator: str | None
    observed_ns: int
    e4_entry_price_sol: float | None
    e4_entry_sol: float
    signature: str | None
    source: str = "e4_wallet"


@dataclass(frozen=True, slots=True)
class PipelineDecision:
    accepted: bool
    score: float
    fraction: float
    family: str
    reason: str
    decision_ns: int
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OracleTrade:
    mint: str
    creator: str | None
    opened_ns: int
    entry_sol: float
    entry_tokens: float
    signature: str | None
    sold_sol: float = 0.0
    sold_tokens: float = 0.0
    last_event_ns: int = 0
    closed: bool = False

    @property
    def gross_pnl_sol(self) -> float:
        return self.sold_sol - self.entry_sol

    @property
    def sold_fraction(self) -> float:
        if self.entry_tokens <= 0:
            return 0.0
        return min(1.0, max(0.0, self.sold_tokens / self.entry_tokens))

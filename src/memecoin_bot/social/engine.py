from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from memecoin_bot.models import MarketSnapshot


@dataclass(frozen=True, slots=True)
class SocialObservation:
    observed_at: str
    available_at: str
    source: str
    platform: str
    unique_mentioners: int | None = None
    mentions: int | None = None
    engagement: float | None = None
    sentiment: float | None = None
    bot_spam_share: float | None = None
    official_mentions: int | None = None
    investor_mentions: int | None = None
    telegram_members: int | None = None
    telegram_messages: int | None = None


class SocialEvidenceProvider(Protocol):
    async def history(self, token_address: str, decision_timestamp: str) -> list[SocialObservation]:
        """Return only observations available no later than the decision."""


class SocialEngine:
    def assess(self, current: MarketSnapshot, previous: dict | None = None) -> dict:
        platforms = sorted({s.get("platform", "unknown") for s in current.socials})
        # Links are real, but presence is not velocity and receives no score.
        return {
            "score": None,
            "platforms": platforms,
            "source": current.source,
            "reason": "VELOCITY_PROVIDER_UNAVAILABLE",
        }

    def assess_history(
        self, observations: Sequence[SocialObservation], decision_timestamp: str
    ) -> dict[str, Any]:
        decision = _time(decision_timestamp)
        ordered = sorted(observations, key=lambda row: _time(row.observed_at))
        for row in ordered:
            observed = _time(row.observed_at)
            available = _time(row.available_at)
            if available < observed:
                raise ValueError("social evidence cannot be available before it was observed")
            if available > decision:
                raise ValueError("future social evidence cannot enter a PIT decision")
        if len(ordered) < 2:
            return {
                "score": None,
                "reason": "INSUFFICIENT_PIT_SOCIAL_HISTORY",
                "social_infrastructure": sorted({row.platform for row in ordered}),
                "independent_attention": None,
                "attention_acceleration": None,
                "sentiment": None,
                "bot_adjusted_sentiment": None,
                "official_channel_activity": None,
                "investor_activity": None,
                "cross_platform_confirmation": len({row.platform for row in ordered}),
            }
        first, last = ordered[0], ordered[-1]
        seconds = max(1.0, (_time(last.observed_at) - _time(first.observed_at)).total_seconds())
        mentions_velocity = _velocity(first.mentions, last.mentions, seconds)
        mentioners_velocity = _velocity(first.unique_mentioners, last.unique_mentioners, seconds)
        sentiment = last.sentiment
        bot_adjusted = (
            sentiment * (1 - last.bot_spam_share)
            if sentiment is not None and last.bot_spam_share is not None
            else None
        )
        return {
            # No composite score: attention, sentiment and provenance remain separate.
            "score": None,
            "reason": "SEPARATE_PIT_SOCIAL_COMPONENTS",
            "social_infrastructure": sorted({row.platform for row in ordered}),
            "independent_attention": last.unique_mentioners,
            "mention_velocity_per_second": mentions_velocity,
            "attention_acceleration": mentioners_velocity,
            "sentiment": sentiment,
            "bot_adjusted_sentiment": bot_adjusted,
            "promotional_concentration": (_ratio(last.official_mentions, last.mentions)),
            "official_channel_activity": last.official_mentions,
            "investor_activity": last.investor_mentions,
            "cross_platform_confirmation": len({row.platform for row in ordered}),
            "provider_provenance": sorted({row.source for row in ordered}),
            "available_evidence_timestamp": max((row.available_at for row in ordered), key=_time),
        }


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include timezone: {value}")
    return parsed.astimezone(UTC)


def _velocity(first: int | None, last: int | None, seconds: float) -> float | None:
    if first is None or last is None:
        return None
    return (last - first) / seconds


def _ratio(numerator: int | None, denominator: int | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator

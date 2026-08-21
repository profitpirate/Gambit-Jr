from __future__ import annotations

from datetime import datetime, timezone

from memecoin_bot.config import Settings
from memecoin_bot.models import MarketSnapshot, SafetyAssessment


class SafetyGates:
    def __init__(self, settings: Settings):
        self.settings = settings

    def evaluate(self, market: MarketSnapshot, chain: SafetyAssessment) -> list[str]:
        """Return only terminal, verified safety failures."""
        reasons = list(chain.rejection_reasons)
        if chain.chain == "solana" and self.settings.reject_mint_authority and chain.mint_authority:
            reasons.append("MINT_AUTHORITY_ACTIVE")
        if (
            chain.chain == "solana"
            and self.settings.reject_freeze_authority
            and chain.freeze_authority
        ):
            reasons.append("FREEZE_AUTHORITY_ACTIVE")
        if (
            chain.top10_percent is not None
            and chain.top10_percent > self.settings.max_top10_percent
        ):
            reasons.append("TOP_HOLDER_CONCENTRATION_HIGH")
        return sorted(set(reasons))

    def readiness(self, market: MarketSnapshot) -> list[str]:
        reasons: list[str] = []
        if market.market_cap_usd is None:
            reasons.append("MARKET_CAP_UNAVAILABLE")
        elif market.market_cap_usd < self.settings.min_market_cap_usd:
            reasons.append("MARKET_CAP_BELOW_RANGE")
        if market.liquidity_usd is None:
            reasons.append("LIQUIDITY_UNAVAILABLE")
        elif market.liquidity_usd < self.settings.min_liquidity_usd:
            reasons.append("LIQUIDITY_TOO_LOW")
        if not market.pair_created_at:
            reasons.append("PAIR_AGE_UNAVAILABLE")
        return reasons

    def expiry(self, market: MarketSnapshot, first_discovered_at: str) -> str | None:
        now = datetime.now(timezone.utc)
        discovered = datetime.fromisoformat(first_discovered_at.replace("Z", "+00:00"))
        if (now - discovered).total_seconds() / 60 > self.settings.candidate_max_age_minutes:
            return "CANDIDATE_MAX_AGE_EXCEEDED"
        if (
            market.market_cap_usd is not None
            and market.market_cap_usd > self.settings.candidate_max_market_cap_usd
        ):
            return "MARKET_CAP_ABOVE_CANDIDATE_RANGE"
        if market.pair_created_at:
            created = datetime.fromisoformat(market.pair_created_at.replace("Z", "+00:00"))
            if (now - created).total_seconds() / 60 > self.settings.max_pair_age_minutes:
                return "PAIR_TOO_OLD"
        return None

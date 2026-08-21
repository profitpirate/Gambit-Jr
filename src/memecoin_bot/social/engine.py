from __future__ import annotations

from memecoin_bot.models import MarketSnapshot


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

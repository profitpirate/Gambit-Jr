from __future__ import annotations

from typing import Any

from memecoin_bot.models import DeveloperClass


class DeveloperEngine:
    """Persistent-provider seam. V1 never invents a reputation for an unobserved deployer."""

    def assess(
        self, deployer: str | None, stored_profile: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not deployer:
            return {
                "classification": DeveloperClass.UNKNOWN,
                "score": None,
                "reason": "DEPLOYER_UNAVAILABLE",
            }
        if not stored_profile:
            return {
                "wallet": deployer,
                "classification": DeveloperClass.UNKNOWN,
                "score": None,
                "reason": "NO_TRACKED_HISTORY",
            }
        # Persistence uses the V1.4 creator vocabulary while the legacy scorer
        # expects DeveloperClass plus a 0..15 component score.  Normalize the
        # stored history here so it is consumed by the production evaluation.
        quality = str(stored_profile.get("quality") or "UNKNOWN").upper()
        score_by_quality = {
            "PROVEN": 15.0,
            "POSITIVE": 12.0,
            "NEUTRAL": 7.5,
            "SUSPICIOUS": 2.0,
            "TOXIC": 0.0,
        }
        return {
            "wallet": deployer,
            "classification": quality,
            "score": score_by_quality.get(quality),
            "reason": "TRACKED_CREATOR_HISTORY",
            "launches": int(stored_profile.get("launches") or 0),
            "rugs": int(stored_profile.get("rugs") or 0),
            "runners": int(stored_profile.get("runners") or 0),
        }

from __future__ import annotations

from typing import Any

from memecoin_bot.models import DeveloperClass


class DeveloperEngine:
    """Persistent-provider seam. V1 never invents a reputation for an unobserved deployer."""

    def assess(self, deployer: str | None, stored_profile: dict[str, Any] | None = None) -> dict[str, Any]:
        if not deployer:
            return {"classification": DeveloperClass.UNKNOWN, "score": None,
                    "reason": "DEPLOYER_UNAVAILABLE"}
        if not stored_profile:
            return {"wallet": deployer, "classification": DeveloperClass.UNKNOWN,
                    "score": None, "reason": "NO_TRACKED_HISTORY"}
        return stored_profile


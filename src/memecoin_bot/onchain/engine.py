from __future__ import annotations

from memecoin_bot.models import SafetyAssessment


class OnchainEngine:
    def assess(self, safety: SafetyAssessment) -> dict:
        if safety.top10_percent is None:
            return {
                "score": None,
                "top10_percent": None,
                "bundled_percent": safety.bundled_percent,
                "holder_count": safety.holder_count,
                "reason": "DISTRIBUTION_UNAVAILABLE",
            }
        # 20 at <=10%, declining linearly to 0 at 60%.
        score = max(0.0, min(20.0, 20.0 * (60.0 - safety.top10_percent) / 50.0))
        return {
            "score": round(score, 2),
            "top10_percent": safety.top10_percent,
            "bundled_percent": safety.bundled_percent,
            "holder_count": safety.holder_count,
            "source": safety.source,
        }


from __future__ import annotations

from memecoin_bot.models import SafetyAssessment


class OnchainEngine:
    def assess(self, safety: SafetyAssessment) -> dict:
        if safety.chain == "bsc":
            if "BSC_OWNER_RENOUNCED" in safety.warnings:
                return {"score": 18.0, "owner_state": "RENOUNCED", "source": safety.source,
                        "limitations": ["HOLDER_CONCENTRATION_UNKNOWN", "TRANSFER_RESTRICTIONS_UNKNOWN"]}
            if "BSC_OWNER_ACTIVE" in safety.warnings:
                return {"score": 6.0, "owner_state": "ACTIVE", "source": safety.source,
                        "limitations": ["HOLDER_CONCENTRATION_UNKNOWN", "TRANSFER_RESTRICTIONS_UNKNOWN"]}
            return {"score": None, "owner_state": "UNKNOWN", "reason": "BSC_ADMIN_EVIDENCE_UNAVAILABLE"}
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


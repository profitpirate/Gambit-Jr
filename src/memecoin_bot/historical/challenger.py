from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .store import HistoricalWarehouse


@dataclass(frozen=True, slots=True)
class ChallengerPolicy:
    version: str = "v1.5-historical-challenger-v1"
    minimum_prospective_decisions: int = 250
    minimum_live_days: int = 30
    maximum_p95_latency_ms: float = 25
    public_alerts: bool = False


class ShadowChallenger:
    """Persists decision differences but exposes no alert-routing capability."""

    def __init__(
        self,
        warehouse: HistoricalWarehouse,
        scorer: Callable[[dict[str, Any]], dict[str, Any]],
        policy: ChallengerPolicy | None = None,
    ):
        self.warehouse = warehouse
        self.scorer = scorer
        self.policy = policy or ChallengerPolicy()
        if self.policy.public_alerts:
            raise ValueError("historical challenger must never route public alerts")

    def evaluate(
        self,
        *,
        entity_key: str,
        observed_at: str,
        live_version: str,
        live_decision: dict[str, Any],
        point_in_time_features: dict[str, Any],
    ) -> dict[str, Any]:
        started = time.perf_counter()
        challenger = self.scorer(dict(point_in_time_features))
        latency_ms = (time.perf_counter() - started) * 1000
        challenger = {
            **challenger,
            "latency_ms": latency_ms,
            "public_alert_routed": False,
            "same_point_in_time_evidence": True,
        }
        shadow_id = self.warehouse.record_shadow_decision(
            entity_key=entity_key,
            observed_at=observed_at,
            live_version=live_version,
            challenger_version=self.policy.version,
            live_decision=live_decision,
            challenger_decision=challenger,
        )
        return {
            "shadow_id": shadow_id,
            "live": live_decision,
            "challenger": challenger,
            "different": live_decision != challenger,
        }

    def readiness(self) -> dict[str, Any]:
        row = self.warehouse.conn.execute(
            "SELECT COUNT(*) AS sample,MIN(observed_at) AS first_at,MAX(observed_at) AS last_at "
            "FROM shadow_decisions WHERE challenger_version=?",
            (self.policy.version,),
        ).fetchone()
        sample = int(row["sample"])
        return {
            "version": self.policy.version,
            "sample": sample,
            "first_at": row["first_at"],
            "last_at": row["last_at"],
            "minimum_sample": self.policy.minimum_prospective_decisions,
            "minimum_live_days": self.policy.minimum_live_days,
            "ready": False,
            "state": "PROSPECTIVE_EVIDENCE_REQUIRED"
            if sample < self.policy.minimum_prospective_decisions
            else "MANUAL_REVIEW_REQUIRED",
            "public_alerts": False,
        }

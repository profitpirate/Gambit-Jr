from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol


class MarketRegime(StrEnum):
    HOT = "HOT"
    NORMAL = "NORMAL"
    COLD = "COLD"


def rank_alpha_wallet(history: list[dict[str, Any]]) -> dict[str, Any]:
    matured = [row for row in history if row.get("matured") is True]
    if not matured:
        return {"score": None, "grade": "UNKNOWN", "sample": 0}
    right_tail = sum(float(row.get("peak_multiple") or 0) >= 5 for row in matured) / len(matured)
    rug_rate = sum(bool(row.get("rug_before_2x")) for row in matured) / len(matured)
    early = sum(float(row.get("entry_age_minutes") or 10_000) <= 10 for row in matured) / len(matured)
    recency = sum(float(row.get("days_ago") or 10_000) <= 30 for row in matured) / len(matured)
    score = max(0.0, min(100.0, 50 * right_tail + 25 * early + 15 * recency - 40 * rug_rate))
    # Small samples remain ranked but cannot be called proven.
    grade = "PROVEN" if score >= 70 and len(matured) >= 20 else "PROMISING" if score >= 55 else "UNPROVEN"
    return {"score": round(score, 2), "grade": grade, "sample": len(matured), "rug_rate": rug_rate}


def operator_relationship(evidence: dict[str, Any]) -> str:
    if evidence.get("direct_transfer"):
        return "LINKED"
    if evidence.get("common_funder"):
        return "COMMON_FUNDER"
    if evidence.get("repeated_deployment_pattern"):
        return "REPEATED_DEPLOYMENT_PATTERN"
    return "ASSOCIATED" if evidence else "UNKNOWN"


def classify_regime(observations: list[dict[str, Any]]) -> MarketRegime:
    if len(observations) < 5:
        return MarketRegime.NORMAL
    runner_rate = sum(float(row.get("peak_multiple") or 0) >= 2 for row in observations) / len(observations)
    failure_rate = sum(bool(row.get("failed_before_2x")) for row in observations) / len(observations)
    if runner_rate >= 0.35 and failure_rate <= 0.4:
        return MarketRegime.HOT
    if runner_rate <= 0.12 or failure_rate >= 0.7:
        return MarketRegime.COLD
    return MarketRegime.NORMAL


def missed_runner_attribution(row: dict[str, Any]) -> str:
    if row.get("not_discovered"):
        return "DISCOVERY_MISS"
    if row.get("provider_outage"):
        return "PROVIDER_MISS"
    if row.get("entry_status") in {"CHASING", "CLOSED"}:
        return "LATE_ENTRY"
    if row.get("evidence_coverage", 100) < 60:
        return "EVIDENCE_GAP"
    if row.get("failure_score", 0) >= 50:
        return "SAFETY_POLICY_FILTER"
    return "MODEL_FALSE_NEGATIVE"


def outcome_class(peak_multiple: float | None, rugged: bool, matured: bool = True) -> str:
    if not matured:
        return "ACTIVE_UNMATURED"
    peak = float(peak_multiple or 0)
    if rugged:
        return "RUG_AFTER_RUNNER" if peak >= 2 else "RUG_BEFORE_2X"
    if peak >= 2:
        return "SURVIVED_RUNNER"
    return "SURVIVED_NO_RUN" if peak > 0 else "FAILED_BEFORE_2X"


def post_call_risk(current: dict[str, Any], prior: dict[str, Any]) -> dict[str, Any]:
    reasons = []
    if (
        current.get("liquidity_usd") is not None
        and prior.get("liquidity_usd")
        and current["liquidity_usd"] / prior["liquidity_usd"] <= 0.6
    ):
        reasons.append("LIQUIDITY_REMOVAL")
    if current.get("buyer_replacement") == "BUYER_COLLAPSE":
        reasons.append("BUYER_COLLAPSE")
    if current.get("creator_selling") is True:
        reasons.append("CREATOR_SELLING")
    if (
        current.get("concentration_percent") is not None
        and prior.get("concentration_percent") is not None
        and current["concentration_percent"] - prior["concentration_percent"] >= 15
    ):
        reasons.append("CONCENTRATION_DETERIORATION")
    return {"state": "EXIT_RISK" if reasons else "RUNNER_CONTINUES", "reasons": reasons}


class SocialCatalystProvider(Protocol):
    async def evidence(self, chain: str, token_address: str) -> dict[str, Any]: ...


@dataclass(slots=True)
class UnknownSocialCatalystProvider:
    name: str = "unconfigured_social_catalyst"

    async def evidence(self, chain: str, token_address: str) -> dict[str, Any]:
        return {
            "chain": chain,
            "token_address": token_address,
            "state": "UNKNOWN",
            "velocity": None,
            "catalyst": None,
            "provider": self.name,
            "retrieved_at": datetime.now(UTC).isoformat(),
        }


def social_state(metadata_match: bool, velocity: float | None, catalyst_confirmed: bool = False) -> str:
    if catalyst_confirmed:
        return "CATALYST_CONFIRMED"
    if velocity is not None and velocity > 0:
        return "SOCIAL_ACCELERATING"
    # Static metadata is narrative identity, never social velocity.
    return "NO_VERIFIED_VELOCITY" if metadata_match else "UNKNOWN"


class ColdArchive:
    """Hot-path-free history contract; Parquet materialization is an offline operation."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def partition_path(self, chain: str, observed_at: str) -> Path:
        stamp = datetime.fromisoformat(observed_at)
        return self.root / f"chain={chain}" / f"date={stamp.date().isoformat()}"

    def assert_outside_hot_database(self, database_path: str | Path) -> None:
        archive = self.root.resolve()
        database = Path(database_path).resolve()
        if archive == database.parent or database in archive.parents:
            raise ValueError("cold archive must not share the hot SQLite database location")

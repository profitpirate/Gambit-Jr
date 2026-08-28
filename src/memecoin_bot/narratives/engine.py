from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from memecoin_bot.models import DiscoveryEvent, MarketSnapshot

CATEGORIES: dict[str, set[str]] = {
    "AI": {"ai", "agent", "robot", "gpt", "neural", "人工智能", "未来ai", "에이아이"},
    "SOLANA": {"solana", "sol", "pump", "bonk"},
    "ANIMAL": {
        "dog",
        "cat",
        "frog",
        "penguin",
        "monkey",
        "goat",
        "猫",
        "猫猫",
        "ねこ",
        "고양이",
        "柴犬",
    },
    "GAMING": {"game", "gaming", "play"},
    "POLITICS": {"president", "election", "senate", "politics"},
    "SPORTS": {"football", "soccer", "basketball", "ufc", "sport"},
    "ASIA": {
        "china",
        "chinese",
        "korea",
        "korean",
        "japan",
        "japanese",
        "中国",
        "韩国",
        "日本",
        "한국",
    },
}


@dataclass(frozen=True, slots=True)
class NarrativeObservation:
    narrative_id: str
    observed_at: str
    available_at: str
    capital_flow: float | None
    independent_tokens: int | None
    leader_token: str | None = None
    catalyst_id: str | None = None


class NarrativeEngine:
    def assess(self, discovery: DiscoveryEvent, market: MarketSnapshot) -> dict[str, Any]:
        description = str(discovery.metadata.get("description") or "")
        text = " ".join(x for x in [market.name, market.symbol, description] if x).lower()
        words = set(re.findall(r"[^\W_]+", text, flags=re.UNICODE))
        matches = [
            name
            for name, terms in CATEGORIES.items()
            if words & terms
            or any(term in text for term in terms if any(ord(c) > 127 for c in term))
        ]
        if not matches:
            return {
                "score": None,
                "label": None,
                "source": discovery.source,
                "reason": "NO_VERIFIABLE_CATALYST",
            }
        # Token metadata establishes concept fit, not that the narrative is genuinely trending.
        return {
            "score": 6.0,
            "label": matches[0],
            "source": discovery.source,
            "fit_evidence": sorted(
                (words & CATEGORIES[matches[0]])
                | {
                    term
                    for term in CATEGORIES[matches[0]]
                    if any(ord(c) > 127 for c in term) and term in text
                }
            ),
            "freshness": None,
            "acceleration": None,
            "limitation": "TOKEN_METADATA_FIT_ONLY_NO_EXTERNAL_CATALYST_VELOCITY",
        }

    def assess_history(
        self,
        observations: list[NarrativeObservation],
        decision_timestamp: str,
    ) -> dict[str, Any]:
        decision = _time(decision_timestamp)
        ordered = sorted(observations, key=lambda row: _time(row.observed_at))
        for row in ordered:
            observed = _time(row.observed_at)
            available = _time(row.available_at)
            if available < observed or available > decision:
                raise ValueError("narrative evidence violates point-in-time availability")
        if len(ordered) < 2:
            return {"score": None, "reason": "INSUFFICIENT_PIT_NARRATIVE_HISTORY"}
        first, last = ordered[0], ordered[-1]
        elapsed = max(1.0, (_time(last.observed_at) - _time(first.observed_at)).total_seconds())
        velocity = (
            (last.capital_flow - first.capital_flow) / elapsed
            if last.capital_flow is not None and first.capital_flow is not None
            else None
        )
        saturation = last.independent_tokens
        return {
            "score": None,
            "reason": "DYNAMIC_COMPONENTS_REQUIRE_OOS_VALIDATION",
            "narrative_id": last.narrative_id,
            "narrative_birth": first.observed_at,
            "narrative_velocity": velocity,
            "narrative_acceleration": None,
            "narrative_leader": last.leader_token,
            "copycat_distance": None,
            "saturation": saturation,
            "capital_fragmentation": None,
            "revival": first.capital_flow == 0 and (last.capital_flow or 0) > 0,
            "catalyst_alignment": last.catalyst_id,
            "decay": velocity is not None and velocity < 0,
        }


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include timezone: {value}")
    return parsed.astimezone(UTC)

from __future__ import annotations

import re
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

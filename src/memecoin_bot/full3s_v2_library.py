"""Causal, read-only developer-history signal for FULL_3S_V2_LIBRARY.

This is an isolated research competitor. It does not modify the frozen FULL_3S,
FULL_3S_V2, 4s, 7s, or 2s-loss-exit models and contains no execution code.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MODEL = "FULL_3S_V2_LIBRARY"


class LibraryIndex:
    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.available_ns = int(data.get("snapshot_available_ns") or 0)
        self.records_by_creator: dict[str, list[dict[str, Any]]] = {}
        for row in data.get("records", []):
            creator = row.get("creator")
            if isinstance(creator, str):
                self.records_by_creator.setdefault(creator, []).append(row)

    @classmethod
    def from_path(cls, path: str | Path = "models/e4/library/e4-hg-trade-library.json") -> "LibraryIndex":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def profile(self, creator: str, decision_ns: int) -> dict[str, Any]:
        """Return only information causally available at ``decision_ns``.

        The frozen E4 corpus has no reliable per-trade observation timestamps in
        the canonical creator file. Therefore it may be used only for decisions
        made after this library snapshot became available. This explicitly blocks
        retroactive tests from leaking future E4 outcome labels.
        """
        if not isinstance(decision_ns, int) or decision_ns <= 0:
            raise ValueError("decision_ns must be a positive integer")
        if self.available_ns and decision_ns <= self.available_ns:
            return {"model": MODEL, "creator": creator, "causal": False,
                    "classification": "NOT_CAUSAL_FOR_RETROACTIVE_DECISION",
                    "wins": 0, "losses": 0, "rejections": 0, "resolved_trades": 0}
        eligible = []
        for row in self.records_by_creator.get(creator, []):
            if row.get("source") == "E4":
                eligible.append(row); continue
            observed = row.get("observed_ns")
            if isinstance(observed, int) and observed < decision_ns:
                eligible.append(row)
        wins = sum(r.get("record_type") == "TRADE" and r.get("outcome") == "WIN" for r in eligible)
        losses = sum(r.get("record_type") == "TRADE" and r.get("outcome") == "LOSS" for r in eligible)
        rejections = sum(r.get("record_type") == "REJECTION" for r in eligible)
        if wins >= 2 and losses == 0: classification = "GOLDEN_BUNCH"
        elif losses >= 2 and wins == 0: classification = "NEGATIVE_REPEAT"
        elif wins + losses == 0: classification = "NO_RESOLVED_HISTORY"
        else: classification = "MIXED_HISTORY"
        return {"model": MODEL, "creator": creator, "causal": True, "classification": classification,
                "wins": wins, "losses": losses, "rejections": rejections,
                "resolved_trades": wins + losses, "record_ids": [r.get("record_id") for r in eligible]}


class Full3SV2LibraryPolicy:
    """Minimal isolated competitor: baseline admission plus repeat-loser veto.

    This does not claim performance. The policy is ready for its own forward-paper
    A/B, but is intentionally not wired into the current live campaigns.
    """
    def __init__(self, index: LibraryIndex):
        self.index = index

    def evaluate(self, *, baseline_accept: bool, creator: str, decision_ns: int) -> dict[str, Any]:
        profile = self.index.profile(creator, decision_ns)
        if not profile["causal"]:
            return {"model": MODEL, "accept": False, "reason": profile["classification"], "profile": profile}
        veto = profile["classification"] == "NEGATIVE_REPEAT"
        return {"model": MODEL, "accept": bool(baseline_accept and not veto),
                "reason": "LIBRARY_REPEAT_LOSER_VETO" if veto else "BASELINE_PRESERVED",
                "profile": profile}

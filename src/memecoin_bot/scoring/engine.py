from __future__ import annotations

from typing import Any

from memecoin_bot.config import Settings
from memecoin_bot.models import ScoreResult, SignalClass


class ScoringEngine:
    def __init__(self, settings: Settings):
        self.settings = settings

    def score(self, component_inputs: dict[str, float | None], hard_rejections: list[str]) -> ScoreResult:
        maxima = self.settings.weights.copy()
        scores: dict[str, float] = {}
        known_weight = 0.0
        for name, maximum in maxima.items():
            value = component_inputs.get(name)
            if value is None:
                scores[name] = 0.0  # No evidence points; the source metric remains None/UNKNOWN.
                continue
            known_weight += maximum
            scores[name] = round(max(0.0, min(float(value), maximum)), 2)
        total = round(sum(scores.values()), 2)
        confidence = round(known_weight / sum(maxima.values()), 4)
        normalized = round(total / known_weight * 100, 2) if known_weight > 0 else None
        if hard_rejections:
            classification = SignalClass.REJECT
        elif confidence < self.settings.min_confidence_for_signal:
            classification = SignalClass.IGNORE
        elif normalized is not None and normalized >= self.settings.high_conviction_threshold:
            classification = SignalClass.HIGH_CONVICTION
        elif normalized is not None and normalized >= self.settings.strong_threshold:
            classification = SignalClass.STRONG
        elif normalized is not None and normalized >= self.settings.watch_threshold:
            classification = SignalClass.WATCH
        else:
            classification = SignalClass.IGNORE
        return ScoreResult(
            total=total,
            component_scores=scores,
            component_maxima=maxima,
            classification=classification,
            confidence=confidence,
            scoring_version=self.settings.scoring_version,
            hard_rejections=hard_rejections,
            normalized_score=normalized,
            available_weight=known_weight,
        )


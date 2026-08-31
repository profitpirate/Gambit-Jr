from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any, Mapping

from .models import CreatorTier, PipelineDecision
from .narrative import ActiveNarrativeCache
from .registry import AtomicCreatorRegistry
from .teacher import E4Teacher

FractionResolver = Callable[[float, str], tuple[str, float]]


def _float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result and abs(result) != float("inf") else default


class PipelineCoordinator:
    """Single O(1) launch decision point for the three intelligence pipelines."""

    def __init__(self, *, registry: AtomicCreatorRegistry, narratives: ActiveNarrativeCache, teacher: E4Teacher, fraction_resolver: FractionResolver) -> None:
        self.registry = registry
        self.narratives = narratives
        self.teacher = teacher
        self.fraction_resolver = fraction_resolver

    def _decision(self, *, accepted: bool, score: float, family: str, reason: str, minimum_tier: str, evidence: Mapping[str, Any], decision_ns: int, max_fraction: float) -> PipelineDecision:
        _tier, fraction = self.fraction_resolver(score, minimum_tier)
        return PipelineDecision(accepted=accepted, score=score if accepted else 0.0, fraction=min(max_fraction, fraction) if accepted else 0.0, family=family, reason=reason, decision_ns=decision_ns, evidence=dict(evidence))

    def evaluate(self, *, state: Any, context: Mapping[str, Any], settings: Any, features: Mapping[str, Any]) -> PipelineDecision:
        started = time.perf_counter_ns()
        now_ns = int(context.get("last_received_ns") or time.time_ns())
        created_received_ns = int(context.get("create_received_ns") or getattr(state, "created_ns", 0) or now_ns)
        age_ms = max(0.0, (now_ns - created_received_ns) / 1_000_000)
        creator = str(getattr(state, "creator", None) or context.get("creator") or "")
        fdv = _float(getattr(state, "fdv_usd", None) or features.get("fdv_usd"))
        creator_buy = _float(features.get("creator_buy_sol"))
        sell_count = _float(features.get("sell_count"))
        sell_sol = _float(features.get("sell_sol"))
        max_age_ms = _float(os.getenv("E4_V10_MAX_ENTRY_AGE_MS", "350"), 350.0)
        max_fdv = min(_float(getattr(settings, "max_entry_fdv_usd", 10_000), 10_000), _float(os.getenv("E4_V10_MAX_ENTRY_FDV_USD", "8500"), 8_500))
        minimum_seed = _float(os.getenv("E4_V10_MIN_CREATOR_BUY_SOL", "0.025"), 0.025)
        base_evidence = {"creator": creator, "age_ms": age_ms, "fdv_usd": fdv, "creator_buy_sol": creator_buy, "sell_count": sell_count, "sell_sol": sell_sol}

        def reject(reason: str, family: str = "rejected") -> PipelineDecision:
            return self._decision(accepted=False, score=0.0, family=family, reason=reason, minimum_tier="probe", evidence=base_evidence, decision_ns=time.perf_counter_ns() - started, max_fraction=settings.max_position_fraction)

        if getattr(state, "complete", False) or getattr(state, "migrated", False) or getattr(state, "wallet_touched", False):
            return reject("not an untouched live Pump curve")
        if not getattr(state, "created_ns", None):
            return reject("creation event not observed")
        if age_ms > max_age_ms:
            return reject("outside E4 V10 launch decision horizon")
        if fdv <= 0 or fdv > max_fdv:
            return reject("outside observed E4 entry FDV")
        if sell_count > 0 or sell_sol > 0:
            return reject("sell appeared before V10 entry")
        if creator_buy < minimum_seed:
            return reject("creator seed not observed")

        profile = self.registry.get(creator)
        prearmed = bool(context.get("prearmed"))
        social_match = self.narratives.match(name=str(context.get("name") or ""), symbol=str(context.get("symbol") or ""), description=str(context.get("description") or ""), now_ns=now_ns)
        public_confirmation = min(1.0, 0.45 * min(1.0, _float(features.get("noncreator_buyers")) / 4.0) + 0.35 * min(1.0, _float(features.get("noncreator_buy_sol")) / 8.0) + 0.20 * min(1.0, max(0.0, _float(features.get("price_multiple"), 1.0) - 1.0) / 0.40))
        base_evidence.update({
            "creator_tier": profile.tier.value if profile else CreatorTier.UNKNOWN.value,
            "creator_confidence": profile.confidence if profile else 0.0,
            "creator_wins": profile.wins if profile else 0,
            "creator_losses": profile.losses if profile else 0,
            "creator_source": profile.source if profile else None,
            "narrative_match": social_match.matched,
            "narrative_score": social_match.score,
            "narrative_key": social_match.key,
            "public_confirmation": public_confirmation,
        })
        if prearmed and age_ms <= _float(os.getenv("E4_V10_PREARMED_MAX_AGE_MS", "80"), 80):
            score = min(0.997, 0.95 + 0.025 * (profile.confidence if profile else 0.0) + 0.015 * social_match.score)
            return self._decision(accepted=True, score=score, family="authorized_prearmed_launch", reason="E4 V10 authorized prearmed launch", minimum_tier="elite", evidence=base_evidence, decision_ns=time.perf_counter_ns() - started, max_fraction=settings.max_position_fraction)
        if profile and profile.is_negative:
            return reject(f"E4 V10 negative creator history {profile.wins}W/{profile.losses}L", "negative_creator")
        if profile and profile.tier == CreatorTier.ELITE and age_ms <= 105:
            score = min(0.995, max(0.955, profile.confidence))
            return self._decision(accepted=True, score=score, family="elite_creator_sniper", reason=f"E4 V10 elite creator {profile.wins}W/{profile.losses}L", minimum_tier="elite", evidence=base_evidence, decision_ns=time.perf_counter_ns() - started, max_fraction=settings.max_position_fraction)
        if profile and profile.tier == CreatorTier.APPROVED and age_ms <= 120:
            proven = profile.trades >= 3 and profile.gross_win_rate >= 0.75
            score = min(0.982, max(0.84, profile.confidence + 0.015 * public_confirmation + 0.01 * social_match.score))
            return self._decision(accepted=True, score=score, family="approved_creator_sniper", reason=f"E4 V10 approved creator {profile.wins}W/{profile.losses}L source={profile.source}", minimum_tier="high" if proven else "standard", evidence=base_evidence, decision_ns=time.perf_counter_ns() - started, max_fraction=settings.max_position_fraction)
        if social_match.matched and age_ms <= 140:
            score = min(0.97, 0.78 + 0.14 * social_match.score + 0.025 * public_confirmation + 0.015 * (profile.confidence if profile else 0.0))
            return self._decision(accepted=True, score=score, family="prelaunch_social_narrative", reason=f"E4 V10 prelaunch narrative match {social_match.key}", minimum_tier="strong", evidence={**base_evidence, **dict(social_match.evidence)}, decision_ns=time.perf_counter_ns() - started, max_fraction=settings.max_position_fraction)
        copy_signal = self.teacher.copy_signal(str(getattr(state, "mint", "")), now_ns=now_ns, current_price_sol=getattr(state, "price_sol", None), max_age_ms=_float(os.getenv("E4_V10_COPY_MAX_AGE_MS", "90"), 90), max_drift_bps=_float(os.getenv("E4_V10_COPY_MAX_DRIFT_BPS", "350"), 350))
        if copy_signal is not None and age_ms <= 250:
            score = 0.93 if profile is None else min(0.985, 0.93 + 0.05 * profile.confidence)
            return self._decision(accepted=True, score=score, family="e4_confirmed_copy", reason="E4 V10 fresh E4 buy confirmation", minimum_tier="strong" if profile is None else "high", evidence={**base_evidence, "e4_signal_age_ms": max(0.0, (now_ns - copy_signal.observed_ns) / 1_000_000), "e4_entry_price_sol": copy_signal.e4_entry_price_sol, "e4_entry_sol": copy_signal.e4_entry_sol, "e4_signature": copy_signal.signature}, decision_ns=time.perf_counter_ns() - started, max_fraction=settings.max_position_fraction)
        return reject("E4 V10 three-pipeline gate: no approved creator, prelaunch narrative, or fresh E4 confirmation")

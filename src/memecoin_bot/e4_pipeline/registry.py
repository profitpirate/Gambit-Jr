from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .models import CreatorProfile, CreatorTier

LOGGER = logging.getLogger("gambit.e4.pipeline.registry")


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number and abs(number) != float("inf") else default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOGGER.exception("Unable to load creator registry path=%s", path)
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _tier_from_history(wins: int, losses: int, pnl: float) -> CreatorTier:
    trades = wins + losses
    rate = wins / trades if trades else 0.0
    if trades >= 3 and rate <= 0.25:
        return CreatorTier.NEGATIVE
    if losses >= 2 and wins == 0:
        return CreatorTier.NEGATIVE
    if trades >= 5 and rate >= 0.80 and pnl > 0:
        return CreatorTier.ELITE
    if trades >= 3 and rate >= 0.70 and wins >= 2:
        return CreatorTier.APPROVED
    if wins >= 2 and losses == 0:
        return CreatorTier.APPROVED
    if wins >= 1 and rate >= 0.50:
        return CreatorTier.APPROVED
    if trades:
        return CreatorTier.WATCH
    return CreatorTier.UNKNOWN


def _confidence(tier: CreatorTier, wins: int, losses: int, evidence_score: float = 0.0) -> float:
    trades = wins + losses
    rate = wins / trades if trades else 0.0
    if tier == CreatorTier.NEGATIVE:
        return 0.0
    if tier == CreatorTier.ELITE:
        return min(0.995, 0.93 + 0.035 * rate + 0.005 * min(6, wins))
    if tier == CreatorTier.APPROVED:
        base = 0.82 if trades <= 1 else 0.86
        return min(0.975, max(base, 0.80 + 0.12 * rate + 0.008 * min(6, wins), evidence_score))
    if tier == CreatorTier.WATCH:
        return min(0.79, max(0.55, evidence_score))
    return max(0.0, min(0.50, evidence_score))


def _profile_from_expectancy(record: Mapping[str, Any]) -> CreatorProfile | None:
    wallet = str(record.get("creator") or "").strip()
    if not wallet:
        return None
    wins = max(0, _integer(record.get("wins", record.get("e4_observed_wins"))))
    losses = max(0, _integer(record.get("losses")))
    trades = max(wins + losses, _integer(record.get("trades")))
    if trades > wins + losses:
        losses = max(losses, trades - wins)
    pnl = _finite(record.get("gross_pnl_sol", record.get("winning_pnl_sol")))
    tier = _tier_from_history(wins, losses, pnl)
    rate = wins / (wins + losses) if wins + losses else _finite(record.get("gross_win_rate"))
    typical = record.get("median_e4_entry_sol")
    maximum = record.get("max_e4_entry_sol")
    return CreatorProfile(
        wallet=wallet,
        tier=tier,
        source="E4_EXPECTANCY",
        wins=wins,
        losses=losses,
        trades=wins + losses,
        gross_win_rate=rate,
        gross_pnl_sol=pnl,
        confidence=_confidence(tier, wins, losses),
        typical_entry_sol=_finite(typical) if typical is not None else None,
        max_entry_sol=_finite(maximum) if maximum is not None else None,
        social_handles=tuple(
            str(value).lower().lstrip("@")
            for value in record.get("social_handles") or ()
            if str(value).strip()
        ),
        evidence=dict(record),
    )


def _profile_from_discovery(wallet: str, record: Mapping[str, Any], watch: bool = False) -> CreatorProfile:
    score = max(0.0, min(1.0, _finite(record.get("score"))))
    outcome = str(record.get("current_e4_outcome") or "").upper()
    wins = 1 if outcome == "WIN" else 0
    losses = 1 if outcome == "LOSS" else 0
    tier = CreatorTier.WATCH if watch else (
        CreatorTier.APPROVED if score >= 0.84 else CreatorTier.WATCH
    )
    runner_values = record.get("observed_prior_multiples") or record.get("observed_prior_max_mc_usd") or ()
    runner_count = sum(
        1
        for value in runner_values
        if _finite(value) >= (2.0 if "multiples" in str(record) else 20_000.0)
    )
    return CreatorProfile(
        wallet=wallet,
        tier=tier,
        source="EXTERNAL_RUNNER_HISTORY",
        wins=wins,
        losses=losses,
        trades=wins + losses,
        gross_win_rate=wins / (wins + losses) if wins + losses else 0.0,
        gross_pnl_sol=_finite(record.get("current_e4_gross_pnl_sol")),
        confidence=_confidence(tier, wins, losses, score),
        runner_count=runner_count,
        launch_count=max(runner_count, len(runner_values)),
        social_handles=tuple(
            str(value).lower().lstrip("@")
            for value in record.get("social_handles") or ()
            if str(value).strip()
        ),
        evidence=dict(record),
    )


def _merge_profiles(current: CreatorProfile, incoming: CreatorProfile) -> CreatorProfile:
    wins = max(current.wins, incoming.wins)
    losses = max(current.losses, incoming.losses)
    pnl = current.gross_pnl_sol if current.source == "E4_EXPECTANCY" else incoming.gross_pnl_sol
    if incoming.source == "E4_EXPECTANCY":
        pnl = incoming.gross_pnl_sol
    tier = _tier_from_history(wins, losses, pnl)
    if tier not in {CreatorTier.NEGATIVE, CreatorTier.ELITE}:
        if CreatorTier.APPROVED in {current.tier, incoming.tier}:
            tier = CreatorTier.APPROVED
        elif CreatorTier.WATCH in {current.tier, incoming.tier}:
            tier = CreatorTier.WATCH
    handles = tuple(dict.fromkeys((*current.social_handles, *incoming.social_handles)))
    evidence = {"sources": [current.source, incoming.source], **dict(current.evidence)}
    evidence.update({f"external_{key}": value for key, value in incoming.evidence.items()})
    trades = wins + losses
    return CreatorProfile(
        wallet=current.wallet,
        tier=tier,
        source="E4_EXPECTANCY+EXTERNAL" if "E4_EXPECTANCY" in {current.source, incoming.source} else incoming.source,
        wins=wins,
        losses=losses,
        trades=trades,
        gross_win_rate=wins / trades if trades else 0.0,
        gross_pnl_sol=pnl,
        confidence=max(
            _confidence(tier, wins, losses),
            current.confidence,
            incoming.confidence if tier != CreatorTier.NEGATIVE else 0.0,
        ) if tier != CreatorTier.NEGATIVE else 0.0,
        typical_entry_sol=current.typical_entry_sol or incoming.typical_entry_sol,
        max_entry_sol=max(
            value for value in (current.max_entry_sol, incoming.max_entry_sol) if value is not None
        ) if current.max_entry_sol is not None or incoming.max_entry_sol is not None else None,
        runner_count=max(current.runner_count, incoming.runner_count),
        launch_count=max(current.launch_count, incoming.launch_count),
        social_handles=handles,
        evidence=evidence,
    )


class AtomicCreatorRegistry:
    """Lock-free reads over an atomically replaced creator snapshot."""

    def __init__(self, expectancy_path: Path, discovered_path: Path) -> None:
        self.expectancy_path = expectancy_path
        self.discovered_path = discovered_path
        self._write_lock = threading.Lock()
        self._snapshot: Mapping[str, CreatorProfile] = MappingProxyType({})
        self.reload()

    def reload(self) -> None:
        expectancy = _read_json(self.expectancy_path)
        discovered = _read_json(self.discovered_path)
        profiles: dict[str, CreatorProfile] = {}
        direct = expectancy.get("creators")
        records: Iterable[Mapping[str, Any]]
        if isinstance(direct, Mapping):
            records = (
                {"creator": creator, **dict(record)}
                for creator, record in direct.items()
                if isinstance(record, Mapping)
            )
        else:
            records = (
                record
                for record in expectancy.get("top_creators") or ()
                if isinstance(record, Mapping)
            )
        for record in records:
            profile = _profile_from_expectancy(record)
            if profile:
                profiles[profile.wallet] = profile
        for key, watch in (("creators", False), ("watchlist", True)):
            values = discovered.get(key)
            if not isinstance(values, Mapping):
                continue
            for wallet, record in values.items():
                if not isinstance(record, Mapping):
                    continue
                incoming = _profile_from_discovery(str(wallet), record, watch)
                current = profiles.get(str(wallet))
                profiles[str(wallet)] = _merge_profiles(current, incoming) if current else incoming
        self._snapshot = MappingProxyType(profiles)

    def get(self, wallet: str | None) -> CreatorProfile | None:
        if not wallet:
            return None
        return self._snapshot.get(str(wallet))

    def classify(self, wallet: str | None) -> CreatorTier:
        profile = self.get(wallet)
        return profile.tier if profile else CreatorTier.UNKNOWN

    def snapshot(self) -> Mapping[str, CreatorProfile]:
        return self._snapshot

    def record_outcome(self, wallet: str, *, won: bool, gross_pnl_sol: float, source: str = "E4_LIVE_TEACHER") -> CreatorProfile:
        if not wallet:
            raise ValueError("creator wallet is required")
        with self._write_lock:
            current = self._snapshot.get(wallet)
            wins = (current.wins if current else 0) + int(won)
            losses = (current.losses if current else 0) + int(not won)
            pnl = (current.gross_pnl_sol if current else 0.0) + gross_pnl_sol
            tier = _tier_from_history(wins, losses, pnl)
            trades = wins + losses
            updated = CreatorProfile(
                wallet=wallet,
                tier=tier,
                source=source,
                wins=wins,
                losses=losses,
                trades=trades,
                gross_win_rate=wins / trades,
                gross_pnl_sol=pnl,
                confidence=_confidence(tier, wins, losses),
                typical_entry_sol=current.typical_entry_sol if current else None,
                max_entry_sol=current.max_entry_sol if current else None,
                runner_count=current.runner_count if current else 0,
                launch_count=current.launch_count if current else 0,
                social_handles=current.social_handles if current else (),
                evidence={
                    **(dict(current.evidence) if current else {}),
                    "last_live_teacher_outcome": "WIN" if won else "LOSS",
                },
            )
            replacement = dict(self._snapshot)
            replacement[wallet] = updated
            self._snapshot = MappingProxyType(replacement)
            return updated

    def apply_scanner_profile(self, wallet: str, *, launch_count: int, runner_count: int, score: float, social_handles: Iterable[str] = (), evidence: Mapping[str, Any] | None = None) -> CreatorProfile:
        with self._write_lock:
            current = self._snapshot.get(wallet)
            ratio = runner_count / launch_count if launch_count else 0.0
            scanner_tier = CreatorTier.APPROVED if launch_count >= 3 and runner_count >= 2 and ratio >= 0.50 and score >= 0.80 else CreatorTier.WATCH
            incoming = CreatorProfile(
                wallet=wallet,
                tier=scanner_tier,
                source="BACKGROUND_HISTORY_SCANNER",
                confidence=_confidence(scanner_tier, 0, 0, score),
                runner_count=max(0, runner_count),
                launch_count=max(0, launch_count),
                social_handles=tuple(dict.fromkeys(str(value).lower().lstrip("@") for value in social_handles if value)),
                evidence=dict(evidence or {}),
            )
            updated = _merge_profiles(current, incoming) if current else incoming
            replacement = dict(self._snapshot)
            replacement[wallet] = updated
            self._snapshot = MappingProxyType(replacement)
            return updated

    def counts(self) -> dict[str, int]:
        result = {tier.value: 0 for tier in CreatorTier}
        for profile in self._snapshot.values():
            result[profile.tier.value] += 1
        result["TOTAL"] = len(self._snapshot)
        return result

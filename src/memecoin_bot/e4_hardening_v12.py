from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any, Mapping

from . import e4_hardening_v10 as v10

core = v10.core
final = v10.final
v6 = v10.v6
v8 = v10.v8
v9 = v10.v9
PIPELINES = v10.PIPELINES

_CONFIG_PATH = Path(os.getenv("E4_V12_SELECTION_PATH", "models/e4/e4-v12-selection.json"))


def _load_config() -> dict[str, Any]:
    try:
        payload = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


_CFG = _load_config()


def _section(name: str) -> dict[str, Any]:
    value = _CFG.get(name)
    return dict(value) if isinstance(value, Mapping) else {}


def _float(section: str, key: str, default: float) -> float:
    try:
        value = float(_section(section).get(key, default))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _int(section: str, key: str, default: int) -> int:
    try:
        return int(_section(section).get(key, default))
    except (TypeError, ValueError):
        return default


def _creator_history(creator: str) -> tuple[int, int, int, float, Any | None]:
    profile = PIPELINES.creators.lookup(creator) if creator else None
    if profile is not None:
        wins = int(profile.wins or 0)
        losses = int(profile.losses or 0)
        trades = max(int(profile.trades or 0), wins + losses)
        rate = float(profile.gross_win_rate or (wins / trades if trades else 0.0))
        return wins, losses, trades, min(1.0, max(0.0, rate)), profile
    record = v9._EXPECTANCY_CREATORS.get(creator, {}) if creator else {}
    if isinstance(record, Mapping):
        wins, losses, trades, rate = v9._history_numbers(record)
        return wins, losses, trades, rate, None
    return 0, 0, 0, 0.0, None


def _fingerprint_match(profile: Any | None, context: Mapping[str, Any]) -> tuple[bool, bool, bool]:
    if profile is None:
        return False, False, False
    host = str(context.get("metadata_host") or "").lower()
    handle = str(context.get("social_handle") or "").lower().lstrip("@")
    known_hosts = {str(item).lower() for item in getattr(profile, "common_metadata_hosts", ()) if str(item)}
    known_handles = {str(item).lower().lstrip("@") for item in getattr(profile, "common_social_handles", ()) if str(item)}
    host_match = bool(host and host in known_hosts)
    handle_match = bool(handle and handle in known_handles)
    return host_match or handle_match, host_match, handle_match


def _make_profile(
    self: core.E4Policy,
    state: core.TokenState,
    features: dict[str, float],
    *,
    family: str,
    score: float,
    fraction: float,
    reason: str,
) -> tuple[bool, float, float, str, dict[str, float]]:
    fraction = min(max(0.0, float(fraction)), self.settings.max_position_fraction)
    tier, ladder = v6.relative_fraction_for_score(score)
    fraction = min(fraction or ladder, self.settings.max_position_fraction)
    for candidate in v6._TIER_ORDER:
        if abs(v6._TIER_FRACTIONS[candidate] - fraction) < 1e-12:
            tier = candidate
            break
    profile = v6.EntryProfile(
        family=family,
        tier=tier,
        fraction=fraction,
        score=float(score),
        first_partial_fraction=v6._profile_partial(tier),
        features=dict(features),
    )
    v6._PROFILE_BY_MINT[state.mint] = profile
    features.update(
        {
            "e4_v12_score": profile.score,
            "e4_v12_fraction": profile.fraction,
            "e4_v12_tier_index": float(v6._TIER_ORDER.index(tier)),
            "e4_v12_first_partial": profile.first_partial_fraction,
        }
    )
    return True, profile.score, profile.fraction, f"E4_V12 family={family} tier={tier}: {reason}", features


def _entry_v12(self: core.E4Policy, state: core.TokenState) -> tuple[bool, float, float, str, dict[str, float]]:
    started = time.perf_counter_ns()
    if state.complete or state.migrated or state.wallet_touched:
        return False, 0.0, 0.0, "V12 not an untouched live Pump curve", {}
    if state.created_ns is None:
        return False, 0.0, 0.0, "V12 creation event not observed", {}

    features = dict(v8._identity_features(state))
    context = v6._CONTEXT_BY_MINT.setdefault(state.mint, {})
    creator = str(context.get("creator") or getattr(state, "creator", "") or "")
    age_ms = float(features.get("age_ms") or 0.0)
    fdv = float(features.get("fdv_usd") or getattr(state, "fdv_usd", 0.0) or 0.0)
    creator_seed = float(features.get("creator_buy_sol") or 0.0)
    sell_count = int(features.get("sell_count") or 0.0)
    sell_sol = float(features.get("sell_sol") or 0.0)
    noncreator_buyers = int(features.get("noncreator_buyers") or 0.0)
    noncreator_sol = float(features.get("noncreator_buy_sol") or 0.0)
    max_fdv = min(self.settings.max_entry_fdv_usd, _float("limits", "max_entry_fdv_usd", 8500.0))

    features["v12_entry_fdv_usd"] = fdv
    features["v12_age_ms"] = age_ms
    if fdv <= 0.0 or fdv > max_fdv:
        return False, 0.0, 0.0, "V12 outside reconstructed E4 FDV envelope", features
    if sell_count > 0 or sell_sol > 0:
        return False, 0.0, 0.0, "V12 sell observed before entry", features

    launch_ns = int(context.get("create_received_ns") or state.created_ns or state.latest_ns or time.time_ns())
    now_ns = int(context.get("last_received_ns") or state.latest_ns or time.time_ns())

    # First preserve explicit deployer authorization. A signed/plain intent is
    # checked using the real creator; creator-tier results from this call are
    # intentionally ignored because V12 never lets tier alone authorize entry.
    delegated = PIPELINES.decide_launch(
        mint=state.mint,
        creator=creator,
        name=str(context.get("name") or ""),
        symbol=str(context.get("symbol") or ""),
        metadata_uri=str(context.get("uri") or ""),
        launch_ns=launch_ns,
        now_ns=now_ns,
        fdv_usd=fdv,
        creator_buy_sol=creator_seed,
        sell_count=sell_count,
        price_sol=float(getattr(state, "price_sol", 0.0) or 0.0),
        e4_confirmed=bool(context.get("e4_confirmed")),
        e4_observed_ns=int(context.get("e4_observed_ns") or 0),
        e4_entry_price=float(context.get("e4_entry_price") or 0.0),
    )
    if bool(context.get("prearmed")) or (delegated.accepted and delegated.family == "authorized_prearmed_launch"):
        if age_ms <= _float("limits", "prearmed_max_age_ms", 80.0):
            features["e4_v12_decision_latency_ns"] = float(time.perf_counter_ns() - started)
            return _make_profile(
                self,
                state,
                features,
                family="authorized_prearmed_launch",
                score=0.97,
                fraction=0.05,
                reason="explicit authenticated/prearmed authority",
            )
        return False, 0.0, 0.0, "V12 prearmed intent arrived outside launch horizon", features

    wins, losses, trades, win_rate, profile = _creator_history(creator)
    features.update(
        {
            "v12_creator_prior_wins": float(wins),
            "v12_creator_prior_losses": float(losses),
            "v12_creator_prior_trades": float(trades),
            "v12_creator_prior_win_rate": float(win_rate),
        }
    )
    if trades >= _int("history", "negative_min_trades", 3) and win_rate <= _float("history", "negative_max_win_rate", 0.25):
        return False, 0.0, 0.0, f"V12 negative creator history {wins}W/{losses}L", features
    if profile is not None and bool(getattr(profile, "negative", False)):
        return False, 0.0, 0.0, "V12 negative creator registry veto", features

    # Evaluate independent social/E4-copy authority with creator deliberately
    # blanked so an approved creator cannot short-circuit the launch-quality gate.
    independent = PIPELINES.decide_launch(
        mint=state.mint,
        creator="",
        name=str(context.get("name") or ""),
        symbol=str(context.get("symbol") or ""),
        metadata_uri=str(context.get("uri") or ""),
        launch_ns=launch_ns,
        now_ns=now_ns,
        fdv_usd=fdv,
        creator_buy_sol=creator_seed,
        sell_count=sell_count,
        price_sol=float(getattr(state, "price_sol", 0.0) or 0.0),
        e4_confirmed=bool(context.get("e4_confirmed")),
        e4_observed_ns=int(context.get("e4_observed_ns") or 0),
        e4_entry_price=float(context.get("e4_entry_price") or 0.0),
    )
    if independent.accepted:
        allowed = False
        if independent.family == "e4_confirmed_fast_copy":
            allowed = True  # PipelineManager already enforces <=100ms, <=8% drift and unsold.
        elif independent.family == "exact_ca_social_launch":
            allowed = age_ms <= 1500.0
        elif independent.family == "preannounced_social_community_launch":
            allowed = age_ms <= _float("limits", "social_max_age_ms", 180.0)
        if allowed:
            features["e4_v12_decision_latency_ns"] = float(time.perf_counter_ns() - started)
            return _make_profile(
                self,
                state,
                features,
                family=independent.family,
                score=float(independent.score),
                fraction=float(independent.fraction),
                reason=independent.reason,
            )

    min_seed = _float("limits", "min_creator_seed_sol", 0.025)
    if creator_seed < min_seed:
        return False, 0.0, 0.0, "V12 creator seed required before creator evaluation", features

    elite = (
        trades >= _int("history", "elite_min_trades", 5)
        and wins >= _int("history", "elite_min_wins", 4)
        and win_rate >= _float("history", "elite_min_win_rate", 0.80)
    )
    proven = (
        trades >= _int("history", "proven_min_trades", 3)
        and wins >= _int("history", "proven_min_wins", 2)
        and win_rate >= _float("history", "proven_min_win_rate", 0.75)
    )
    if not (elite or proven):
        return False, 0.0, 0.0, "V12 creator history permits observation but not autonomous entry", features

    max_age = _float("limits", "elite_max_age_ms", 150.0) if elite else _float("limits", "proven_max_age_ms", 180.0)
    if age_ms > max_age:
        return False, 0.0, 0.0, "V12 creator launch outside early-entry horizon", features

    fingerprint, host_match, handle_match = _fingerprint_match(profile, context)
    elite_public = (
        noncreator_buyers >= _int("launch_support", "elite_public_buyers", 1)
        and noncreator_sol >= _float("launch_support", "elite_public_sol", 0.03)
    )
    proven_public = (
        noncreator_buyers >= _int("launch_support", "proven_public_buyers", 2)
        and noncreator_sol >= _float("launch_support", "proven_public_sol", 0.10)
    )
    big_seed = creator_seed >= (
        _float("launch_support", "elite_big_seed_sol", 0.35)
        if elite
        else _float("launch_support", "proven_big_seed_sol", 0.75)
    )
    launch_support = bool(fingerprint or big_seed or (elite_public if elite else proven_public))
    features.update(
        {
            "v12_fingerprint_match": 1.0 if fingerprint else 0.0,
            "v12_metadata_host_match": 1.0 if host_match else 0.0,
            "v12_social_handle_match": 1.0 if handle_match else 0.0,
            "v12_public_confirmation": 1.0 if (elite_public if elite else proven_public) else 0.0,
            "v12_big_creator_seed": 1.0 if big_seed else 0.0,
        }
    )
    if not launch_support:
        return False, 0.0, 0.0, "V12 good creator / unsupported launch veto", features

    history_strength = min(1.0, 0.75 * win_rate + 0.25 * min(1.0, trades / 5.0))
    fdv_fit = v6._fdv_fit(fdv)
    seed_score = min(1.0, creator_seed / (0.35 if elite else 0.75))
    public_score = min(1.0, 0.55 * (noncreator_buyers / 2.0) + 0.45 * (noncreator_sol / 0.25))
    weights = _section("quality").get("weights") or {}
    w_history = float(weights.get("history", 0.45))
    w_fdv = float(weights.get("fdv_fit", 0.20))
    w_seed = float(weights.get("creator_seed", 0.15))
    w_public = float(weights.get("public_confirmation", 0.15))
    w_fingerprint = float(weights.get("creator_fingerprint", 0.05))
    quality = (
        w_history * history_strength
        + w_fdv * fdv_fit
        + w_seed * seed_score
        + w_public * public_score
        + w_fingerprint * (1.0 if fingerprint else 0.0)
    )
    features.update(
        {
            "v12_history_strength": history_strength,
            "v12_fdv_fit": fdv_fit,
            "v12_seed_score": seed_score,
            "v12_public_score": public_score,
            "v12_launch_quality": quality,
        }
    )
    threshold = _float("quality", "elite_min_score", 0.72) if elite else _float("quality", "proven_min_score", 0.77)
    if quality < threshold:
        return False, 0.0, 0.0, f"V12 good creator / low launch-quality veto score={quality:.3f}", features

    score = min(0.985, 0.78 + 0.18 * quality + 0.02 * win_rate)
    family = "v12_elite_creator_quality_launch" if elite else "v12_proven_creator_quality_launch"
    fraction = 0.03 if elite else 0.0185
    features["e4_v12_decision_latency_ns"] = float(time.perf_counter_ns() - started)
    return _make_profile(
        self,
        state,
        features,
        family=family,
        score=score,
        fraction=fraction,
        reason=f"creator history + launch support + quality={quality:.3f}",
    )


core.E4Policy.entry = _entry_v12

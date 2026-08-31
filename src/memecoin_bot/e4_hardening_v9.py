from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping

from . import e4_hardening_v8 as v8

core = v8.core
final = v8.final
v6 = v8.v6
LOGGER = logging.getLogger("gambit.e4.hardening.v9")


def _read_model() -> dict[str, Any]:
    path = Path(os.getenv("E4_CREATOR_EXPECTANCY_PATH", "models/e4/e4-creator-expectancy.json"))
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOGGER.exception("Could not load creator expectancy model path=%s", path)
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _creator_map(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    # The persisted analysis artifact stores creator records in `top_creators`.
    # Accept a dict as well so future compact model formats remain compatible.
    direct = payload.get("creators")
    if isinstance(direct, Mapping):
        return {
            str(creator): dict(record)
            for creator, record in direct.items()
            if isinstance(record, Mapping)
        }
    output: dict[str, dict[str, Any]] = {}
    for record in payload.get("top_creators") or []:
        if not isinstance(record, Mapping):
            continue
        creator = str(record.get("creator") or "")
        if creator:
            output[creator] = dict(record)
    return output


_EXPECTANCY_MODEL = _read_model()
_EXPECTANCY_CREATORS = _creator_map(_EXPECTANCY_MODEL)


def reload_creator_expectancy() -> None:
    global _EXPECTANCY_MODEL, _EXPECTANCY_CREATORS
    _EXPECTANCY_MODEL = _read_model()
    _EXPECTANCY_CREATORS = _creator_map(_EXPECTANCY_MODEL)


def _history(state: core.TokenState) -> tuple[str, dict[str, Any]]:
    context = v6._CONTEXT_BY_MINT.get(state.mint, {})
    creator = state.creator or str(context.get("creator") or "")
    return creator, _EXPECTANCY_CREATORS.get(creator, {})


def _history_numbers(record: Mapping[str, Any]) -> tuple[int, int, int, float]:
    wins = max(0, int(record.get("wins") or record.get("e4_observed_wins") or 0))
    losses = max(0, int(record.get("losses") or 0))
    trades = max(wins + losses, int(record.get("trades") or 0))
    rate = float(record.get("gross_win_rate") or (wins / trades if trades else 0.0))
    return wins, losses, trades, min(1.0, max(0.0, rate))


def _make_profile(
    self: core.E4Policy,
    state: core.TokenState,
    features: dict[str, float],
    family: str,
    score: float,
    minimum_tier: str,
) -> tuple[bool, float, float, str, dict[str, float]]:
    tier, fraction = v6.relative_fraction_for_score(score, minimum_tier)
    fraction = min(fraction, self.settings.max_position_fraction)
    profile = v6.EntryProfile(
        family=family,
        tier=tier,
        fraction=fraction,
        score=score,
        first_partial_fraction=v6._profile_partial(tier),
        features=dict(features),
    )
    v6._PROFILE_BY_MINT[state.mint] = profile
    features.update(
        {
            "e4_v9_score": score,
            "e4_v9_fraction": fraction,
            "e4_v9_tier_index": float(v6._TIER_ORDER.index(tier)),
            "e4_v9_first_partial": profile.first_partial_fraction,
        }
    )
    return True, score, fraction, f"E4_V9 family={family} tier={tier}", features


def _entry_v9(
    self: core.E4Policy,
    state: core.TokenState,
) -> tuple[bool, float, float, str, dict[str, float]]:
    if state.complete or state.migrated or state.wallet_touched:
        return False, 0.0, 0.0, "not an untouched live Pump curve", {}
    if state.created_ns is None:
        return False, 0.0, 0.0, "creation event not observed", {}

    features = dict(v8._identity_features(state))
    creator, record = _history(state)
    wins, losses, trades, win_rate = _history_numbers(record)
    features.update(
        {
            "creator_prior_wins": float(wins),
            "creator_prior_losses": float(losses),
            "creator_prior_trades": float(trades),
            "creator_prior_gross_win_rate": win_rate,
        }
    )

    max_age_ms = float(os.getenv("E4_V9_MAX_ENTRY_AGE_MS", "350"))
    max_fdv = min(self.settings.max_entry_fdv_usd, float(os.getenv("E4_V9_MAX_ENTRY_FDV_USD", "8500")))
    if features["age_ms"] > max_age_ms:
        return False, 0.0, 0.0, "outside E4 V9 launch decision horizon", features
    if features["fdv_usd"] <= 0 or features["fdv_usd"] > max_fdv:
        return False, 0.0, 0.0, "outside observed E4 entry FDV", features
    if features["sell_count"] > 0 or features["sell_sol"] > 0:
        return False, 0.0, 0.0, "sell appeared before E4 V9 confirmation", features
    if features["creator_buy_sol"] < float(os.getenv("E4_V9_MIN_CREATOR_BUY_SOL", "0.025")):
        return False, 0.0, 0.0, "creator seed not observed", features

    # Historical negative evidence is allowed to veto every non-authorized path.
    # The 316-position reconstruction contains a repeat creator at 0W/4L; V8's
    # old winner-membership model could not represent that information.
    negative_min_trades = max(2, int(os.getenv("E4_V9_NEGATIVE_MIN_TRADES", "3")))
    negative_rate = float(os.getenv("E4_V9_NEGATIVE_MAX_WIN_RATE", "0.25"))
    negative_creator = trades >= negative_min_trades and win_rate <= negative_rate

    fdv_score = v6._fdv_fit(features["fdv_usd"])
    creator_seed = min(1.0, features["creator_buy_sol"] / 3.0)
    funder = features["funder_score"]
    social = max(features["social_authority_score"], features["community_score"])
    public_confirm = min(
        1.0,
        0.45 * min(1.0, features["noncreator_buyers"] / 4.0)
        + 0.35 * min(1.0, features["noncreator_buy_sol"] / 8.0)
        + 0.20 * min(1.0, max(0.0, features["price_multiple"] - 1.0) / 0.40),
    )

    # Explicit deployer authorization is stronger than historical statistics.
    # This remains distinct from merely observing a J7 metadata hostname.
    if features["prearmed"] >= 1.0 and features["age_ms"] <= 80:
        score = min(0.995, 0.91 + 0.03 * max(funder, social, win_rate) + 0.03 * fdv_score + 0.03 * creator_seed)
        return _make_profile(self, state, features, "authorized_prearmed_launch", score, "elite")

    if negative_creator:
        return (
            False,
            0.0,
            0.0,
            f"E4 V9 negative creator history: {wins}W/{losses}L",
            features,
        )

    # Strongest causal denominator found so far. In the reconstructed chronology,
    # launches from creators with >=3 PRIOR E4 trades and >=75% PRIOR gross WR
    # produced ~84.9% gross WR on the next E4-selected trades. Runtime records
    # are historical priors only; no future outcome is consulted.
    if trades >= 3 and win_rate >= 0.75 and wins >= 2 and features["age_ms"] <= 100:
        score = min(
            0.992,
            0.89
            + 0.055 * win_rate
            + 0.02 * min(1.0, trades / 10.0)
            + 0.015 * fdv_score
            + 0.01 * creator_seed,
        )
        return _make_profile(self, state, features, "proven_repeat_e4_creator", score, "high")

    # Any creator with a prior E4 win is still a meaningful fast path. A causal
    # chronology test yielded ~83.2% gross WR for subsequent E4-selected trades,
    # but mixed histories receive smaller sizing than the proven-repeat tier.
    if wins >= 1 and win_rate >= 0.50 and features["age_ms"] <= 110:
        score = min(
            0.965,
            0.82
            + 0.07 * win_rate
            + 0.025 * min(1.0, wins / 4.0)
            + 0.02 * fdv_score
            + 0.015 * creator_seed,
        )
        minimum = "strong" if trades >= 2 else "standard"
        return _make_profile(self, state, features, "prior_e4_winning_creator", score, minimum)

    # Pre-launch social/community evidence remains a secondary independent path.
    # It is intentionally unavailable to an ordinary post-launch Twitter lookup.
    if features["prelaunch_social"] >= 1.0 and social >= 0.70 and features["age_ms"] <= 120:
        score = min(
            0.955,
            0.72
            + 0.13 * social
            + 0.05 * features["social_follower_score"]
            + 0.03 * fdv_score
            + 0.02 * creator_seed
            + 0.02 * public_confirm,
        )
        return _make_profile(self, state, features, "preannounced_social_community_launch", score, "strong")

    # Funder identity is kept as corroboration only until the operator graph is
    # better resolved. The first funder pass did not yet find shared multi-dev
    # clusters reliably, so it cannot authorize a trade on its own.
    if funder >= 0.80 and public_confirm >= 0.30 and features["age_ms"] <= 160:
        score = min(0.94, 0.72 + 0.12 * funder + 0.06 * public_confirm + 0.025 * fdv_score)
        return _make_profile(self, state, features, "trusted_funder_with_confirmation", score, "standard")

    # Crucially: unknown/anonymous public flow has no standalone entry authority.
    return False, 0.0, 0.0, "E4 V9 identity-only gate: no proven creator/prearmed/social edge", features


core.E4Policy.entry = _entry_v9

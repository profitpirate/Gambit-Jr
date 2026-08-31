from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from . import e4_hardening_v7 as v7

core = v7.core
final = v7.final
v6 = v7.v6
LOGGER = logging.getLogger("gambit.e4.hardening.v8")


def _read_json(path: str) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {}
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOGGER.exception("Could not load E4 V8 model path=%s", target)
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


_CREATOR_MODEL = _read_json(
    os.getenv("E4_WINNING_CREATORS_PATH", "models/e4/e4-winning-creators.json")
)
_WINNER_CREATORS: dict[str, dict[str, Any]] = {
    str(key): dict(value)
    for key, value in dict(_CREATOR_MODEL.get("creators") or {}).items()
    if isinstance(value, Mapping)
}
_SOCIAL_MODEL = _read_json(
    os.getenv("E4_SOCIAL_SOURCES_PATH", "models/e4/e4-social-sources.json")
)
_SOCIAL_HANDLES: dict[str, dict[str, Any]] = {
    str(key).lower().lstrip("@"): dict(value)
    for key, value in dict(_SOCIAL_MODEL.get("handles") or {}).items()
    if isinstance(value, Mapping)
}


def reload_identity_models() -> None:
    """Reload creator/social registries without restarting the process."""
    global _CREATOR_MODEL, _WINNER_CREATORS, _SOCIAL_MODEL, _SOCIAL_HANDLES
    _CREATOR_MODEL = _read_json(
        os.getenv("E4_WINNING_CREATORS_PATH", "models/e4/e4-winning-creators.json")
    )
    _WINNER_CREATORS = {
        str(key): dict(value)
        for key, value in dict(_CREATOR_MODEL.get("creators") or {}).items()
        if isinstance(value, Mapping)
    }
    _SOCIAL_MODEL = _read_json(
        os.getenv("E4_SOCIAL_SOURCES_PATH", "models/e4/e4-social-sources.json")
    )
    _SOCIAL_HANDLES = {
        str(key).lower().lstrip("@"): dict(value)
        for key, value in dict(_SOCIAL_MODEL.get("handles") or {}).items()
        if isinstance(value, Mapping)
    }


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _score01(value: Any) -> float:
    number = _finite(value)
    if number is None:
        return 0.0
    if number > 1.0:
        number /= 100.0
    return min(1.0, max(0.0, number))


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _merged(row: Mapping[str, Any]) -> dict[str, Any]:
    output = dict(row)
    for key in ("payload_json", "event_json", "raw_json", "data_json", "payload", "metadata"):
        for nested_key, nested_value in _mapping(output.get(key)).items():
            output.setdefault(str(nested_key), nested_value)
    return output


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    lowered = {str(key).lower(): value for key, value in row.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return None


def _handle(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "x.com/" in text or "twitter.com/" in text:
        try:
            path = urlparse(text).path.strip("/")
            text = path.split("/", 1)[0]
        except Exception:
            pass
    return text.lstrip("@").lower()


def observe_context(mint: str, row: Mapping[str, Any]) -> None:
    """Cache identity/social facts before the hot-path entry decision.

    This function performs no network I/O. Upstream X/Telegram/community watchers
    can project prelaunch evidence into the canonical event payload and V8 will
    consume it here at O(1) cost.
    """
    if not mint:
        return
    merged = _merged(row)
    context = v6._CONTEXT_BY_MINT.setdefault(mint, {})
    creator = _first(merged, "creator", "developer", "dev_wallet")
    if creator:
        context["creator"] = str(creator)
    funder = _first(merged, "funder", "creator_funder", "funding_wallet")
    if funder:
        context["funder"] = str(funder)
    context["funder_score"] = max(
        _score01(context.get("funder_score")),
        _score01(_first(merged, "funder_score", "funding_wallet_score")),
    )
    social_value = _first(
        merged,
        "social_handle",
        "twitter_handle",
        "x_handle",
        "twitter",
        "x_url",
    )
    social_handle = _handle(social_value)
    if social_handle:
        context["social_handle"] = social_handle
    context["social_authority_score"] = max(
        _score01(context.get("social_authority_score")),
        _score01(
            _first(
                merged,
                "social_authority_score",
                "social_score",
                "twitter_score",
                "x_score",
            )
        ),
    )
    followers = _finite(_first(merged, "followers", "twitter_followers", "x_followers"))
    if followers is not None:
        context["social_followers"] = max(float(context.get("social_followers") or 0), followers)
    context["prelaunch_social"] = bool(
        context.get("prelaunch_social")
        or _truthy(
            _first(
                merged,
                "prelaunch_social",
                "announced_prelaunch",
                "launch_announced",
                "community_preannounced",
            )
        )
    )
    context["community_score"] = max(
        _score01(context.get("community_score")),
        _score01(_first(merged, "community_score", "community_authority_score")),
    )
    # Explicit authorization is distinct from simply observing j7 metadata.
    context["prearmed"] = bool(
        context.get("prearmed")
        or _truthy(
            _first(
                merged,
                "prearmed",
                "authorized_sniper",
                "sniper_authorized",
                "launch_intent",
                "prelaunch_intent",
                "j7_sniper_authorized",
            )
        )
    )


_previous_from_row = core.Event.from_row.__func__


def _from_row_v8(cls: type[core.Event], row: Mapping[str, Any]) -> core.Event:
    event = _previous_from_row(cls, row)
    observe_context(event.mint, row)
    return event


core.Event.from_row = classmethod(_from_row_v8)


def _identity_features(state: core.TokenState) -> dict[str, float]:
    base = dict(v6._entry_features(state))
    context = v6._CONTEXT_BY_MINT.get(state.mint, {})
    creator = state.creator or str(context.get("creator") or "")
    creator_record = _WINNER_CREATORS.get(creator, {})
    repeat_score = _score01(creator_record.get("score"))
    repeat_wins = float(creator_record.get("e4_observed_wins") or 0.0)
    repeat_pnl = float(creator_record.get("e4_gross_pnl_sol") or 0.0)

    handle = str(context.get("social_handle") or "").lower().lstrip("@")
    social_record = _SOCIAL_HANDLES.get(handle, {})
    social_score = max(
        _score01(context.get("social_authority_score")),
        _score01(social_record.get("score")),
    )
    community_score = max(
        _score01(context.get("community_score")),
        _score01(social_record.get("community_score")),
    )
    followers = float(context.get("social_followers") or social_record.get("followers") or 0.0)
    follower_score = min(1.0, max(0.0, followers) / 250_000.0)

    base.update(
        {
            "winner_creator_score": repeat_score,
            "winner_creator_wins": repeat_wins,
            "winner_creator_gross_pnl_sol": repeat_pnl,
            "funder_score": _score01(context.get("funder_score")),
            "social_authority_score": social_score,
            "community_score": community_score,
            "social_followers": followers,
            "social_follower_score": follower_score,
            "prelaunch_social": 1.0 if context.get("prelaunch_social") else 0.0,
            "prearmed": 1.0 if context.get("prearmed") else base.get("prearmed", 0.0),
            "j7_source": 1.0
            if str(context.get("metadata_host") or "").lower() == "metadata.j7tracker.io"
            else 0.0,
        }
    )
    return base


def _make_profile(
    state: core.TokenState,
    features: dict[str, float],
    family: str,
    score: float,
    minimum_tier: str,
) -> tuple[bool, float, float, str, dict[str, float]]:
    tier, fraction = v6.relative_fraction_for_score(score, minimum_tier)
    fraction = min(fraction, core.E4Policy.__dict__.get("settings", None) or fraction) if False else fraction
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
            "e4_v8_score": score,
            "e4_v8_fraction": fraction,
            "e4_v8_tier_index": float(v6._TIER_ORDER.index(tier)),
            "e4_v8_first_partial": profile.first_partial_fraction,
        }
    )
    return True, score, fraction, f"E4_V8 family={family} tier={tier}", features


def _entry_v8(
    self: core.E4Policy,
    state: core.TokenState,
) -> tuple[bool, float, float, str, dict[str, float]]:
    if state.complete or state.migrated or state.wallet_touched:
        return False, 0.0, 0.0, "not an untouched live Pump curve", {}
    if state.created_ns is None:
        return False, 0.0, 0.0, "creation event not observed", {}

    features = _identity_features(state)
    max_age_ms = float(os.getenv("E4_V8_MAX_ENTRY_AGE_MS", "350"))
    max_fdv = min(self.settings.max_entry_fdv_usd, float(os.getenv("E4_V8_MAX_ENTRY_FDV_USD", "8500")))
    if features["age_ms"] > max_age_ms:
        return False, 0.0, 0.0, "outside E4 V8 launch decision horizon", features
    if features["fdv_usd"] <= 0 or features["fdv_usd"] > max_fdv:
        return False, 0.0, 0.0, "outside observed E4 entry FDV", features
    if features["sell_count"] > 0 or features["sell_sol"] > 0:
        return False, 0.0, 0.0, "sell appeared before E4 V8 confirmation", features
    if features["creator_buy_sol"] < float(os.getenv("E4_V8_MIN_CREATOR_BUY_SOL", "0.025")):
        return False, 0.0, 0.0, "creator seed not observed", features

    fdv_score = v6._fdv_fit(features["fdv_usd"])
    creator_seed = min(1.0, features["creator_buy_sol"] / 3.0)
    repeat = features["winner_creator_score"]
    funder = features["funder_score"]
    social = max(features["social_authority_score"], features["community_score"])
    public_confirm = min(
        1.0,
        0.45 * min(1.0, features["noncreator_buyers"] / 4.0)
        + 0.35 * min(1.0, features["noncreator_buy_sol"] / 8.0)
        + 0.20 * min(1.0, max(0.0, features["price_multiple"] - 1.0) / 0.40),
    )

    # 1) Deployer-authorized/prearmed launches: this is the only family that can
    # intentionally behave like a configured J7 sniper_wallet with zero delay.
    if features["prearmed"] >= 1.0 and features["age_ms"] <= 80:
        score = min(0.995, 0.90 + 0.03 * max(repeat, funder, social) + 0.03 * fdv_score + 0.04 * creator_seed)
        accepted, score, fraction, reason, features = _make_profile(
            state, features, "authorized_prearmed_launch", score, "elite"
        )
        fraction = min(fraction, self.settings.max_position_fraction)
        v6._PROFILE_BY_MINT[state.mint] = v6.EntryProfile(
            family="authorized_prearmed_launch",
            tier=v6._PROFILE_BY_MINT[state.mint].tier,
            fraction=fraction,
            score=score,
            first_partial_fraction=v6._PROFILE_BY_MINT[state.mint].first_partial_fraction,
            features=dict(features),
        )
        return accepted, score, fraction, reason, features

    # 2) Exact suggestion from the E4 forensics: if a creator previously made a
    # gross-winning E4 trade, their next launch is eligible immediately on the
    # creator seed. We still refuse a launch that already sold before decision.
    if repeat >= 0.80 and features["age_ms"] <= 100:
        score = min(
            0.985,
            0.82
            + 0.10 * repeat
            + 0.03 * min(1.0, features["winner_creator_wins"] / 3.0)
            + 0.02 * fdv_score
            + 0.03 * creator_seed,
        )
        minimum = "high" if features["winner_creator_wins"] >= 2 else "strong"
        accepted, score, fraction, reason, features = _make_profile(
            state, features, "repeat_e4_winning_creator", score, minimum
        )
        fraction = min(fraction, self.settings.max_position_fraction)
        profile = v6._PROFILE_BY_MINT[state.mint]
        v6._PROFILE_BY_MINT[state.mint] = v6.EntryProfile(
            family=profile.family,
            tier=profile.tier,
            fraction=fraction,
            score=profile.score,
            first_partial_fraction=profile.first_partial_fraction,
            features=profile.features,
        )
        return accepted, score, fraction, reason, features

    # 3) A large/established X or community account that announced the launch
    # before creation can act as identity evidence. It must be cached upstream;
    # V8 never waits on a social HTTP call after token creation.
    if features["prelaunch_social"] >= 1.0 and social >= 0.65 and features["age_ms"] <= 120:
        score = min(
            0.97,
            0.72
            + 0.12 * social
            + 0.05 * features["social_follower_score"]
            + 0.04 * fdv_score
            + 0.03 * creator_seed
            + 0.04 * public_confirm,
        )
        accepted, score, fraction, reason, features = _make_profile(
            state, features, "preannounced_social_community_launch", score, "strong"
        )
        fraction = min(fraction, self.settings.max_position_fraction)
        profile = v6._PROFILE_BY_MINT[state.mint]
        v6._PROFILE_BY_MINT[state.mint] = v6.EntryProfile(
            family=profile.family,
            tier=profile.tier,
            fraction=fraction,
            score=profile.score,
            first_partial_fraction=profile.first_partial_fraction,
            features=profile.features,
        )
        return accepted, score, fraction, reason, features

    # 4) Known creator/funder evidence can enter with a small amount of public
    # confirmation. J7 metadata is useful supporting evidence but never enough
    # by itself because its public docs expose a deploy API, not a public feed of
    # other users' future launches.
    identity = max(repeat, funder, social)
    if identity >= 0.72 and public_confirm >= 0.25 and features["age_ms"] <= 180:
        score = min(
            0.955,
            0.66 + 0.13 * identity + 0.07 * public_confirm + 0.04 * fdv_score + 0.03 * creator_seed,
        )
        accepted, score, fraction, reason, features = _make_profile(
            state, features, "known_identity_with_public_confirmation", score, "standard"
        )
        fraction = min(fraction, self.settings.max_position_fraction)
        profile = v6._PROFILE_BY_MINT[state.mint]
        v6._PROFILE_BY_MINT[state.mint] = v6.EntryProfile(
            family=profile.family,
            tier=profile.tier,
            fraction=fraction,
            score=profile.score,
            first_partial_fraction=profile.first_partial_fraction,
            features=profile.features,
        )
        return accepted, score, fraction, reason, features

    # The 300-launch holdout proved that public flow alone was the main false-
    # positive source. Preserve its diagnostics but revoke standalone authority.
    return False, 0.0, 0.0, "E4 V8 identity-first gate: public flow alone cannot authorize entry", features


core.E4Policy.entry = _entry_v8

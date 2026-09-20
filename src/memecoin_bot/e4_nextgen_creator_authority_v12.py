"""Post-certification canonical creator authority for V12.

This layer replaces only the creator-history source after the frozen causal
100-trade test passes. The existing V12 launch-quality formula remains
authoritative: FDV, seed, sell-before-entry, launch age, fingerprint/public
support, quality scoring and execution guards still apply.

It also applies the post-cert risk ladder to creator-quality entries. Direct E4
copy and independent social/narrative families are not reclassified here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from . import e4_role_model_v12 as role_model
from .v12_creator_library import (
    CreatorLibrary,
    promoted_library_activation_allowed,
)
from .v12_postcert_risk import decide_fraction

core = role_model.core
v12 = role_model.v12
v6 = role_model.v6

_CREATOR_LIBRARY_PATH = Path(
    os.getenv("V12_CREATOR_LIBRARY_PATH", "models/e4/v12-creator-library.json")
)
_CAUSAL_STATE_PATH = Path(
    os.getenv(
        "V12_CAUSAL_CERTIFICATION_STATE_PATH",
        "research/v12-pre-armed-100-causal-paper-live.json",
    )
)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


_LIBRARY = (
    CreatorLibrary.from_path(_CREATOR_LIBRARY_PATH)
    if _CREATOR_LIBRARY_PATH.exists()
    else None
)
_CAUSAL_STATE = _load_json(_CAUSAL_STATE_PATH)
_PROMOTED_RAW = {
    str(row.get("creator") or ""): dict(row)
    for row in ((_LIBRARY.payload.get("promoted") or []) if _LIBRARY else [])
    if str(row.get("creator") or "")
}


def active() -> bool:
    return bool(
        _LIBRARY
        and promoted_library_activation_allowed(_CAUSAL_STATE)
        and str(os.getenv("V12_POSTCERT_AGGRESSIVE_RISK_ENABLED", "")).lower()
        in {"1", "true", "yes", "on"}
    )


def reload_authority() -> None:
    """Reload proof/library state between deployments without touching hot path."""
    global _LIBRARY, _CAUSAL_STATE, _PROMOTED_RAW
    _LIBRARY = (
        CreatorLibrary.from_path(_CREATOR_LIBRARY_PATH)
        if _CREATOR_LIBRARY_PATH.exists()
        else None
    )
    _CAUSAL_STATE = _load_json(_CAUSAL_STATE_PATH)
    _PROMOTED_RAW = {
        str(row.get("creator") or ""): dict(row)
        for row in ((_LIBRARY.payload.get("promoted") or []) if _LIBRARY else [])
        if str(row.get("creator") or "")
    }


_PREVIOUS_CREATOR_HISTORY = v12._creator_history


def _history_numbers(row: dict[str, Any]) -> tuple[int, int, int, float]:
    e4 = row.get("e4") or {}
    fresh = row.get("fresh") or {}
    e4_wins = int(e4.get("wins") or 0)
    e4_losses = int(e4.get("losses") or 0)
    fresh_wins = int(fresh.get("wins") or 0)
    fresh_losses = int(fresh.get("losses") or 0)
    wins = e4_wins + fresh_wins
    losses = e4_losses + fresh_losses
    trades = wins + losses
    return wins, losses, trades, wins / trades if trades else 0.0


def _creator_history_nextgen(
    creator: str,
) -> tuple[int, int, int, float, Any | None]:
    if not active():
        return _PREVIOUS_CREATOR_HISTORY(creator)

    row = _PROMOTED_RAW.get(str(creator or ""))
    if row is None:
        # Critical: once post-cert canonical authority is enabled, old
        # expectancy/discovered registries cannot silently grant creator entry.
        return 0, 0, 0, 0.0, None

    wins, losses, trades, rate = _history_numbers(row)
    raw_fresh = row.get("fresh") or {}
    profile = SimpleNamespace(
        creator=str(creator),
        tier=SimpleNamespace(name=str(row.get("tier") or "PROMOTED")),
        score=float(row.get("quality_score") or 0.0),
        wins=wins,
        losses=losses,
        trades=trades,
        gross_win_rate=rate,
        negative=False,
        common_metadata_hosts=tuple(raw_fresh.get("metadata_hosts") or ()),
        common_social_handles=tuple(raw_fresh.get("social_handles") or ()),
        source="v12-canonical-promoted-library",
    )
    return wins, losses, trades, rate, profile


# _entry_v12 resolves this global at call time, so the launch-quality formula
# itself stays unchanged while its creator-history source becomes canonical.
v12._creator_history = _creator_history_nextgen


_PREVIOUS_ENTRY = core.E4Policy.entry
_CREATOR_FAMILIES = {
    "v12_elite_creator_quality_launch",
    "v12_proven_creator_quality_launch",
    "v12_recent_e4_repeat_launch",
    "elite_recurring_creator",
    "proven_repeat_creator",
    "prior_e4_winning_creator",
    "proven_repeat_e4_creator",
}


def _creator_for_state(state: Any) -> str:
    context = v6._CONTEXT_BY_MINT.get(str(state.mint), {})
    return str(context.get("creator") or getattr(state, "creator", "") or "")


def _entry_nextgen_authority(
    self: Any,
    state: Any,
) -> tuple[bool, float, float, str, dict[str, float]]:
    result = _PREVIOUS_ENTRY(self, state)
    if not active() or _LIBRARY is None:
        return result

    accepted, score, _fraction, reason, features = result
    if not accepted:
        return result

    profile = v6._PROFILE_BY_MINT.get(str(state.mint))
    family = str(getattr(profile, "family", "") or "")
    creator = _creator_for_state(state)

    if family in _CREATOR_FAMILIES and not _LIBRARY.is_promoted(creator):
        v6._PROFILE_BY_MINT.pop(str(state.mint), None)
        features = dict(features)
        features["v12_canonical_creator_veto"] = 1.0
        return (
            False,
            0.0,
            0.0,
            "V12 post-cert canonical creator veto",
            features,
        )

    if family not in _CREATOR_FAMILIES:
        return result

    drawdown = float(
        os.getenv("V12_CURRENT_CLOSED_EQUITY_DRAWDOWN_FRACTION", "0") or 0
    )
    risk = decide_fraction(
        creator=creator,
        causal_state=_CAUSAL_STATE,
        library=_LIBRARY,
        closed_equity_drawdown_fraction=drawdown,
    )
    if risk.blocked:
        v6._PROFILE_BY_MINT.pop(str(state.mint), None)
        return (
            False,
            0.0,
            0.0,
            f"V12 post-cert risk block: {risk.reason}",
            dict(features),
        )

    upgraded_fraction = min(
        float(risk.fraction),
        float(self.settings.max_position_fraction),
    )
    if profile is not None:
        profile.fraction = upgraded_fraction
        profile.features["v12_postcert_risk"] = 1.0
        profile.features["v12_postcert_fraction"] = upgraded_fraction
    enriched = dict(features)
    enriched["v12_postcert_risk"] = 1.0
    enriched["v12_postcert_fraction"] = upgraded_fraction
    enriched["v12_canonical_creator"] = 1.0
    return (
        True,
        float(score),
        upgraded_fraction,
        f"{reason}; post-cert canonical risk={risk.reason}",
        enriched,
    )


core.E4Policy.entry = _entry_nextgen_authority


def status() -> dict[str, Any]:
    return {
        "active": active(),
        "library": _LIBRARY.status() if _LIBRARY else None,
        "causal_closed_trades": int(
            (_CAUSAL_STATE.get("metrics") or {}).get("closed_trades") or 0
        ),
        "old_creator_registries_have_authority_when_active": False,
        "selection_formula_replaced": False,
        "risk_policy": "postcert-bounded",
    }

from __future__ import annotations

from typing import Any

from . import e4_hardening_v6 as v6

core = v6.core
final = v6.final


# Launch fixtures and some canonical providers identify the creator on the
# CREATE event's trader field without copying it into Event.creator. Recover
# that identity before classifying creator-seeded flow.
_previous_entry_features = v6._entry_features


def _entry_features_v7(state: core.TokenState) -> dict[str, float]:
    context = v6._CONTEXT_BY_MINT.setdefault(state.mint, {})
    creator = state.creator or str(context.get("creator") or "")
    if not creator:
        create = next(
            (
                event
                for event in state.events
                if event.kind == core.EventKind.CREATE and event.trader
            ),
            None,
        )
        if create is not None:
            creator = str(create.trader)
            context["creator"] = creator
            state.creator = creator
    return _previous_entry_features(state)


v6._entry_features = _entry_features_v7


_previous_entry = core.E4Policy.entry


def _entry_v7(
    self: core.E4Policy,
    state: core.TokenState,
) -> tuple[bool, float, float, str, dict[str, float]]:
    accepted, score, fraction, reason, features = _previous_entry(self, state)
    if not accepted and reason == "creator seed not observed":
        # Preserve the old diagnostic contract while making clear that V6 no
        # longer treats bundle structure as the only possible E4 entry family.
        reason = "creator seed or E4 multi-buy structure not observed"
    if accepted and "authorized_prearmed_launch" in reason and score < 0.93:
        score = 0.94
        tier, fraction = v6.relative_fraction_for_score(score, "elite")
        fraction = min(fraction, self.settings.max_position_fraction)
        profile = v6.EntryProfile(
            family="authorized_prearmed_launch",
            tier=tier,
            fraction=fraction,
            score=score,
            first_partial_fraction=v6._profile_partial(tier),
            features=dict(features),
        )
        v6._PROFILE_BY_MINT[state.mint] = profile
        features = dict(features)
        features.update(
            {
                "e4_v6_score": score,
                "e4_v6_fraction": fraction,
                "e4_v6_tier_index": float(v6._TIER_ORDER.index(tier)),
                "e4_v6_first_partial": profile.first_partial_fraction,
            }
        )
        reason = f"E4_V6 family=authorized_prearmed_launch tier={tier}"
    return accepted, score, fraction, reason, features


core.E4Policy.entry = _entry_v7


_previous_exit = core.E4Policy.exit


def _exit_v7(
    self: core.E4Policy,
    position: core.Position,
    state: core.TokenState,
) -> tuple[str, float, str]:
    action, fraction, reason = _previous_exit(self, position, state)
    # Old/recovered positions that predate V6 have no confidence profile. Keep
    # their legacy hard horizon so existing safety guarantees remain intact.
    if (
        action == "HOLD"
        and reason == "E4 V6 confirmed runner beyond legacy horizon"
        and position.mint not in v6._PROFILE_BY_MINT
    ):
        return "SELL_ALL", 1.0, "E4 observed hold horizon"
    return action, fraction, reason


core.E4Policy.exit = _exit_v7

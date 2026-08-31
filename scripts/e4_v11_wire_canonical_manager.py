#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


def replace_block(text: str, start: str, end: str, replacement: str) -> str:
    begin = text.index(start)
    finish = text.index(end, begin)
    return text[:begin] + replacement.rstrip() + "\n\n" + text[finish:]


def patch_public_api() -> None:
    path = Path("src/memecoin_bot/e4_pipelines_v10.py")
    text = path.read_text(encoding="utf-8")
    marker = "# V11_CANONICAL_PIPELINE_API"
    if marker not in text:
        text = text.rstrip() + '''\n\n\n# V11_CANONICAL_PIPELINE_API\n# The V10 primitives above remain reusable and directly tested. V11 exposes one\n# canonical manager for production and compatibility with the earlier engine.\nfrom .e4_pipeline_manager_v11 import PipelineManager  # noqa: E402\nfrom .e4_pipeline_singleton_v11 import manager  # noqa: E402\n'''
        path.write_text(text, encoding="utf-8")


def patch_hardening() -> None:
    path = Path("src/memecoin_bot/e4_hardening_v10.py")
    text = path.read_text(encoding="utf-8")
    import_line = "from .e4_pipeline_singleton_v11 import manager as PIPELINES\n"
    if import_line not in text:
        anchor = "from .e4_pipeline import ActiveNarrativeCache, AtomicCreatorRegistry, PipelineCoordinator, SocialPost, V10Runtime\n"
        text = text.replace(anchor, anchor + import_line, 1)

    entry = '''def _entry_v10(self: core.E4Policy, state: core.TokenState) -> tuple[bool, float, float, str, dict[str, float]]:
    context = v6._CONTEXT_BY_MINT.setdefault(state.mint, {})
    features = dict(v8._identity_features(state))
    creator = str(context.get("creator") or getattr(state, "creator", "") or "")
    launch_ns = int(context.get("create_received_ns") or getattr(state, "first_ns", 0) or getattr(state, "latest_ns", 0) or time.time_ns())
    now_ns = int(context.get("last_received_ns") or getattr(state, "latest_ns", 0) or time.time_ns())
    creator_seed = float(features.get("creator_buy_sol", 0.0) or 0.0)

    # A cooperating/prearmed launch is an explicit authority path and must be
    # evaluated before any historical negative record.
    if bool(context.get("prearmed")):
        decision_family = "authorized_prearmed_launch"
        decision_score = 0.97
        decision_fraction = 0.10
        decision_reason = "authorized_prearmed_launch identity authority"
        decision_ns = 0
        decision_evidence: dict[str, Any] = {"prearmed": True}
    else:
        # Preserve the causal V9 test/model interface while V11's registry is
        # the production source of truth. This also lets older model snapshots
        # remain valid during a rolling deployment.
        legacy = v9._EXPECTANCY_CREATORS.get(creator) if creator else None
        if isinstance(legacy, Mapping):
            wins = int(legacy.get("wins") or 0)
            losses = int(legacy.get("losses") or 0)
            trades = max(int(legacy.get("trades") or 0), wins + losses)
            rate = float(legacy.get("gross_win_rate") if legacy.get("gross_win_rate") is not None else (wins / trades if trades else 0.0))
            features["creator_prior_wins"] = float(wins)
            features["creator_prior_losses"] = float(losses)
            if trades >= 3 and rate <= 0.25:
                return False, 0.0, 0.0, "negative creator history identity veto", features
            if trades >= 3 and wins >= 2 and rate >= 0.75:
                decision_family = "proven_repeat_e4_creator"
                decision_score = max(0.93, min(0.99, 0.90 + rate * 0.08))
                decision_fraction = 0.05 if rate >= 0.90 else 0.03
                decision_reason = "proven_repeat_e4_creator identity fast path"
                decision_ns = 0
                decision_evidence = {"creator_tier": "ELITE"}
            elif wins >= 1 and rate >= 0.50:
                decision_family = "prior_e4_winning_creator"
                decision_score = max(0.84, min(0.93, 0.82 + rate * 0.08))
                decision_fraction = 0.0185
                decision_reason = "prior_e4_winning_creator identity fast path"
                decision_ns = 0
                decision_evidence = {"creator_tier": "APPROVED"}
            else:
                legacy = None
        if not isinstance(legacy, Mapping) or 'decision_family' not in locals():
            if creator_seed <= 0 and not bool(context.get("e4_confirmed")):
                return False, 0.0, 0.0, "creator seed required before identity evaluation", features
            decision = PIPELINES.decide_launch(
                mint=state.mint,
                creator=creator,
                name=str(context.get("name") or ""),
                symbol=str(context.get("symbol") or ""),
                metadata_uri=str(context.get("uri") or ""),
                launch_ns=launch_ns,
                now_ns=now_ns,
                fdv_usd=float(getattr(state, "fdv_usd", 0.0) or 0.0),
                creator_buy_sol=creator_seed,
                sell_count=int(features.get("sell_count", features.get("sells", 0.0)) or 0),
                price_sol=float(getattr(state, "price_sol", 0.0) or 0.0),
                e4_confirmed=bool(context.get("e4_confirmed")),
                e4_observed_ns=int(context.get("e4_observed_ns") or 0),
                e4_entry_price=float(context.get("e4_entry_price") or 0.0),
            )
            decision_family = decision.family
            decision_score = decision.score
            decision_fraction = decision.fraction
            decision_reason = decision.reason
            decision_ns = decision.decision_ns
            decision_evidence = dict(decision.evidence)
            if not decision.accepted:
                context["v10_decision_duration_ns"] = decision_ns
                features["e4_v10_decision_latency_ns"] = float(decision_ns)
                for key, value in decision_evidence.items():
                    if isinstance(value, bool):
                        features[f"v10_{key}"] = float(value)
                    elif isinstance(value, (int, float)):
                        features[f"v10_{key}"] = float(value)
                return False, 0.0, 0.0, decision_reason, features

    context["v10_decision_completed_ns"] = time.time_ns()
    context["v10_decision_duration_ns"] = int(decision_ns)
    features["e4_v10_decision_latency_ns"] = float(decision_ns)
    features["v10_decision_duration_ms"] = float(decision_ns) / 1_000_000.0
    for key, value in decision_evidence.items():
        if isinstance(value, bool):
            features[f"v10_{key}"] = float(value)
        elif isinstance(value, (int, float)):
            features[f"v10_{key}"] = float(value)

    tier, ladder_fraction = v6.relative_fraction_for_score(float(decision_score))
    fraction = min(float(decision_fraction or ladder_fraction), self.settings.max_position_fraction)
    for candidate in v6._TIER_ORDER:
        if abs(v6._TIER_FRACTIONS[candidate] - fraction) < 1e-12:
            tier = candidate
            break
    profile = v6.EntryProfile(
        family=str(decision_family),
        tier=tier,
        fraction=fraction,
        score=float(decision_score),
        first_partial_fraction=v6._profile_partial(tier),
        features=dict(features),
    )
    v6._PROFILE_BY_MINT[state.mint] = profile
    features.update({
        "e4_v10_score": profile.score,
        "e4_v10_fraction": profile.fraction,
        "e4_v10_tier_index": float(v6._TIER_ORDER.index(tier)),
        "e4_v10_first_partial": profile.first_partial_fraction,
    })
    return True, profile.score, profile.fraction, f"E4_V11 family={profile.family} tier={tier}: {decision_reason}", features
'''
    text = replace_block(text, "def _entry_v10(", "core.E4Policy.entry = _entry_v10", entry + "\n\ncore.E4Policy.entry = _entry_v10")

    if "def _runtime_for(" not in text:
        anchor = "_previous_engine_init = core.Engine.__init__\n"
        helper = '''_previous_engine_init = core.Engine.__init__\n\n\ndef _runtime_for(engine: core.Engine) -> V10Runtime:\n    runtime = getattr(engine, "v10_runtime", None)\n    if runtime is None:\n        settings = engine.settings\n        runtime = V10Runtime(\n            oracle_wallet=settings.oracle_wallet,\n            fraction_resolver=v6.relative_fraction_for_score,\n            execution_db=settings.execution_db,\n        )\n        engine.v10_runtime = runtime\n        if getattr(engine, "policy", None) is not None:\n            engine.policy.v10_runtime = runtime\n        engine.v10_runtime_started = False\n    return runtime\n\n'''
        text = text.replace(anchor, helper, 1)
    text = text.replace("runtime: V10Runtime = self.v10_runtime", "runtime: V10Runtime = _runtime_for(self)")

    if "def _exit_v11(" not in text:
        text = text.rstrip() + '''\n\n\n_previous_exit_v11 = core.E4Policy.exit\n\n\ndef _exit_v11(self: core.E4Policy, position: core.Position, state: core.TokenState):\n    profile = v6._PROFILE_BY_MINT.get(position.mint)\n    if profile is not None and str(getattr(profile, "family", "")) == "e4_confirmed_fast_copy":\n        source = PIPELINES.e4_signal(position.mint)\n        if source is not None:\n            if source.fully_exited:\n                return "SELL_FULL", 1.0, "E4 V11 copy source fully exited"\n            if source.last_sell_ns > 0 and not position.first_partial_done:\n                fraction = min(0.50, max(0.20, source.last_sell_fraction or profile.first_partial_fraction))\n                return "SELL_PARTIAL", fraction, "E4 V11 copy source first partial"\n            if source.sell_count >= 2 and position.first_partial_done:\n                return "SELL_FULL", 1.0, "E4 V11 copy source second exit leg"\n    return _previous_exit_v11(self, position, state)\n\n\ncore.E4Policy.exit = _exit_v11\n'''

    path.write_text(text, encoding="utf-8")


def main() -> int:
    patch_public_api()
    patch_hardening()
    print("V11 canonical pipeline manager wired")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

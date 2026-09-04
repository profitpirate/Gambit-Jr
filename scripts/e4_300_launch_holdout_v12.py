#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

from memecoin_bot import e4_hardening_v12 as v12
from memecoin_bot import e4_role_model_v12 as role_model
from memecoin_bot import e4_direct_copy_v12 as direct_copy
from memecoin_bot import e4_copy_fidelity_v12 as copy_fidelity
from memecoin_bot import e4_preconfirm_v12 as preconfirm

E4_V12_ROLE_MODEL_POLICY_SHA256 = "f4d5959b25f607bc667073b672d66570bf29d8d2b2020811605808ce08e032df"
E4_V12_DIRECT_COPY_POLICY_SHA256 = "b286da26c965420ce2146b396b76717cb79c681c1ea90e72120662d953a6bdc9"
E4_V12_COPY_FIDELITY_POLICY_SHA256 = "f02f3bafd259fcfee918568397ebb83906681773a4958b5d58728ade500f0633"
E4_V12_PRECONFIRM_POLICY_SHA256 = "3aef1c8ec287f529be4dd9314c383b748b818fe913e62639a3c0b52b29420961"
role_model.assert_policy_fingerprint(E4_V12_ROLE_MODEL_POLICY_SHA256)
direct_copy.assert_policy_fingerprint(E4_V12_DIRECT_COPY_POLICY_SHA256)
copy_fidelity.assert_policy_fingerprint(E4_V12_COPY_FIDELITY_POLICY_SHA256)
preconfirm.assert_policy_fingerprint(E4_V12_PRECONFIRM_POLICY_SHA256)


def load_holdout():
    path = Path(__file__).with_name("e4_300_launch_holdout.py")
    name = "e4_300_launch_holdout_v12_base"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    holdout = load_holdout()
    base = holdout.load_base()
    previous_anchor_to_live = base.anchor_to_live

    def anchor_to_live_v12(item, *args, **kwargs):
        event = previous_anchor_to_live(item, *args, **kwargs)
        if event is None:
            return None
        # Static creation metadata may be cached during capture. Economic and
        # E4-wallet signals are deliberately deferred until replay order so no
        # future trade can leak into an earlier decision. Historical holdout
        # data does not contain raw instruction bytes, so the preconfirm module
        # is pinned here but cannot fabricate pre-E4 authority in replay.
        v12.v8.observe_context(event.mint, item)
        context = v12.v6._CONTEXT_BY_MINT.setdefault(event.mint, {})
        for key in ("creator", "name", "symbol", "uri", "token_program", "is_mayhem_mode"):
            value = item.get(key) if isinstance(item, dict) else None
            if value not in (None, ""):
                context[key] = value
        return event

    base.anchor_to_live = anchor_to_live_v12

    previous_to_core = base.LiveEvent.to_core

    def to_core_v12(self):
        event = previous_to_core(self)
        role_model.observe_market_event(event)
        return event

    base.LiveEvent.to_core = to_core_v12

    previous_simulate_token = base.simulate_token

    def simulate_token_v12(events, settings, latency_ms):
        if not events:
            return None
        mint = events[0].mint
        role_model.reset_role_model_replay(mint)
        trade = previous_simulate_token(events, settings, latency_ms)
        if trade is not None:
            return trade

        profile = v12.v6._PROFILE_BY_MINT.get(mint)
        if profile is None or str(getattr(profile, "family", "")) != direct_copy.DIRECT_COPY_FAMILY:
            return None

        original_slippage = settings.buy_slippage_bps
        try:
            settings.buy_slippage_bps = direct_copy.direct_copy_slippage_bps(settings)
            role_model.reset_role_model_replay(mint)
            return previous_simulate_token(events, settings, latency_ms)
        finally:
            settings.buy_slippage_bps = original_slippage

    base.simulate_token = simulate_token_v12
    holdout.load_base = lambda: base
    raise SystemExit(asyncio.run(holdout.main()))

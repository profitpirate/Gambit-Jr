from __future__ import annotations

import sys
from pathlib import Path

import pytest

from memecoin_bot.v12_creator_library import CreatorLibrary
from memecoin_bot.v12_postcert_risk import decide_fraction

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import e4_v12_fast_prearmed_selector as selector


def library() -> CreatorLibrary:
    return CreatorLibrary(
        {
            "schema_version": "v12-creator-library-v2",
            "promoted": [
                {
                    "creator": "elite",
                    "status": "PROMOTED",
                    "tier": "ELITE",
                    "source": "E4_VERIFIED_HISTORY",
                    "quality_score": 0.95,
                    "selection_authority": "RECOGNISED_CREATOR_ONLY",
                    "auto_buy": False,
                },
                {
                    "creator": "promoted",
                    "status": "PROMOTED",
                    "tier": "PROMOTED",
                    "source": "FRESH_APPRENTICESHIP",
                    "quality_score": 0.8,
                    "selection_authority": "RECOGNISED_CREATOR_ONLY",
                    "auto_buy": False,
                },
            ],
            "shortlisted": [
                {
                    "creator": "watch",
                    "status": "SHORTLISTED",
                    "tier": "SHORTLIST",
                    "source": "FRESH_APPRENTICESHIP",
                    "quality_score": 0.7,
                    "selection_authority": "RESEARCH_ONLY",
                    "auto_buy": False,
                }
            ],
        }
    )


def passed_state() -> dict:
    return {
        "completion": {"reached": True},
        "metrics": {"closed_trades": 100, "acceptance_gate_passed": True},
    }


def incomplete_state() -> dict:
    return {
        "completion": {"reached": False},
        "metrics": {"closed_trades": 7, "acceptance_gate_passed": False},
    }


def test_library_forbids_auto_buy_records() -> None:
    with pytest.raises(ValueError, match="auto-buy"):
        CreatorLibrary(
            {
                "schema_version": "v12-creator-library-v2",
                "promoted": [
                    {
                        "creator": "bad",
                        "status": "PROMOTED",
                        "tier": "ELITE",
                        "source": "test",
                        "quality_score": 1,
                        "selection_authority": "RECOGNISED_CREATOR_ONLY",
                        "auto_buy": True,
                    }
                ],
                "shortlisted": [],
            }
        )


def test_promoted_creator_only_replaces_recognition_leg() -> None:
    kwargs = dict(
        creator="promoted",
        social_handle="new-handle",
        social_status_ns=1_000_000_000,
        create_ns=2_000_000_000,
        prior_e4_attempts=0,
        creator_seed_sol=2.0,
        mayhem_mode=False,
        creator_handles={},
        promoted_creators={"promoted"},
        promoted_library_enabled=True,
    )
    assert selector.should_select_nextgen(**kwargs) is True
    assert selector.should_select_nextgen(**{**kwargs, "creator_seed_sol": 1.99}) is False
    assert selector.should_select_nextgen(**{**kwargs, "mayhem_mode": True}) is False
    assert selector.should_select_nextgen(**{**kwargs, "social_handle": ""}) is False
    assert selector.should_select_nextgen(
        **{**kwargs, "social_status_ns": -9_000_000_001}
    ) is False


def test_shortlisted_creator_never_gets_selection_authority() -> None:
    assert selector.should_select_nextgen(
        creator="watch",
        social_handle="watch-handle",
        social_status_ns=1_000_000_000,
        create_ns=2_000_000_000,
        prior_e4_attempts=0,
        creator_seed_sol=3.0,
        mayhem_mode=False,
        creator_handles={},
        promoted_creators=set(library().promoted),
        promoted_library_enabled=True,
    ) is False


def test_aggressive_risk_cannot_activate_before_100_trade_pass() -> None:
    decision = decide_fraction(
        creator="elite",
        causal_state=incomplete_state(),
        library=library(),
        closed_equity_drawdown_fraction=0.0,
        environment={"V12_POSTCERT_AGGRESSIVE_RISK_ENABLED": "true"},
    )
    assert decision.fraction == 0.10
    assert decision.postcert_active is False


def test_postcert_risk_tiers_are_bounded_and_drawdown_aware() -> None:
    env = {"V12_POSTCERT_AGGRESSIVE_RISK_ENABLED": "true"}
    elite = decide_fraction(
        creator="elite",
        causal_state=passed_state(),
        library=library(),
        closed_equity_drawdown_fraction=0.0,
        environment=env,
    )
    promoted = decide_fraction(
        creator="promoted",
        causal_state=passed_state(),
        library=library(),
        closed_equity_drawdown_fraction=0.0,
        environment=env,
    )
    base = decide_fraction(
        creator="unknown",
        causal_state=passed_state(),
        library=library(),
        closed_equity_drawdown_fraction=0.0,
        environment=env,
    )
    assert elite.fraction == 0.175
    assert promoted.fraction == 0.15
    assert base.fraction == 0.125
    assert elite.maximum_concurrent_positions == 2

    reduced = decide_fraction(
        creator="elite",
        causal_state=passed_state(),
        library=library(),
        closed_equity_drawdown_fraction=0.10,
        environment=env,
    )
    assert reduced.fraction == 0.0875

    halted = decide_fraction(
        creator="elite",
        causal_state=passed_state(),
        library=library(),
        closed_equity_drawdown_fraction=0.15,
        environment=env,
    )
    assert halted.blocked is True
    assert halted.fraction == 0.0

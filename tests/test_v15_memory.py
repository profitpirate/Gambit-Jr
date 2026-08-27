from __future__ import annotations

import pytest

from memecoin_bot.v15_memory import (
    ColdArchive,
    MarketRegime,
    classify_regime,
    missed_runner_attribution,
    operator_relationship,
    outcome_class,
    post_call_risk,
    rank_alpha_wallet,
    social_state,
)


def test_empirical_alpha_wallet_ranking_requires_sample_for_proven():
    small = [
        {"matured": True, "peak_multiple": 10, "entry_age_minutes": 2, "days_ago": 2}
        for _ in range(3)
    ]
    assert rank_alpha_wallet(small)["grade"] == "PROMISING"
    large = small * 7
    assert rank_alpha_wallet(large)["grade"] == "PROVEN"


def test_operator_language_never_infers_same_owner():
    assert operator_relationship({"common_funder": True}) == "COMMON_FUNDER"
    assert operator_relationship({"repeated_deployment_pattern": True}) == "REPEATED_DEPLOYMENT_PATTERN"
    assert "OWNER" not in operator_relationship({"direct_transfer": True})


def test_market_regime_miss_attribution_and_post_call_risk():
    hot = [{"peak_multiple": 3, "failed_before_2x": False} for _ in range(5)]
    assert classify_regime(hot) == MarketRegime.HOT
    assert missed_runner_attribution({"provider_outage": True}) == "PROVIDER_MISS"
    risk = post_call_risk(
        {"liquidity_usd": 30, "buyer_replacement": "BUYER_COLLAPSE"},
        {"liquidity_usd": 100},
    )
    assert risk["state"] == "EXIT_RISK"


def test_five_x_then_rug_is_runner_and_failed_after_runner():
    assert outcome_class(5, True) == "RUG_AFTER_RUNNER"
    assert outcome_class(1.5, True) == "RUG_BEFORE_2X"


def test_static_social_metadata_is_not_velocity():
    assert social_state(True, None) == "NO_VERIFIED_VELOCITY"
    assert social_state(True, 3) == "SOCIAL_ACCELERATING"


def test_cold_archive_partition_and_hot_db_separation(tmp_path):
    archive = ColdArchive(tmp_path / "archive")
    assert "chain=solana" in str(archive.partition_path("solana", "2026-08-27T00:00:00+00:00"))
    archive.assert_outside_hot_database(tmp_path / "hot" / "live.db")
    with pytest.raises(ValueError):
        ColdArchive(tmp_path / "hot").assert_outside_hot_database(tmp_path / "hot" / "live.db")

from __future__ import annotations

import pytest

from memecoin_bot.historical.intelligence_v2 import (
    CONTROL_FREEZE_SHA,
    IDENTIFIER_REGISTRY,
    IdentifierFamily,
    IdentifierState,
    IntelligenceV2Research,
    Objective,
    build_trajectory_features,
    entry_quality_v2,
    failure_v2,
    migration_continuity_v2,
    survival_v2,
)
from memecoin_bot.historical.intelligence_v2_research import (
    IntelligenceV2Experiment,
    _calibration,
    _percentile_scores,
    _rank,
)
from memecoin_bot.v15_engine import Stage, evaluate_v15


def _observations(**overrides):
    base = {
        "stage": "NEW",
        "market_cap_unit": "SOL",
        "initial_market_cap_sol": 8,
        "creator_past_tokens": 5,
        "creator_past_rugs": 0,
        "concentration_score": 75,
        "tradeability_score": 80,
        "liquidity_score": 75,
    }
    points = [
        {
            **base,
            "timestamp_seconds": 30,
            "current_market_cap": 8,
            "buyer_count": 5,
            "buy_count": 6,
            "sell_count": 1,
            "trade_count": 7,
            "buy_volume": 2,
        },
        {
            **base,
            "timestamp_seconds": 60,
            "current_market_cap": 11,
            "buyer_count": 12,
            "buyer_growth": 7,
            "buy_count": 15,
            "sell_count": 3,
            "trade_count": 18,
            "buy_volume": 6,
        },
        {
            **base,
            "timestamp_seconds": 180,
            "current_market_cap": 15,
            "buyer_count": 28,
            "buyer_growth": 16,
            "buyer_acceleration": 9,
            "buy_count": 36,
            "sell_count": 8,
            "trade_count": 44,
            "buy_volume": 15,
            "price_return_pct": 25,
        },
    ]
    for point in points:
        point.update(overrides)
    return points


def test_registry_is_unique_versioned_and_covers_every_required_family():
    definitions = IDENTIFIER_REGISTRY.all()
    assert len(definitions) >= 40
    assert len({definition.identifier_id for definition in definitions}) == len(definitions)
    assert {definition.family for definition in definitions} == set(IdentifierFamily)
    assert all(definition.quantitative_definition for definition in definitions)
    assert all(definition.status == "RESEARCH_ONLY" for definition in definitions)


def test_trajectory_preserves_levels_change_acceleration_persistence_and_decay():
    features = build_trajectory_features(_observations())
    assert features["buyer_count"] == 28
    assert features["buyer_count_delta"] == 16
    assert features["buyer_count_velocity"] > 0
    assert features["buyer_count_acceleration"] < 0
    assert features["buyer_count_persistence"] == 1
    assert features["market_cap_drawdown"] == 0


def test_survival_never_claims_certain_100_from_sparse_evidence():
    failure = failure_v2({})
    survival = survival_v2({}, failure)
    assert survival["score"] < 100
    assert survival["confidence"] < 50
    assert survival["unknown_inputs"]


def test_migration_continuity_is_measured_or_explicitly_unknown():
    unknown = migration_continuity_v2({"post_migration_liquidity": 10_000})
    assert unknown["state"] == "UNKNOWN"
    healthy = migration_continuity_v2(
        {
            "pre_migration_liquidity": 10_000,
            "post_migration_liquidity": 9_000,
            "pre_migration_price": 0.001,
            "post_migration_price": 0.0011,
            "migration_gap_seconds": 20,
            "post_migration_sell_pressure": 0.3,
        }
    )
    assert healthy["state"] == "HEALTHY"
    assert healthy["confidence"] == 100


def test_context_aware_entry_does_not_punish_real_acceleration_automatically():
    features = build_trajectory_features(
        _observations(current_market_cap=30, initial_market_cap_sol=8)
    )
    valid = entry_quality_v2(features, {"state": "UNKNOWN"})
    assert valid["state"] == "ACCELERATING_BUT_ENTRY_VALID"
    features["buyer_growth"] = 0
    features["buyer_acceleration"] = -5
    chasing = entry_quality_v2(features, {"state": "UNKNOWN"})
    assert chasing["state"] == "CHASE"


@pytest.mark.parametrize(
    ("name", "overrides", "expected_policy"),
    [
        (
            "clean quick 2x",
            {"buyer_acceleration": 0},
            {"PREMIUM_RESEARCH", "STRONG_RESEARCH"},
        ),
        (
            "clean 5x",
            {"initial_market_cap_sol": 4, "buyer_acceleration": 1},
            {"PREMIUM_RESEARCH", "STRONG_RESEARCH"},
        ),
        (
            "clean 20x",
            {"current_market_cap": 4, "initial_market_cap_sol": 1},
            {"RIGHT_TAIL_ALERT_RESEARCH", "STRONG_RESEARCH", "PREMIUM_RESEARCH"},
        ),
        (
            "extreme 50x style",
            {"current_market_cap": 2, "initial_market_cap_sol": 0.5, "buyer_acceleration": 20},
            {"RIGHT_TAIL_ALERT_RESEARCH", "STRONG_RESEARCH", "PREMIUM_RESEARCH"},
        ),
        (
            "high risk runner",
            {"toxic_creator": True, "current_market_cap": 4, "initial_market_cap_sol": 1},
            {"HIGH_RISK_MOMENTUM_RESEARCH", "STRONG_RESEARCH", "SILENT_WATCH_RESEARCH"},
        ),
        ("rug", {"sell_restriction": True}, {"REJECT"}),
        ("liquidity collapse", {"liquidity_removal": True, "liquidity_usd": 100}, {"REJECT"}),
        (
            "fake buyer growth",
            {"sybil_adjusted_buyer_ratio": 0.2},
            {"PREMIUM_RESEARCH", "STRONG_RESEARCH", "SILENT_WATCH_RESEARCH"},
        ),
        (
            "Sybil pump",
            {"sybil_adjusted_buyer_ratio": 0.1, "buyer_acceleration": 30},
            {"PREMIUM_RESEARCH", "STRONG_RESEARCH", "SILENT_WATCH_RESEARCH"},
        ),
        (
            "creator linked",
            {"creator_linked_buyer_share": 0.8},
            {"PREMIUM_RESEARCH", "STRONG_RESEARCH", "SILENT_WATCH_RESEARCH"},
        ),
        (
            "healthy migration",
            {
                "stage": "MIGRATED",
                "market_cap_unit": "USD",
                "pre_migration_liquidity": 10_000,
                "post_migration_liquidity": 9_000,
                "pre_migration_price": 0.001,
                "post_migration_price": 0.0011,
                "migration_gap_seconds": 20,
            },
            {"STRONG_RESEARCH", "SILENT_WATCH_RESEARCH", "RIGHT_TAIL_ALERT_RESEARCH"},
        ),
        (
            "failed migration",
            {
                "stage": "MIGRATED",
                "market_cap_unit": "USD",
                "pre_migration_liquidity": 10_000,
                "post_migration_liquidity": 1_000,
                "pre_migration_price": 0.001,
                "post_migration_price": 0.0002,
                "migration_gap_seconds": 900,
            },
            {"SILENT_WATCH_RESEARCH", "STRONG_RESEARCH"},
        ),
        (
            "revival",
            {"stage": "REVIVAL", "fresh_catalyst_score": 90},
            {"CATALYST_REVIVAL_RESEARCH", "STRONG_RESEARCH", "SILENT_WATCH_RESEARCH"},
        ),
        (
            "overextended",
            {"current_market_cap": 500, "initial_market_cap_sol": 8},
            {"SILENT_WATCH_RESEARCH"},
        ),
        ("reject", {"unsellable": True}, {"REJECT"}),
    ],
)
def test_realistic_signal_fixtures_traverse_complete_v2_pipeline(name, overrides, expected_policy):
    decision = IntelligenceV2Research().evaluate(_observations(**overrides))
    assert decision.signal_policy in expected_policy, name
    assert set(decision.objectives) == {
        str(Objective.QUICK_2X),
        str(Objective.MID_5X),
        str(Objective.RIGHT_TAIL),
    }
    assert decision.public_alert_routed is False
    assert decision.production_eligible is False
    assert decision.control_freeze_sha == CONTROL_FREEZE_SHA
    assert decision.entry["state"]
    assert decision.survival["confidence"] <= decision.survival["coverage"]


def test_unknown_wallet_cluster_inputs_remain_unknown_not_zero():
    decision = IntelligenceV2Research().evaluate(_observations())
    signals = {signal.identifier_id: signal for signal in decision.identifiers}
    assert signals["REPEAT_RUNNER_WALLET"].state == IdentifierState.UNKNOWN
    assert signals["SHARED_FUNDER_CLUSTER"].state == IdentifierState.UNKNOWN
    assert signals["SYBIL_BUYER_RISK"].state == IdentifierState.UNKNOWN


def test_control_v15_remains_the_original_unweighted_mean():
    features = {
        "launch_verified": 80,
        "early_demand": 60,
        "buyer_independence": None,
        "creator_quality": None,
        "early_liquidity": None,
        "survival_quality": None,
        "payoff_quality": None,
        "call_market_cap": 10,
        "current_market_cap": 12,
        "age_minutes": 1,
    }
    decision = evaluate_v15(Stage.NEW, features)
    assert decision.runner_score == 70
    assert decision.feature_vector == features


def test_research_ranking_is_deterministic_and_frequency_locked():
    rows = [{"mint": mint} for mint in ("c", "a", "b", "d")]
    selected, indexes = _rank(rows, [0.5, 0.5, 0.5, 0.1], 0.5)

    assert [row["mint"] for row in selected] == ["a", "b"]
    assert indexes == [1, 2]
    assert _percentile_scores([0.5, 0.5, 0.1]) == [1.0, 0.5, 0.0]


def test_calibration_reports_brier_ece_and_reliability_without_claiming_perfection():
    result = _calibration([0.1, 0.2, 0.8, 0.9], [False, True, True, False])

    assert result["brier"] == pytest.approx(0.375)
    assert result["ece"] == pytest.approx(0.5)
    assert sum(bucket["sample"] for bucket in result["reliability"]) == 4


def test_v2_recovery_counts_both_recovered_runners_and_new_false_positives():
    rows = [
        {"mint": "a", "peak_multiple": 20},
        {"mint": "b", "peak_multiple": 1},
        {"mint": "c", "peak_multiple": 50},
        {"mint": "d", "peak_multiple": 1},
    ]
    result = IntelligenceV2Experiment._recovery(
        rows,
        [0.9, 0.8, 0.2, 0.1],
        [0.1, 0.8, 0.9, 0.2],
        frequency=0.5,
    )

    assert result["20x"]["control_captured"] == 1
    assert result["20x"]["v2_captured"] == 1
    assert result["20x"]["recovered"] == 1
    assert result["20x"]["lost_vs_control"] == 1
    assert result["new_false_positives"] == 0


def test_baseline_latency_is_explicitly_zero_not_missing():
    rows = [{"mint": "a", "peak_multiple": 2, "terminal_failure": False}]
    result = IntelligenceV2Experiment._baseline("FIXED", rows, [0.5])

    assert result["batch_inference_ms"] == 0.0
    assert result["per_decision_inference_ms"] == 0.0

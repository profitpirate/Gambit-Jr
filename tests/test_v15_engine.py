from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from memecoin_bot.models import DiscoveryEvent, MarketSnapshot, iso
from memecoin_bot.v15_engine import (
    EntryStatus,
    EvidenceState,
    ProvenanceValue,
    SignalTier,
    Stage,
    buyer_trajectory,
    economic_concentration,
    entry_status,
    evaluate_v15,
    independent_alpha_count,
    provider_truth,
    tradeability,
)
from tests.helpers import settings, store, temp_db_path


def complete(stage: Stage, score: float = 85) -> dict:
    from memecoin_bot.v15_engine import STAGE_FEATURES

    return {
        **{name: score for name in STAGE_FEATURES[stage]},
        "call_market_cap": 10_000,
        "current_market_cap": 11_000,
        "age_minutes": 10,
    }


def test_independent_runner_and_failure_policy():
    premium = evaluate_v15(Stage.NEW, complete(Stage.NEW, 90))
    assert premium.signal_tier == SignalTier.PREMIUM
    assert premium.runner_grade == "HIGH" and premium.failure_grade == "LOW"

    risky = complete(Stage.NEW, 90)
    risky.update(buyer_collapse=True, toxic_creator=True)
    decision = evaluate_v15(Stage.NEW, risky)
    assert decision.signal_tier == SignalTier.HIGH_RISK_MOMENTUM
    assert decision.runner_score == premium.runner_score
    assert decision.failure_score >= 50


def test_coverage_unknown_conflict_and_stale_cap_premium():
    low = complete(Stage.NEW, 90)
    low["creator_quality"] = None
    low["buyer_independence"] = None
    assert evaluate_v15(Stage.NEW, low).signal_tier != SignalTier.PREMIUM

    for field, value in (
        ("critical_unknowns", ["SELLABILITY_UNKNOWN"]),
        ("provider_conflicts", ["liquidity"]),
        ("stale_evidence", ["holders"]),
    ):
        features = complete(Stage.NEW, 90)
        features[field] = value
        assert evaluate_v15(Stage.NEW, features).signal_tier != SignalTier.PREMIUM


def test_stage_specific_youth_migration_and_revival_rules():
    young = complete(Stage.NEW, 90)
    young["age_minutes"] = 0.2
    result = evaluate_v15(Stage.NEW, young)
    assert result.setup_conviction == 90 and result.signal_tier == SignalTier.PREMIUM

    migrated = complete(Stage.MIGRATED, 90)
    migrated["tradeability"] = None
    assert "TRADEABILITY_UNKNOWN" in evaluate_v15(Stage.MIGRATED, migrated).critical_unknowns

    revival = complete(Stage.REVIVAL, 90)
    revival["fresh_catalyst"] = None
    assert evaluate_v15(Stage.REVIVAL, revival).signal_tier != SignalTier.CATALYST_REVIVAL
    revival["fresh_catalyst"] = 90
    assert evaluate_v15(Stage.REVIVAL, revival).signal_tier == SignalTier.CATALYST_REVIVAL


def test_entry_open_extended_chasing_and_missing_is_not_zero():
    assert entry_status(10_000, 11_000, 5) == EntryStatus.OPEN
    assert entry_status(10_000, 18_000, 20) == EntryStatus.EXTENDED
    assert entry_status(10_000, 31_000, 20) == EntryStatus.CHASING
    assert entry_status(10_000, 11_000, None) == EntryStatus.UNKNOWN


def test_provider_provenance_conflict_and_freshness():
    now = datetime.now(UTC)
    a = ProvenanceValue(10, "a", now.isoformat())
    b = ProvenanceValue(11, "b", now.isoformat())
    assert provider_truth([a, b], 60)["state"] == EvidenceState.DATA_CONFLICT
    old = ProvenanceValue(10, "a", (now - timedelta(minutes=5)).isoformat())
    assert provider_truth([old], 60)["state"] == EvidenceState.STALE


def test_tradeability_concentration_and_buyer_replacement():
    estimates = tradeability(100_000)
    assert set(estimates["estimates"]) == {"50", "100", "250", "500", "1000"}
    assert estimates["method"] == "constant_product_estimate"
    concentration = economic_concentration(
        [
            {"wallet": "lp", "percent": 30, "excluded_non_economic": True},
            {"wallet": "a", "percent": 8, "cluster_id": "family"},
            {"wallet": "b", "percent": 9, "cluster_id": "family"},
            {"wallet": "c", "percent": 5},
        ]
    )
    assert concentration["raw_top10_percent"] == 22
    assert concentration["effective_actor_concentration"] == 17
    healthy = buyer_trajectory(
        [{"cohort_size": 25, "retained": 12, "fully_exited": 8, "replacement_buyers": 15, "independent_replacements": 7}]
    )
    collapse = buyer_trajectory(
        [{"cohort_size": 25, "retained": 2, "fully_exited": 15, "replacement_buyers": 2, "independent_replacements": 1}]
    )
    assert healthy["state"] == "HEALTHY_REPLACEMENT"
    assert collapse["state"] == "BUYER_COLLAPSE"


def test_linked_alpha_wallets_count_once_independent_clusters_count_separately():
    wallets = [
        {"wallet": "a", "family_id": "one", "empirical_alpha": True},
        {"wallet": "b", "family_id": "one", "empirical_alpha": True},
        {"wallet": "c", "family_id": "two", "empirical_alpha": True},
        {"wallet": "bot", "family_id": "three", "empirical_alpha": True, "bot_or_mayhem": True},
    ]
    assert independent_alpha_count(wallets) == 2


def test_immutable_t0_database_boundary():
    with temp_db_path() as path:
        db = store(path)
        token_id, _ = db.upsert_discovery(DiscoveryEvent(token_address="immutable"))
        candidate_id, _ = db.ensure_candidate(token_id, iso(), "v1.5")
        decision = evaluate_v15(Stage.NEW, complete(Stage.NEW, 90))
        market = MarketSnapshot(
            "immutable", iso(), "fixture", market_cap_usd=10_000, price_usd=0.1, liquidity_usd=20_000
        )
        assert db.record_v15_decision(candidate_id, decision, settings(path), "immutable", "solana", market)
        row = db.conn.execute("SELECT * FROM v15_t0_calls").fetchone()
        assert row and row["runner_score"] == 90
        with pytest.raises(sqlite3.IntegrityError), db.conn:
            db.conn.execute("UPDATE v15_t0_calls SET runner_score=0")
        db.close()


def test_no_lookahead_feature_contract():
    features = complete(Stage.NEW, 80)
    features["future_ath"] = 100
    features["future_rug"] = True
    baseline = evaluate_v15(Stage.NEW, complete(Stage.NEW, 80))
    result = evaluate_v15(Stage.NEW, features)
    assert result.runner_score == baseline.runner_score
    assert result.failure_score == baseline.failure_score


def test_v15_migration_restart_and_launch_cursor_persist():
    with temp_db_path() as path:
        db = store(path)
        db.save_launch_cursor("bsc_direct_launch", "123", {"backfill": True})
        assert db.launch_cursor("bsc_direct_launch") == "123"
        db.close()
        restarted = store(path)
        assert restarted.launch_cursor("bsc_direct_launch") == "123"
        versions = {
            row[0] for row in restarted.conn.execute("SELECT version FROM schema_migrations")
        }
        assert {"007_v14_hardening.sql", "008_v15_decision_core.sql"} <= versions
        restarted.close()

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from memecoin_bot.database import Store
from memecoin_bot.models import DiscoveryEvent
from memecoin_bot.realtime.thesis import RunnerThesisEngine

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


@pytest.fixture
def thesis_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "thesis.db", Path("migrations"))
    store.migrate()
    yield store
    store.close()


def _token(store: Store) -> int:
    token_id, _ = store.upsert_discovery(
        DiscoveryEvent(
            token_address="RunnerThesis111",
            chain="solana",
            source="native",
            discovered_at=NOW.isoformat(),
            estimated_creation_timestamp=NOW.isoformat(),
        )
    )
    return token_id


def _feature(*, strong: bool = True, risk: float = 0.04) -> dict:
    multiplier = 1 if strong else -1
    return {
        "feature_version": "realtime-trajectory-v1",
        "decision_timestamp": NOW.isoformat(),
        "token_age_seconds": 25,
        "migration_state": "PRE_MIGRATION",
        "capital_trajectory": {
            "real_sol_velocity": 0.12 * multiplier,
            "real_sol_acceleration": 0.012 * multiplier,
            "capital_persistence": 1 if strong else 0,
            "curve_progress_velocity": 0.03 * multiplier,
            "capital_reversal": not strong,
        },
        "buyer_arrival": {
            "new_buyers_per_second": 0.35 if strong else 0.005,
            "independent_new_buyers_per_second": 0.28 if strong else 0.002,
            "buyer_retention": 0.8 if strong else 0.05,
            "buyer_replacement": 8 if strong else 0,
            "buyer_deceleration_observed": not strong,
            "creator_buyer_share": risk,
        },
        "first_sell": {
            "sell_absorption_rate": 3 if strong else 0.05,
            "buyers_after_first_meaningful_sell": 9 if strong else 0,
            "first_sell_absorbed": strong,
        },
        "activity_adjustment": {
            "wash_probability": risk,
            "linked_wallet_share": risk,
            "bundle_linked_share": risk,
            "adjusted_volume_sol": 12 if strong else 0.1,
        },
        "migration_continuity": {
            "flow_survival": None,
            "buyer_retention": None,
            "liquidity_continuity": None,
        },
        "actor_intelligence": {
            "wallet_consensus": {
                "independent_smart_wallet_count": 3 if strong else 0,
                "linked_wallet_share": risk,
            },
            "funder": {
                "funder_independence": 0.95 if strong else 0.1,
                "creator_link_score": risk,
            },
        },
        "coverage": {
            "trade_events": True,
            "curve_observations": True,
            "real_sol_reserve": True,
            "buyer_identity": True,
            "wallet_linkage": True,
            "creator": True,
            "funder": True,
            "bundle": True,
            "first_sell": True,
            "migration": False,
            "provider_timestamps": True,
        },
        "monitoring": {"state": "GENESIS"},
    }


def test_runner_failure_and_actionability_are_independent_probabilities(
    thesis_store: Store,
) -> None:
    token_id = _token(thesis_store)
    thesis = RunnerThesisEngine(thesis_store).evaluate(
        token_id, NOW.isoformat(), _feature(), entry_market_cap=45_000, entry_price=0.0001
    )
    assert thesis.runner_probability > 0.7
    assert thesis.failure_probability < 0.35
    assert thesis.actionable_probability > 0.55
    assert thesis.runner_probability + thesis.failure_probability != pytest.approx(1)
    assert thesis.public_route is False
    assert thesis.state == "CONFIRMED"
    assert thesis.call_readiness == "RECONFIRM"
    assert thesis.supporting_evidence
    assert thesis.invalidation_conditions
    assert thesis_store.conn.execute("SELECT SUM(public_route) FROM runner_theses_v15").fetchone()[0] == 0


def test_rapid_reconfirmation_freezes_one_immutable_shadow_call(
    thesis_store: Store,
) -> None:
    token_id = _token(thesis_store)
    engine = RunnerThesisEngine(thesis_store)
    first = engine.evaluate(token_id, NOW.isoformat(), _feature())
    second_time = (NOW + timedelta(seconds=8)).isoformat()
    second = engine.evaluate(token_id, second_time, _feature())
    third = engine.evaluate(token_id, (NOW + timedelta(seconds=12)).isoformat(), _feature())
    assert first.state == "CONFIRMED"
    assert second.state == "CALL_READY"
    assert second.call_readiness == "SHADOW_CALL_READY"
    assert third.state == "CALLED"
    row = thesis_store.conn.execute("SELECT * FROM prospective_shadow_calls_v15").fetchone()
    assert row["thesis_id"] == second.thesis_id
    assert row["public_route"] == 0
    assert thesis_store.conn.execute("SELECT COUNT(*) FROM prospective_shadow_calls_v15").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        thesis_store.conn.execute(
            "UPDATE prospective_shadow_calls_v15 SET tier='PUBLIC' WHERE shadow_call_id=?",
            (row["shadow_call_id"],),
        )


def test_matured_shadow_outcome_reflects_once_and_becomes_a_time_safe_analogue(
    thesis_store: Store,
) -> None:
    token_id = _token(thesis_store)
    engine = RunnerThesisEngine(thesis_store)
    engine.evaluate(token_id, NOW.isoformat(), _feature())
    frozen_at = (NOW + timedelta(seconds=8)).isoformat()
    engine.evaluate(token_id, frozen_at, _feature())
    call = thesis_store.conn.execute("SELECT * FROM prospective_shadow_calls_v15").fetchone()
    with pytest.raises(ValueError, match="follow"):
        engine.settle_shadow_call(
            call["shadow_call_id"],
            outcome_available_at=frozen_at,
            peak_multiple=3,
            maximum_adverse_excursion=-0.2,
            terminal_failure=False,
            time_to_2x_seconds=240,
            evidence={"source": "fixed-horizon-fixture"},
        )
    matured_at = (NOW + timedelta(hours=24)).isoformat()
    result = engine.settle_shadow_call(
        call["shadow_call_id"],
        outcome_available_at=matured_at,
        peak_multiple=3,
        maximum_adverse_excursion=-0.2,
        terminal_failure=False,
        time_to_2x_seconds=240,
        evidence={"source": "fixed-horizon-fixture"},
    )
    assert result["error_class"] == "TRUE_RUNNER"
    assert engine.settle_shadow_call(
        call["shadow_call_id"],
        outcome_available_at=matured_at,
        peak_multiple=3,
        maximum_adverse_excursion=-0.2,
        terminal_failure=False,
        time_to_2x_seconds=240,
        evidence={"source": "fixed-horizon-fixture-retry"},
    )["reflection_id"] == result["reflection_id"]
    with pytest.raises(ValueError, match="conflicts"):
        engine.settle_shadow_call(
            call["shadow_call_id"],
            outcome_available_at=matured_at,
            peak_multiple=99,
            maximum_adverse_excursion=-0.2,
            terminal_failure=False,
            time_to_2x_seconds=240,
            evidence={"source": "conflicting-retry"},
        )
    assert engine.shadow_scorecard()["2x_precision"] == 1
    assert thesis_store.conn.execute("SELECT COUNT(*) FROM runner_reflections_v15").fetchone()[0] == 1
    assert thesis_store.conn.execute("SELECT COUNT(*) FROM runner_analogue_memory_v15").fetchone()[0] == 1
    vector = RunnerThesisEngine._feature_vector(_feature())
    assert engine.analogues("solana", "LAUNCH", frozen_at, vector, call["thesis_type"]) == {
        "successes": [],
        "failures": [],
    }
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        thesis_store.conn.execute(
            "UPDATE prospective_shadow_outcomes_v15 SET peak_multiple=99 "
            "WHERE shadow_call_id=?",
            (call["shadow_call_id"],),
        )


def test_analogue_lookup_cannot_see_future_outcomes(thesis_store: Store) -> None:
    token_id = _token(thesis_store)
    engine = RunnerThesisEngine(thesis_store)
    vector = RunnerThesisEngine._feature_vector(_feature())
    common = {
        name: value for name, value in vector.items() if name in sorted(vector)[:8]
    }
    engine.record_analogue(
        entity_key="past-success",
        chain="solana",
        thesis_type="ORGANIC_ACCELERATION",
        stage="LAUNCH",
        regime="BULL",
        decision_timestamp=(NOW - timedelta(days=10)).isoformat(),
        outcome_available_at=(NOW - timedelta(days=3)).isoformat(),
        features=common,
        peak_multiple=8,
        terminal_failure=False,
        actionable_at_decision=True,
        entry_market_cap=30_000,
        maximum_adverse_excursion=-0.2,
        time_to_2x_seconds=600,
        source_dataset="real-history",
        evidence={"checksum": "verified"},
    )
    engine.record_analogue(
        entity_key="future-failure",
        chain="solana",
        thesis_type="ORGANIC_ACCELERATION",
        stage="LAUNCH",
        regime="BULL",
        decision_timestamp=(NOW - timedelta(days=1)).isoformat(),
        outcome_available_at=(NOW + timedelta(days=2)).isoformat(),
        features=common,
        peak_multiple=0.1,
        terminal_failure=True,
        actionable_at_decision=True,
        entry_market_cap=30_000,
        maximum_adverse_excursion=-0.95,
        time_to_2x_seconds=None,
        source_dataset="real-history",
        evidence={"checksum": "verified"},
    )
    thesis = engine.evaluate(token_id, NOW.isoformat(), _feature())
    assert [row["entity_key"] for row in thesis.analogous_successes] == ["past-success"]
    assert thesis.analogous_failures == []


def test_contradictory_sequence_weakens_then_invalidates_thesis(
    thesis_store: Store,
) -> None:
    token_id = _token(thesis_store)
    engine = RunnerThesisEngine(thesis_store)
    engine.evaluate(token_id, NOW.isoformat(), _feature())
    weakened = engine.evaluate(
        token_id,
        (NOW + timedelta(seconds=10)).isoformat(),
        _feature(strong=False, risk=0.35),
    )
    invalidated = engine.evaluate(
        token_id,
        (NOW + timedelta(seconds=20)).isoformat(),
        _feature(strong=False, risk=0.95),
    )
    assert weakened.state in {"WEAKENING", "INVALIDATED"}
    assert invalidated.state == "INVALIDATED"
    assert invalidated.failure_probability > invalidated.runner_probability
    assert invalidated.contradictory_evidence


def test_analogue_requires_outcome_to_follow_decision(thesis_store: Store) -> None:
    engine = RunnerThesisEngine(thesis_store)
    with pytest.raises(ValueError, match="follow"):
        engine.record_analogue(
            entity_key="bad",
            chain="solana",
            thesis_type="ORGANIC_ACCELERATION",
            stage="LAUNCH",
            regime="UNKNOWN",
            decision_timestamp=NOW.isoformat(),
            outcome_available_at=NOW.isoformat(),
            features={"x": 1.0},
            peak_multiple=2,
            terminal_failure=False,
            actionable_at_decision=True,
            entry_market_cap=None,
            maximum_adverse_excursion=None,
            time_to_2x_seconds=None,
            source_dataset="invalid",
            evidence={},
        )

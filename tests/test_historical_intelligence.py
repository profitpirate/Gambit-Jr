from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from memecoin_bot.historical import (
    ApprovedFeatureStore,
    BackfillEngine,
    BackfillPage,
    HistoricalContextReader,
    HistoricalWarehouse,
    RawEvidence,
    ResearchEngine,
    actor_clusters,
    buyer_quality,
    creator_reputation,
    empirical_wallet_reputation,
    fingerprint_similarity,
    funding_relationship,
    hierarchical_prior,
)
from memecoin_bot.historical.research import LeakageError

T0 = "2021-01-01T00:00:00+00:00"
T1 = "2021-01-01T00:01:00+00:00"
T2 = "2021-01-01T00:02:00+00:00"
T3 = "2021-01-01T00:03:00+00:00"


def dataset() -> dict:
    return {
        "dataset_id": "sol-launches-v1",
        "dataset_version": "2021-2026-v1",
        "provider": "fixture-provider",
        "chain": "solana",
        "acquisition_method": "paginated_api",
        "refresh_method": "checkpointed_backfill",
        "timestamp_precision": "second",
        "reliability": "FIXTURE",
        "history_kind": "TRUE_HISTORICAL",
        "point_in_time_safe": True,
        "estimated_completeness": 1.0,
    }


def evidence(entity: str = "Token111", timestamp: str = T0) -> RawEvidence:
    return RawEvidence(
        dataset_id="sol-launches-v1",
        provider="fixture-provider",
        chain="solana",
        entity_type="token",
        entity_id=entity,
        source_timestamp=timestamp,
        availability_timestamp=timestamp,
        endpoint_type="launch_event",
        payload={"token": entity, "timestamp": timestamp},
        schema_version="v1",
        acquisition_version="v1",
        provenance={"source": "test fixture"},
    )


@pytest.fixture
def warehouse(tmp_path):
    value = HistoricalWarehouse(tmp_path / "warehouse" / "history.db", tmp_path / "archive")
    value.register_dataset(dataset())
    try:
        yield value
    finally:
        value.close()


def test_raw_archive_is_content_addressed_deduplicated_and_immutable(warehouse):
    evidence_id, inserted = warehouse.ingest_raw(evidence())
    duplicate_id, duplicate = warehouse.ingest_raw(evidence())
    assert inserted is True and duplicate is False and duplicate_id == evidence_id
    coverage = warehouse.coverage_map()[0]
    assert coverage["earliest_timestamp"] == T0
    assert coverage["entity_count"] == 1
    assert coverage["observation_count"] == 1
    archive_file = warehouse.archive.root / warehouse.conn.execute(
        "SELECT archive_path FROM raw_evidence"
    ).fetchone()[0]
    assert archive_file.exists()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        warehouse.conn.execute(
            "UPDATE raw_evidence SET quality_state='BAD' WHERE evidence_id=?", (evidence_id,)
        )


def test_point_in_time_features_exclude_future_availability(warehouse):
    evidence_id, _ = warehouse.ingest_raw(evidence())
    entity_key = warehouse.upsert_entity("token", "solana", "Token111", T0)
    event_id = warehouse.normalize_event(
        evidence_id, "2021-2026-v1", entity_key, "LAUNCH", T0, T0, {"verified": True}
    )
    warehouse.write_feature(
        dataset_version="2021-2026-v1",
        feature_version="features-v1",
        entity_key=entity_key,
        feature_name="creator_quality",
        value=30,
        observed_at=T0,
        available_at=T0,
        source_event_ids=[event_id],
    )
    warehouse.write_feature(
        dataset_version="2021-2026-v1",
        feature_version="features-v1",
        entity_key=entity_key,
        feature_name="creator_quality",
        value=99,
        observed_at=T1,
        available_at=T3,
        source_event_ids=[event_id],
    )
    assert warehouse.features_at(entity_key, T2, "features-v1")["creator_quality"]["value"] == 30
    assert warehouse.features_at(entity_key, T3, "features-v1")["creator_quality"]["value"] == 99


def test_timestamp_contract_rejects_reconstructed_future_data(warehouse):
    with pytest.raises(ValueError, match="cannot precede"):
        warehouse.ingest_raw(replace(evidence(), availability_timestamp="2020-12-31T23:59:00+00:00"))


class TwoPageProvider:
    name = "fixture-provider"
    dataset_id = "sol-launches-v1"

    async def fetch_page(self, cursor):
        if cursor is None:
            return BackfillPage([evidence("Token111", T0)], {"page": 2}, 1)
        return BackfillPage([evidence("Token222", T1)], None, 0)


@pytest.mark.asyncio
async def test_backfill_is_checkpointed_restart_safe_and_idempotent(warehouse):
    engine = BackfillEngine(warehouse)
    first = await engine.run(TwoPageProvider(), maximum_pages=1)
    assert first["state"] == "RUNNING"
    assert first["records_ingested"] == 1
    completed = await engine.run(TwoPageProvider(), job_id=first["job_id"])
    assert completed["state"] == "COMPLETE"
    assert completed["records_ingested"] == 2
    rerun = await engine.run(TwoPageProvider())
    assert rerun["records_ingested"] == 0
    assert warehouse.coverage_map()[0]["observation_count"] == 2


def test_outcomes_are_available_only_after_measurement(warehouse):
    entity_key = warehouse.upsert_entity("token", "solana", "Token111", T0)
    with pytest.raises(ValueError, match="available after measurement"):
        warehouse.record_outcome(
            {
                "dataset_version": "2021-2026-v1",
                "outcome_version": "outcomes-v1",
                "entity_key": entity_key,
                "decision_at": T0,
                "measurement_end_at": T2,
                "available_at": T1,
                "peak_multiple": 10,
            }
        )
    outcome_id = warehouse.record_outcome(
        {
            "dataset_version": "2021-2026-v1",
            "outcome_version": "outcomes-v1",
            "entity_key": entity_key,
            "decision_at": T0,
            "measurement_end_at": T2,
            "available_at": T2,
            "peak_multiple": 10,
        }
    )
    assert outcome_id
    assert warehouse.conn.execute("SELECT class_name FROM outcomes").fetchone()[0] == "10X"


def test_research_leakage_and_chronology_fail_closed():
    with pytest.raises(LeakageError, match="future information"):
        ResearchEngine.assert_point_in_time(
            [
                {
                    "feature_name": "future_ath",
                    "observed_at": T0,
                    "available_at": T3,
                    "decision_at": T2,
                }
            ]
        )
    with pytest.raises(LeakageError, match="strictly chronological"):
        ResearchEngine.validate_windows((T0, T2), (T1, T2), (T2, T3))


def test_walk_forward_research_is_catalogued_and_machine_readable(warehouse):
    observations = [
        ("TrainToken", "2021-01-10T00:00:00+00:00", 10),
        ("ValidationToken", "2021-02-10T00:00:00+00:00", 1),
        ("TestToken", "2021-03-10T00:00:00+00:00", 6),
    ]
    for token, decision_at, peak in observations:
        entity_key = warehouse.upsert_entity("token", "solana", token, decision_at)
        warehouse.write_feature(
            dataset_version="2021-2026-v1",
            feature_version="features-v1",
            entity_key=entity_key,
            feature_name="liquidity_usd",
            value=1000 * peak,
            observed_at=decision_at,
            available_at=decision_at,
            source_event_ids=[],
        )
        measurement = decision_at.replace("T00:00:00", "T01:00:00")
        warehouse.record_outcome(
            {
                "dataset_version": "2021-2026-v1",
                "outcome_version": "outcomes-v1",
                "entity_key": entity_key,
                "decision_at": decision_at,
                "measurement_end_at": measurement,
                "available_at": measurement,
                "peak_multiple": peak,
            }
        )
    result = ResearchEngine(warehouse).run_walk_forward(
        research_type="RUNNER_FINGERPRINT_REPORT",
        dataset_version="2021-2026-v1",
        feature_version="features-v1",
        outcome_version="outcomes-v1",
        rules_version="rules-v1",
        code_version="test",
        provider_set=["fixture-provider"],
        train=("2021-01-01T00:00:00+00:00", "2021-02-01T00:00:00+00:00"),
        validation=("2021-02-01T00:00:00+00:00", "2021-03-01T00:00:00+00:00"),
        test=("2021-03-01T00:00:00+00:00", "2021-04-01T00:00:00+00:00"),
    )
    assert result["metrics"]["test"]["sample"] == 1
    catalogued = warehouse.conn.execute(
        "SELECT leakage_state,result_json FROM research_runs WHERE research_run_id=?",
        (result["research_run_id"],),
    ).fetchone()
    assert catalogued["leakage_state"] == "PASS"
    assert "baselines" in catalogued["result_json"]


def approved_record() -> dict:
    return {
        "feature_name": "historical_creator_prior",
        "feature_version": "production-v1",
        "target_stage": "NEW",
        "target_feature": "creator_quality",
        "research_run_id": "research-1",
        "research_evidence": {"5x_lift": 0.08},
        "sample_size": 1000,
        "walk_forward": {"windows": 4, "state": "PASS"},
        "approved_by": "operator@example.invalid",
        "merge_policy": "BOUNDED_BLEND",
        "max_contribution": 0.2,
        "limitations": ["descriptive evidence"],
    }


def test_only_manually_approved_features_reach_bounded_live_context(tmp_path):
    store = ApprovedFeatureStore(tmp_path / "production" / "features.db")
    try:
        with pytest.raises(ValueError, match="approver"):
            store.approve(approved_record() | {"approved_by": ""})
        store.approve(approved_record())
        store.publish_snapshot(
            chain="solana",
            entity_id="Token111",
            feature_name="historical_creator_prior",
            feature_version="production-v1",
            value=100,
            observed_at=T0,
            available_at=T1,
            source_research_run_id="research-1",
            provenance={"dataset_version": "2021-2026-v1"},
        )
        reader = HistoricalContextReader(store, latency_budget_ms=1000)
        before = reader.apply("solana", "Token111", T0, "NEW", {"creator_quality": 50})
        assert before["creator_quality"] == 50
        assert before["historical_context"]["state"] == "EMPTY"
        after = reader.apply("solana", "Token111", T2, "NEW", {"creator_quality": 50})
        assert after["creator_quality"] == 60
        assert after["historical_context"]["state"] == "APPLIED"
    finally:
        store.close()


def test_wallet_and_creator_memory_are_point_in_time_and_sample_aware():
    future = {
        "available_at": T3,
        "matured": True,
        "peak_multiple": 20,
        "entry_age_minutes": 2,
        "survival_hours": 100,
    }
    past = {
        "available_at": T1,
        "matured": True,
        "peak_multiple": 1,
        "entry_age_minutes": 20,
        "survival_hours": 30,
    }
    wallet = empirical_wallet_reputation([past, future], T2)
    creator = creator_reputation([past, future], T2)
    assert wallet["sample"] == 1 and wallet["grade"] != "PROVEN"
    assert creator["sample"] == 1 and creator["runner_count"] == 0


def test_missing_aware_fingerprints_buyers_graphs_and_hierarchical_priors():
    match = fingerprint_similarity(
        {"liquidity": 90, "unknown": None},
        {
            "liquidity": {"median": 100, "scale": 50},
            "unknown": {"median": 20, "scale": 5},
        },
        {"liquidity": 0.7, "unknown": 0.3},
    )
    assert match == {
        "state": "KNOWN",
        "score": 80.0,
        "coverage": 70.0,
        "features": ["liquidity"],
    }
    assert buyer_quality(None)["state"] == "UNKNOWN"
    assert buyer_quality(
        {
            "cohort_size": 10,
            "independent_buyers": 9,
            "retained_buyers": 8,
            "independent_alpha_families": 4,
            "connected_actor_percent": 5,
        }
    )["state"] == "HIGH"
    assert funding_relationship({"common_funder": True})["same_owner"] == "UNKNOWN"
    clusters = actor_clusters(
        [
            {"source": "A", "target": "B", "available_at": T1},
            {"source": "B", "target": "C", "available_at": T3},
        ],
        T2,
    )
    assert clusters[0]["members"] == ["A", "B"]
    assert hierarchical_prior(50, 80, 100)["value"] == 84.0

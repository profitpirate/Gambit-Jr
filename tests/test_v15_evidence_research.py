from __future__ import annotations

import hashlib

import pytest

from memecoin_bot.historical.evidence_research import (
    ALL_LAUNCH_FEATURES,
    WALK_FORWARDS,
    storage_projection,
    verify_corpora,
)
from memecoin_bot.historical.store import HistoricalWarehouse, RawEvidence

T0 = "2026-01-01T00:00:00+00:00"


def _dataset() -> dict:
    return {
        "dataset_id": "batch-evidence",
        "dataset_version": "v1",
        "provider": "fixture",
        "chain": "solana",
        "acquisition_method": "fixture",
        "refresh_method": "immutable",
        "timestamp_precision": "second",
        "reliability": "FIXTURE",
        "history_kind": "TRUE_HISTORICAL",
        "point_in_time_safe": True,
    }


def test_batch_events_preserve_entities_and_payloads_at_the_same_timestamp(tmp_path):
    warehouse = HistoricalWarehouse(tmp_path / "warehouse.db", tmp_path / "archive")
    try:
        warehouse.register_dataset(_dataset())
        evidence_id, _ = warehouse.ingest_raw(
            RawEvidence(
                dataset_id="batch-evidence",
                provider="fixture",
                chain="solana",
                entity_type="dataset_file",
                entity_id="events.parquet",
                source_timestamp=T0,
                availability_timestamp=T0,
                endpoint_type="fixture",
                payload={"sha256": "fixture"},
                schema_version="v1",
                acquisition_version="v1",
            )
        )
        wallet_a = warehouse.upsert_entity("wallet", "solana", "wallet-a", T0)
        wallet_b = warehouse.upsert_entity("wallet", "solana", "wallet-b", T0)

        first = warehouse.normalize_event(
            evidence_id, "v1", wallet_a, "ENTRY", T0, T0, {"mint": "mint-a"}
        )
        duplicate = warehouse.normalize_event(
            evidence_id, "v1", wallet_a, "ENTRY", T0, T0, {"mint": "mint-a"}
        )
        second_payload = warehouse.normalize_event(
            evidence_id, "v1", wallet_a, "ENTRY", T0, T0, {"mint": "mint-b"}
        )
        second_entity = warehouse.normalize_event(
            evidence_id, "v1", wallet_b, "ENTRY", T0, T0, {"mint": "mint-a"}
        )

        assert first == duplicate
        assert len({first, second_payload, second_entity}) == 3
        assert warehouse.conn.execute("SELECT COUNT(*) FROM normalized_events").fetchone()[0] == 3
    finally:
        warehouse.close()


def test_public_corpus_verification_is_checksum_fail_closed(tmp_path, monkeypatch):
    from memecoin_bot.historical import evidence_research

    payload = b"verified corpus row"
    digest = hashlib.sha256(payload).hexdigest()
    sources = {
        "TRENCHES_FILES": ("trenches-pumpfun-forward-2026-08", {"a.bin": digest}),
        "MELT_FILES": ("MELT", {"b.bin": digest}),
        "LAUNCH_CORPUS_FILES": ("Pumpfun_Memecoin_Corpus", {"c.bin": digest}),
    }
    for constant, (directory, files) in sources.items():
        monkeypatch.setattr(evidence_research, constant, files)
        target = tmp_path / directory
        target.mkdir()
        (target / next(iter(files))).write_bytes(payload)

    assert all(row["verified"] for row in verify_corpora(tmp_path).values())
    (tmp_path / "MELT" / "b.bin").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum verification"):
        verify_corpora(tmp_path)


def test_walk_forward_windows_are_strictly_chronological_and_embargoed():
    for windows in WALK_FORWARDS.values():
        train_start, train_end = windows["train"]
        validation_start, validation_end = windows["validation"]
        test_start, test_end = windows["test"]
        assert train_start < train_end < validation_start < validation_end
        assert validation_end < test_start < test_end


def test_research_features_exclude_known_future_and_corrupt_fields():
    forbidden = {
        "entry_price_20s_usd",
        "entry_price_30s_usd",
        "entry_price_1m_usd",
        "graduated_at",
        "peak_market_cap_sol",
        "trade_count",
        "initial_top10_pct",
    }
    assert forbidden.isdisjoint(ALL_LAUNCH_FEATURES)
    assert "initial_top10_pct_corrected" in ALL_LAUNCH_FEATURES


def test_storage_projection_uses_only_selected_verified_launch_files():
    projection = storage_projection(
        {
            "verification": {"launch_corpus": {"bytes": 480}},
            "summaries": {"launch_universe": {"launches": 2}},
        }
    )
    assert projection["observed_bytes_per_launch_selected_files"] == 240
    assert projection["estimated_100k_launches_bytes"] == 24_000_000

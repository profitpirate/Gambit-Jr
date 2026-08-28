from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from memecoin_bot.historical import (
    BackfillEngine,
    ChallengerPolicy,
    HistoricalWarehouse,
    ShadowChallenger,
)
from memecoin_bot.historical.finalization import _chronological_windows
from memecoin_bot.historical.providers import (
    BinanceKlineProvider,
    BirdeyeOhlcvProvider,
    DuneQueryProvider,
    GeckoTerminalPoolProvider,
    OperationalHistoryProvider,
)
from memecoin_bot.signals import format_discord_event


def manifest(dataset_id: str, provider: str = "test") -> dict:
    return {
        "dataset_id": dataset_id,
        "dataset_version": "v1",
        "provider": provider,
        "chain": "multi",
        "acquisition_method": "test",
        "refresh_method": "cursor",
        "timestamp_precision": "second",
        "reliability": "TEST",
        "history_kind": "TRUE_HISTORICAL",
        "point_in_time_safe": True,
    }


@pytest.mark.asyncio
async def test_binance_provider_uses_candle_close_as_availability():
    rows = [
        [
            1_672_531_200_000,
            "10",
            "12",
            "9",
            "11",
            "100",
            1_672_617_599_999,
            "1100",
            42,
            "55",
            "600",
            "0",
        ]
    ]

    async def fetch(_url: str):
        return rows

    provider = BinanceKlineProvider(
        "BTCUSDT",
        "2023-01-01T00:00:00+00:00",
        "2023-01-02T00:00:00+00:00",
        fetch_json=fetch,
    )
    page = await provider.fetch_page(None)
    evidence = page.records[0]
    assert evidence.source_timestamp.startswith("2023-01-01T00:00:00")
    assert evidence.availability_timestamp.startswith("2023-01-01T23:59:59")
    assert evidence.source_timestamp < evidence.availability_timestamp


@pytest.mark.asyncio
async def test_live_pool_snapshot_is_not_backdated_to_pool_creation():
    async def fetch(_url: str):
        return {
            "data": [
                {
                    "id": "solana_pool",
                    "attributes": {
                        "address": "pool",
                        "pool_created_at": "2021-01-01T00:00:00Z",
                        "reserve_in_usd": "1000",
                    },
                }
            ]
        }

    provider = GeckoTerminalPoolProvider(
        "solana", maximum_pages=1, fetch_json=fetch
    )
    page = await provider.fetch_page(None)
    record = page.records[0]
    assert record.source_timestamp.startswith("20")
    assert not record.source_timestamp.startswith("2021")
    assert record.provenance["pool_created_at_is_metadata_not_observation_time"] is True


@pytest.mark.asyncio
async def test_credentialed_adapters_require_keys_and_preserve_availability_contract():
    with pytest.raises(ValueError, match="DUNE_API_KEY"):
        DuneQueryProvider(
            1,
            None,
            chain="solana",
            entity_field="token",
            observed_at_field="block_time",
        )
    with pytest.raises(ValueError, match="BIRDEYE_API_KEY"):
        BirdeyeOhlcvProvider(
            ["token"],
            "2025-01-01T00:00:00+00:00",
            "2025-01-02T00:00:00+00:00",
            None,
        )

    async def dune_fetch(_url: str):
        return {
            "result": {
                "rows": [{"token": "T", "block_time": "2025-01-01T00:00:00+00:00"}],
                "metadata": {"total_row_count": 1},
            }
        }

    dune = DuneQueryProvider(
        42,
        None,
        chain="solana",
        entity_field="token",
        observed_at_field="block_time",
        fetch_json=dune_fetch,
    )
    page = await dune.fetch_page(None)
    assert page.records[0].availability_timestamp > page.records[0].source_timestamp
    assert page.records[0].provenance["availability_from_source"] is False


@pytest.mark.asyncio
async def test_operational_import_is_allowlisted_read_only_and_idempotent(tmp_path):
    source = tmp_path / "operational.db"
    connection = sqlite3.connect(source)
    connection.executescript(
        "CREATE TABLE token_snapshots(id INTEGER PRIMARY KEY,token_id INTEGER,captured_at TEXT,price REAL);"
        "CREATE TABLE guild_settings(id INTEGER PRIMARY KEY,discord_token TEXT,created_at TEXT);"
        "INSERT INTO token_snapshots VALUES(1,7,'2025-01-01T00:00:00+00:00',1.2);"
        "INSERT INTO guild_settings VALUES(1,'must-not-import','2025-01-01T00:00:00+00:00');"
    )
    connection.commit()
    connection.close()
    warehouse = HistoricalWarehouse(tmp_path / "warehouse.db", tmp_path / "archive")
    warehouse.register_dataset(manifest("gambit-jr-operational-evidence"))
    try:
        provider = OperationalHistoryProvider(source)
        first = await BackfillEngine(warehouse).run(provider)
        second = await BackfillEngine(warehouse).run(provider)
        assert first["records_ingested"] == 1
        assert second["records_ingested"] == 0
        endpoints = {
            row[0] for row in warehouse.conn.execute("SELECT endpoint_type FROM raw_evidence")
        }
        assert endpoints == {"operational_table:token_snapshots"}
        assert "must-not-import" not in "".join(
            path.read_text(encoding="utf-8") for path in (tmp_path / "archive").rglob("*.json")
        )
    finally:
        warehouse.close()


def test_coverage_operator_surface_and_negative_feature_decision(tmp_path):
    warehouse = HistoricalWarehouse(tmp_path / "warehouse.db", tmp_path / "archive")
    warehouse.register_dataset(manifest("real-public"))
    try:
        warehouse.assess_coverage(
            "real-public",
            {
                "launch_platform": "test platform",
                "normalized_rows": 12,
                "missing_ranges": ["before observation"],
                "completeness_estimate": 0.1,
                "point_in_time_safe": True,
                "timestamp_precision": "second",
                "survivorship_bias": "HIGH",
                "quality_state": "REAL_EXPLORATORY",
                "licensing_limitations": "provider terms",
                "cost_class": "FREE",
                "information_gain": "limited",
            },
        )
        warehouse.record_research_decision(
            {
                "feature_name": "candidate",
                "feature_version": "v1",
                "dataset_version": "v1",
                "sample_size": 12,
                "approval_state": "RESEARCH_ONLY",
                "merge_policy": "EXPLANATION_ONLY",
                "leakage_state": "PASS",
                "limitations": ["survivor bias"],
            }
        )
        status = warehouse.operator_status()
        assert status["datasets"][0]["normalized_rows"] == 12
        assert status["research_decisions"][0]["approval_state"] == "RESEARCH_ONLY"
        assert status["raw_archive_bytes"] == 0
    finally:
        warehouse.close()


def test_challenger_is_persisted_and_cannot_route_public_alerts(tmp_path):
    warehouse = HistoricalWarehouse(tmp_path / "warehouse.db", tmp_path / "archive")
    try:
        challenger = ShadowChallenger(warehouse, lambda features: {"score": features["x"] + 1})
        result = challenger.evaluate(
            entity_key="token:1",
            observed_at="2025-01-01T00:00:00+00:00",
            live_version="v1.5",
            live_decision={"score": 1},
            point_in_time_features={"x": 1},
        )
        assert result["challenger"]["public_alert_routed"] is False
        assert challenger.readiness()["state"] == "PROSPECTIVE_EVIDENCE_REQUIRED"
        with pytest.raises(ValueError, match="never route"):
            ShadowChallenger(
                warehouse,
                lambda _features: {},
                ChallengerPolicy(public_alerts=True),
            )
    finally:
        warehouse.close()


def test_realistic_discord_fixture_matrix_is_signals_first_and_safe():
    fixture = Path("fixtures/v15_discord_signal_cases.json")
    cases = json.loads(fixture.read_text(encoding="utf-8"))
    for case in cases:
        message = format_discord_event(case["event_type"], case["payload"])
        embed = message["embeds"][0]
        assert len(embed["title"]) <= 256
        assert all(len(field["value"]) <= 1024 for field in embed["fields"])
        labels = [component["label"] for component in message["components"][0]["components"]]
        assert labels == ["Copy CA", "DexScreener", "Open GMGN", "Solscan" if case["payload"]["chain"] == "solana" else "BscScan", "Watch"]
        if case["event_type"] == "SIGNAL":
            names = [field["name"] for field in embed["fields"]]
            assert names == [
                "Tier",
                "Chain",
                "Contract address",
                "Market cap",
                "Liquidity",
                "Entry",
                "Runner potential",
                "Failure risk",
                "Confidence / evidence coverage",
                "Why now",
                "Historical context",
                "Risks",
            ]


def test_chronological_windows_use_distinct_timestamps_not_repeated_row_quantiles(tmp_path):
    warehouse = HistoricalWarehouse(tmp_path / "warehouse.db", tmp_path / "archive")
    try:
        dates = ["2026-03-04T00:00:00+00:00"] * 7 + [
            "2026-04-24T00:00:00+00:00"
        ] * 2 + ["2026-08-21T00:00:00+00:00"]
        for index, decision in enumerate(dates):
            entity = warehouse.upsert_entity("dex_pool", "solana", f"pool-{index}", decision)
            measured = decision.replace("T00:00:00", "T00:00:01")
            warehouse.record_outcome(
                {
                    "dataset_version": "geckoterminal-ranked-pool-real-v1",
                    "outcome_version": "v15-real-ohlcv-outcomes-v1",
                    "entity_key": entity,
                    "decision_at": decision,
                    "measurement_end_at": measured,
                    "available_at": measured,
                    "peak_multiple": 1,
                }
            )
        windows = _chronological_windows(warehouse)
        assert windows["train"] == (
            "2026-03-04T00:00:00+00:00",
            "2026-04-24T00:00:00+00:00",
        )
        assert windows["validation"] == (
            "2026-04-24T00:00:00+00:00",
            "2026-08-21T00:00:00+00:00",
        )
    finally:
        warehouse.close()


def test_staging_and_release_contracts_are_isolated_and_manual():
    compose = Path("docker-compose.staging.yml").read_text(encoding="utf-8")
    environment = Path(".env.staging.example").read_text(encoding="utf-8")
    release = Path("scripts/release_v15.sh").read_text(encoding="utf-8")
    workflow = Path(".github/workflows/v15-release.yml").read_text(encoding="utf-8")
    assert "name: gambit-jr-v15-staging" in compose
    assert compose.count("/app/staging/") >= 8
    assert "/app/data" not in compose
    assert 'SHADOW_SEND_ALERTS: "false"' in compose
    assert "SHADOW_SEND_ALERTS=false" in environment
    assert "environment: production" in workflow
    assert "workflow_dispatch:" in workflow
    assert "cp --preserve=all data/memecoin.db" in release
    assert release.count("git switch --detach \"$previous_sha\"") == 2

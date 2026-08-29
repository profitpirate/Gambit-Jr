from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import memecoin_bot.convergence.providers as provider_module
from memecoin_bot.convergence.providers import ProviderRegistry, capabilities
from memecoin_bot.convergence.runner import ConvergenceOrchestrator, historical_months
from memecoin_bot.historical.backfill import BackfillEngine
from memecoin_bot.historical.providers import DuneMonthHistoricalProvider
from memecoin_bot.historical.store import HistoricalWarehouse
from memecoin_bot.social.sources import (
    AuthorizedDiscordSocialSource,
    BlueskyJetstreamSocialSource,
    social_events_from_text,
)


@pytest.fixture
def convergence_warehouse(tmp_path):
    warehouse = HistoricalWarehouse(tmp_path / "warehouse.db", tmp_path / "archive")
    try:
        yield warehouse
    finally:
        warehouse.close()


def test_convergence_schema_is_shadow_only_and_has_all_durable_controls(
    convergence_warehouse,
):
    tables = {
        row[0]
        for row in convergence_warehouse.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {
        "convergence_runs",
        "convergence_phases",
        "convergence_artifacts",
        "provider_capabilities_v15",
        "provider_probes_v15",
        "historical_month_coverage_v15",
        "retired_holdouts_v15",
        "audit_findings_v15",
        "daily_convergence_reports_v15",
    } <= tables
    with convergence_warehouse.conn:
        convergence_warehouse.conn.execute(
            "INSERT INTO convergence_runs(run_id,orchestration_version,code_version,state,"
            "public_route,started_at,configuration_json) VALUES('locked','v','v','PENDING',0,?, '{}')",
            (datetime.now(UTC).isoformat(),),
        )
    with pytest.raises(Exception, match="cannot route publicly"), convergence_warehouse.conn:
        convergence_warehouse.conn.execute(
            "UPDATE convergence_runs SET public_route=1 WHERE run_id='locked'"
        )


def test_provider_preflight_is_dated_truthful_and_never_persists_credentials(
    convergence_warehouse,
):
    secret = "secret-marker"
    registry = ProviderRegistry(
        convergence_warehouse,
        {
            "HELIUS_API_KEY": secret,
            "DUNE_API_KEY": "dune-secret-value",
            "DUNE_QUERY_ID": "123",
        },
    )
    preflight = {row["provider"]: row for row in registry.refresh()}
    assert preflight["helius"]["configured"] is True
    assert preflight["pumpportal"]["admission_state"] == "BLOCKED_EXTERNAL_CREDENTIAL"
    assert preflight["x_direct_api"]["admission_state"] == "REJECTED"
    dumped = json.dumps(registry.status())
    assert secret not in dumped
    helius = next(row for row in registry.status() if row["provider"] == "helius")
    assert helius["current_docs_checked_at"] == "2026-08-29"
    assert helius["cost"]["wss_metered"] is True
    assert (
        next(row for row in capabilities() if row.provider == "dexscreener").rate_limit[
            "token_batch_max"
        ]
        == 30
    )


@pytest.mark.asyncio
async def test_credentialed_probe_error_cannot_persist_or_return_secret(
    convergence_warehouse, monkeypatch
):
    secret = "credential-that-must-never-appear"

    async def fail_with_url(*_args, **_kwargs):
        raise ValueError(f"https://provider.invalid/?api-key={secret}")

    monkeypatch.setattr(provider_module, "_request_json", fail_with_url)
    registry = ProviderRegistry(convergence_warehouse, {"HELIUS_API_KEY": secret})
    result = await registry.probe({"helius"})
    assert secret not in json.dumps(result)
    assert "details redacted" in result[0]["errors"][0]
    assert secret not in json.dumps(registry.status())


class FakeDuneClient:
    def __init__(self):
        self.executions = []
        self.pages = []

    async def execute(self, query_id, parameters):
        self.executions.append((query_id, parameters))
        return "execution-1"

    async def wait(self, execution_id):
        assert execution_id == "execution-1"
        return {"state": "QUERY_STATE_COMPLETED"}

    async def results(self, execution_id, offset, limit):
        self.pages.append((execution_id, offset, limit))
        rows = [
            {
                "token_address": "So11111111111111111111111111111111111111112",
                "observed_at": "2024-01-02 03:04:05",
                "creator": "creator-1",
                "tx_id": "tx-1",
                "block_slot": 123,
                "source": "fixture",
            }
        ]
        return {
            "result": {
                "rows": rows if offset == 0 else [],
                "metadata": {"total_row_count": 1},
            }
        }


@pytest.mark.asyncio
async def test_dune_month_provider_executes_reviewed_partition_and_resumes(
    convergence_warehouse,
):
    client = FakeDuneClient()
    provider = DuneMonthHistoricalProvider(
        42,
        "2024-01",
        None,
        client=client,
    )
    convergence_warehouse.register_dataset(
        {
            "dataset_id": provider.dataset_id,
            "dataset_version": "test-v1",
            "provider": provider.name,
            "chain": "solana",
            "acquisition_method": "fixture",
            "refresh_method": "month_partition",
            "timestamp_precision": "block_time",
            "reliability": "FIXTURE",
            "history_kind": "TRUE_HISTORICAL",
            "point_in_time_safe": True,
        }
    )
    result = await BackfillEngine(convergence_warehouse).run(provider)
    assert result["state"] == "COMPLETE"
    assert client.executions == [
        (
            42,
            {
                "month_start": "2024-01-01T00:00:00+00:00",
                "month_end": "2024-02-01T00:00:00+00:00",
            },
        )
    ]
    row = convergence_warehouse.conn.execute("SELECT * FROM raw_evidence").fetchone()
    assert row["source_timestamp"] == "2024-01-02T03:04:05+00:00"
    assert row["availability_timestamp"] > row["source_timestamp"]
    envelope = json.loads((convergence_warehouse.archive.root / row["archive_path"]).read_text())
    assert envelope["provenance"]["partial_results_allowed"] is False


@pytest.mark.asyncio
async def test_convergence_continues_all_independent_phases_when_data_is_blocked(
    convergence_warehouse,
):
    runner = ConvergenceOrchestrator(
        convergence_warehouse,
        environment={},
        code_version="test-head",
    )
    result = await runner.run(live_probes=False)
    states = result["phase_states"]
    assert states["HISTORICAL_ACQUISITION"] == "BLOCKED_EXTERNAL"
    assert states["NORMALIZATION"] == "BLOCKED_EXTERNAL"
    assert states["DATA_QUALITY"] == "PASSED_ENGINEERING"
    assert states["OUTCOMES"] == "AWAITING_MATURITY"
    assert states["AUDIT"] == "PASSED_ENGINEERING"
    assert states["REPORT"] == "PASSED_ENGINEERING"
    assert all(state not in {"PENDING", "RUNNING"} for state in states.values())
    assert result["public_production_ready"] is False
    persisted = convergence_warehouse.conn.execute(
        "SELECT completed_at,public_route FROM convergence_runs WHERE run_id=?",
        (result["run_id"],),
    ).fetchone()
    assert persisted["completed_at"] is not None
    assert persisted["public_route"] == 0


def test_expired_process_lease_is_recovered_without_duplicate_authority(
    convergence_warehouse,
):
    runner = ConvergenceOrchestrator(convergence_warehouse, environment={})
    run_id = runner._resume_or_create(None)
    assert runner._claim(run_id, "HISTORICAL_ACQUISITION")
    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with convergence_warehouse.conn:
        convergence_warehouse.conn.execute(
            "UPDATE convergence_phases SET lease_expires_at=? WHERE run_id=? "
            "AND phase_name='HISTORICAL_ACQUISITION'",
            (expired, run_id),
        )
    replacement = ConvergenceOrchestrator(convergence_warehouse, environment={})
    replacement._recover_expired_leases(run_id)
    row = convergence_warehouse.conn.execute(
        "SELECT state,lease_owner,last_error FROM convergence_phases WHERE run_id=? "
        "AND phase_name='HISTORICAL_ACQUISITION'",
        (run_id,),
    ).fetchone()
    assert row["state"] == "RETRYABLE_FAILURE"
    assert row["lease_owner"] is None
    assert row["last_error"] == "expired worker lease recovered"


def test_hard_killed_worker_is_recovered_by_a_real_restart(tmp_path):
    warehouse_path = tmp_path / "killed-worker.db"
    archive_path = tmp_path / "archive"
    operational_path = tmp_path / "operational.db"
    child = """
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from memecoin_bot.convergence.runner import ConvergenceOrchestrator
from memecoin_bot.database import Store
from memecoin_bot.historical.store import HistoricalWarehouse
from memecoin_bot.realtime import CanonicalEvent, CanonicalEventFabric, CanonicalEventType
from memecoin_bot.realtime.features import RealtimeFeatureProjector

now = datetime.now(UTC)
operational = Store(sys.argv[3], Path("migrations"))
operational.migrate()
fabric = CanonicalEventFabric(operational)
def event(kind, seconds, source_id, payload):
    timestamp = (now + timedelta(seconds=seconds)).isoformat()
    return CanonicalEvent.create(
        kind, "HardKillHot111", "solana", "pumpfun", "process-kill-e2e", timestamp,
        received_timestamp=timestamp, available_timestamp=timestamp,
        transaction_signature=source_id, source_event_id=f"{source_id}:0", payload=payload,
    )
created = event(
    CanonicalEventType.TOKEN_CREATED, 0, "hard-kill-create",
    {"creator": "CreatorHardKill", "bonding_curve": "CurveHardKill", "real_token_reserves": 1000},
)
fabric.publish(created)
token_id, _ = fabric.project(created)
for index in range(14):
    trade = event(
        CanonicalEventType.TOKEN_TRADE, 2 + index * 8, f"hard-kill-buy-{index}",
        {"actor": f"Buyer{index}", "side": "buy", "sol_amount": 0.25},
    )
    fabric.publish(trade)
    fabric.project(trade)
feature = RealtimeFeatureProjector(operational).compute(
    token_id, (now + timedelta(seconds=130)).isoformat()
)
assert feature["monitoring"]["state"] == "HOT"

warehouse = HistoricalWarehouse(sys.argv[1], sys.argv[2])
runner = ConvergenceOrchestrator(warehouse, environment={}, lease_seconds=1)
run_id = runner._resume_or_create(None)
assert runner._claim(run_id, "HISTORICAL_ACQUISITION")
print(f"{run_id}|{token_id}", flush=True)
time.sleep(300)
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path.cwd() / "src")
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            child,
            str(warehouse_path),
            str(archive_path),
            str(operational_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    assert process.stdout is not None
    identifiers = process.stdout.readline().strip()
    assert identifiers
    run_id, token_id = identifiers.split("|", maxsplit=1)
    process.kill()
    process.wait(timeout=10)
    assert process.returncode != 0
    time.sleep(1.2)

    from memecoin_bot.database import Store

    operational = Store(operational_path, Path("migrations"))
    operational.migrate()
    try:
        latest = operational.conn.execute(
            "SELECT feature_json FROM trajectory_feature_snapshots_v15 "
            "WHERE token_id=? ORDER BY available_timestamp DESC LIMIT 1",
            (int(token_id),),
        ).fetchone()
        assert json.loads(latest["feature_json"])["monitoring"]["state"] == "HOT"
        assert operational.conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert operational.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0
        assert operational.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    finally:
        operational.close()

    restarted = HistoricalWarehouse(warehouse_path, archive_path)
    try:
        replacement = ConvergenceOrchestrator(restarted, environment={})
        replacement._recover_expired_leases(run_id)
        row = restarted.conn.execute(
            "SELECT state,lease_owner,last_error,attempt FROM convergence_phases "
            "WHERE run_id=? AND phase_name='HISTORICAL_ACQUISITION'",
            (run_id,),
        ).fetchone()
        assert row["state"] == "RETRYABLE_FAILURE"
        assert row["lease_owner"] is None
        assert row["last_error"] == "expired worker lease recovered"
        assert row["attempt"] == 1
        assert replacement._claim(run_id, "HISTORICAL_ACQUISITION")
    finally:
        restarted.close()


def test_social_plugins_preserve_pit_and_privacy_and_reject_unknown_tokens():
    address = "So11111111111111111111111111111111111111112"
    secret_text = f"watch this CA {address} before it moves"
    events = social_events_from_text(
        secret_text,
        source="fixture",
        platform="discord",
        source_event_id="message-1",
        source_event_at="2026-08-29T12:00:00+00:00",
        received_at="2026-08-29T12:00:01+00:00",
        author_id="author-1",
        channel_id="channel-1",
        known_token=lambda chain, token: chain == "solana" and token == address,
    )
    assert len(events) == 1
    event = events[0]
    assert event.event_type == "SOCIAL_OBSERVATION"
    assert secret_text not in json.dumps(event.to_dict())
    assert event.payload["content_sha256"]
    assert event.payload["author_hash"] != "author-1"

    discord = AuthorizedDiscordSocialSource(
        [123], lambda chain, token: chain == "solana" and token == address
    )
    assert not discord.parse_message(
        message_id=1,
        channel_id=999,
        author_id=1,
        content=secret_text,
        created_at=datetime.now(UTC),
    )
    bluesky = BlueskyJetstreamSocialSource(lambda _chain, _token: False)
    assert not bluesky.parse_message(
        {
            "cursor": "3",
            "payload": {
                "operation": "create",
                "did": "did:plc:test",
                "collection": "app.bsky.feed.post",
                "rkey": "1",
                "time": "2026-08-29T12:00:00+00:00",
                "record": {"text": secret_text},
            },
        },
        "2026-08-29T12:00:01+00:00",
    )
    assert bluesky.cursor == "3"


def test_historical_month_range_is_explicit_and_complete():
    months = historical_months("2024-01", "2026-08")
    assert len(months) == 32
    assert months[0] == "2024-01"
    assert months[-1] == "2026-08"


@pytest.mark.asyncio
async def test_existing_local_history_advances_acquisition_but_not_24_month_gate(
    convergence_warehouse,
):
    convergence_warehouse.register_dataset(
        {
            "dataset_id": "existing-partial",
            "dataset_version": "existing-partial-v1",
            "provider": "fixture",
            "chain": "solana",
            "acquisition_method": "fixture",
            "refresh_method": "immutable",
            "timestamp_precision": "seconds",
            "reliability": "FIXTURE",
            "history_kind": "TRUE_HISTORICAL",
            "point_in_time_safe": True,
        }
    )
    result = await ConvergenceOrchestrator(
        convergence_warehouse, environment={}
    )._historical_acquisition()
    assert result.state == "PASSED_ENGINEERING"
    assert result.evidence["acquired"]["existing_local_datasets"]["datasets"] == 1
    assert "DUNE_API_KEY" in result.evidence["blockers"][1]


def test_convergence_cli_import_is_side_effect_free():
    from memecoin_bot.convergence.__main__ import parser

    args = parser().parse_args(["--warehouse", "x.db", "status"])
    assert args.command == "status"
    asyncio.run(asyncio.sleep(0))

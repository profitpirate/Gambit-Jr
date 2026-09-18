from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from memecoin_bot.alpha_engine import LaunchEvent
from memecoin_bot.config import Settings
from memecoin_bot.database import Store
from memecoin_bot.discord.bot_runtime import _run_coupled_runtime
from memecoin_bot.models import DiscoveryEvent, iso
from memecoin_bot.providers.base import ProviderError, sanitize_provider_error
from memecoin_bot.providers.launch_events import EvmFactoryLaunchSource
from memecoin_bot.realtime import CanonicalEvent, CanonicalEventFabric, CanonicalEventType
from memecoin_bot.service import IntelligenceService


@pytest.fixture
def db(tmp_path: Path) -> Store:
    value = Store(tmp_path / "hardening.db", Path("migrations"))
    value.migrate()
    try:
        yield value
    finally:
        value.close()


def canonical(
    token: str,
    *,
    source: str = "native",
    seconds: float = 0,
    kind: CanonicalEventType = CanonicalEventType.TOKEN_CREATED,
    payload: dict | None = None,
    signature: str | None = None,
    pool: str | None = None,
) -> CanonicalEvent:
    now = datetime(2026, 9, 18, 6, 0, tzinfo=UTC) + timedelta(seconds=seconds)
    stamp = now.isoformat()
    return CanonicalEvent.create(
        kind,
        token,
        "solana",
        "pumpfun",
        source,
        stamp,
        received_timestamp=stamp,
        available_timestamp=stamp,
        transaction_signature=signature,
        pool_identity=pool,
        source_event_id=f"{source}:{kind}:{seconds}",
        payload=payload or {},
    )


def test_solana_identity_is_case_sensitive_and_evm_identity_is_not() -> None:
    upper = CanonicalEvent.create(
        CanonicalEventType.TOKEN_CREATED,
        "AbCdEf123",
        "solana",
        "pumpfun",
        "one",
        "2026-09-18T06:00:00+00:00",
    )
    lower = CanonicalEvent.create(
        CanonicalEventType.TOKEN_CREATED,
        "aBcDeF123",
        "solana",
        "pumpfun",
        "one",
        "2026-09-18T06:00:00+00:00",
    )
    assert upper.canonical_key != lower.canonical_key

    evm_a = CanonicalEvent.create(
        CanonicalEventType.TOKEN_CREATED,
        "0xAbCdEf",
        "bsc",
        "fourmeme",
        "one",
        "2026-09-18T06:00:00+00:00",
    )
    evm_b = CanonicalEvent.create(
        CanonicalEventType.TOKEN_CREATED,
        "0xabcdef",
        "BSC",
        "fourmeme",
        "one",
        "2026-09-18T06:00:00+00:00",
    )
    assert evm_a.canonical_key == evm_b.canonical_key


def test_legacy_solana_canonical_key_is_found_without_duplicate(db: Store) -> None:
    fabric = CanonicalEventFabric(db)
    first = canonical("MiXeDBase58Address", source="native", payload={"creator": "C"})
    assert fabric.publish(first).is_new
    with db.conn:
        db.conn.execute(
            "UPDATE canonical_events SET canonical_key=? WHERE event_id=?",
            (first.legacy_canonical_key, first.event_id),
        )
    confirmed = canonical("MiXeDBase58Address", source="backup", payload={"creator": "C"})
    result = fabric.publish(confirmed)
    assert not result.is_new
    assert not result.conflict
    assert db.conn.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0] == 1


def test_conflicting_provider_is_provenance_not_confirmation(db: Store) -> None:
    fabric = CanonicalEventFabric(db)
    first = CanonicalEvent.create(
        CanonicalEventType.TOKEN_CREATED,
        "ConflictMint",
        "solana",
        "pumpfun",
        "provider-a",
        "2026-09-18T06:00:00+00:00",
        received_timestamp="2026-09-18T06:00:00+00:00",
        available_timestamp="2026-09-18T06:00:00+00:00",
        confidence=0.55,
        payload={"creator": "creator-a"},
    )
    assert fabric.publish(first).confirmation_count == 1
    conflicting = CanonicalEvent.create(
        CanonicalEventType.TOKEN_CREATED,
        "ConflictMint",
        "solana",
        "pumpfun",
        "provider-b",
        "2026-09-18T06:00:00+00:00",
        received_timestamp="2026-09-18T06:00:01+00:00",
        available_timestamp="2026-09-18T06:00:01+00:00",
        confidence=0.99,
        payload={"creator": "creator-b"},
    )
    result = fabric.publish(conflicting)
    row = db.conn.execute(
        "SELECT confirmation_sources_json,confidence,conflicts_json FROM canonical_events"
    ).fetchone()
    assert result.conflict
    assert result.confirmation_count == 1
    assert row["confirmation_sources_json"] == '["provider-a"]'
    assert row["confidence"] == pytest.approx(0.55)
    assert "provider-b" in row["conflicts_json"]
    assert db.conn.execute("SELECT COUNT(*) FROM canonical_event_sources").fetchone()[0] == 2


def test_migration_state_never_regresses_on_stale_event(db: Store) -> None:
    fabric = CanonicalEventFabric(db)
    token = "MigrationTruthMint"
    created = canonical(token, seconds=0, payload={"creator": "creator"})
    completed = canonical(
        token,
        seconds=10,
        source="amm",
        kind=CanonicalEventType.MIGRATION_COMPLETED,
        pool="PoolNew",
    )
    stale_started = canonical(
        token,
        seconds=5,
        source="curve",
        kind=CanonicalEventType.MIGRATION_STARTED,
        pool="PoolOld",
    )
    fabric.publish(created)
    fabric.project(created)
    fabric.publish(completed)
    fabric.project(completed)
    fabric.publish(stale_started)
    fabric.project(stale_started)
    token_id = db.token_id(token, "solana")
    row = db.conn.execute(
        "SELECT migration_state,pool_identity,migration_completed_at "
        "FROM token_realtime_state WHERE token_id=?",
        (token_id,),
    ).fetchone()
    assert row["migration_state"] == "MIGRATED"
    assert row["pool_identity"] == "PoolNew"
    assert row["migration_completed_at"] == completed.source_timestamp


def test_out_of_order_completion_can_still_advance_migration(db: Store) -> None:
    fabric = CanonicalEventFabric(db)
    token = "LateCompletionMint"
    created = canonical(token, seconds=0, payload={"creator": "creator"})
    newer_trade = canonical(
        token,
        seconds=20,
        source="trade-feed",
        kind=CanonicalEventType.TOKEN_TRADE,
        payload={"side": "buy", "real_token_reserves": 100.0},
        signature="NewerTradeSig",
    )
    late_completion = canonical(
        token,
        seconds=10,
        source="amm",
        kind=CanonicalEventType.MIGRATION_COMPLETED,
        pool="LatePool",
    )
    for event in (created, newer_trade, late_completion):
        fabric.publish(event)
        fabric.project(event)
    token_id = db.token_id(token, "solana")
    row = db.conn.execute(
        "SELECT migration_state,pool_identity,last_event_at,migration_completed_at "
        "FROM token_realtime_state WHERE token_id=?",
        (token_id,),
    ).fetchone()
    assert row["migration_state"] == "MIGRATED"
    assert row["pool_identity"] == "LatePool"
    assert row["last_event_at"] == newer_trade.source_timestamp
    assert row["migration_completed_at"] == late_completion.source_timestamp


def test_stale_processing_claim_hits_attempt_ceiling(db: Store) -> None:
    fabric = CanonicalEventFabric(db)
    event = canonical("PoisonMint")
    fabric.publish(event)
    claimed = fabric.claim_pending(1)
    assert len(claimed) == 1
    stale = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    with db.conn:
        db.conn.execute(
            "UPDATE canonical_events SET claimed_at=?,processing_attempts=5 "
            "WHERE event_id=?",
            (stale, event.event_id),
        )
    assert fabric.recover_stale_claims(lease_seconds=1, max_attempts=5) == 0
    row = db.conn.execute(
        "SELECT processing_status,processing_error FROM canonical_events WHERE event_id=?",
        (event.event_id,),
    ).fetchone()
    assert row["processing_status"] == "FAILED"
    assert row["processing_error"] == "STALE_CLAIM_MAX_ATTEMPTS"


def test_outbox_backoff_and_dead_letter_prevent_retry_storm(db: Store) -> None:
    now = iso()
    with db.conn:
        db.conn.execute(
            "INSERT INTO outbox(event_key,event_type,payload_json,created_at) "
            "VALUES('stress:1','TEST','{}',?)",
            (now,),
        )
    row = db.claim_outbox(1)[0]
    claim = row["claim_token"]
    db.mark_outbox_error(
        int(row["id"]),
        "down",
        claim,
        max_attempts=2,
        base_delay_seconds=60,
    )
    assert db.claim_outbox(1) == []
    with db.conn:
        db.conn.execute(
            "UPDATE outbox SET next_attempt_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), row["id"]),
        )
    second = db.claim_outbox(1)[0]
    db.mark_outbox_error(
        int(second["id"]),
        "still down",
        second["claim_token"],
        max_attempts=2,
    )
    final = db.conn.execute(
        "SELECT attempts,dead_lettered_at,next_attempt_at FROM outbox WHERE id=?",
        (row["id"],),
    ).fetchone()
    assert final["attempts"] == 2
    assert final["dead_lettered_at"] is not None
    assert final["next_attempt_at"] is None
    assert db.claim_outbox(1) == []
    assert db.pending_outbox() == []


@pytest.mark.asyncio
async def test_candidate_batch_failure_falls_back_to_individual_monitor(db: Store) -> None:
    class BatchMarket:
        name = "batch-market"

        def __init__(self) -> None:
            self.individual = 0

        async def batch_market_snapshots(self, _addresses, _chain):
            raise ProviderError("batch unavailable")

        async def market_snapshot(self, _address, _chain):
            self.individual += 1

    class Null:
        async def send(self, _content):
            pass

    config = Settings(
        database_path=db.path,
        historical_warehouse_path=db.path.with_name("historical.db"),
        approved_feature_store_path=db.path.with_name("approved.db"),
    )
    market = BatchMarket()
    service = IntelligenceService(config, db, object(), market, object(), Null())
    token_id, _ = db.upsert_discovery(
        DiscoveryEvent(token_address="BatchFallbackMint", chain="solana", source="test")
    )
    db.ensure_candidate(token_id, iso(), config.scoring_version)
    result = await service.monitor_candidates_once()
    assert market.individual == 1
    assert result
    candidate = db.candidate_for_token(token_id)
    assert candidate["state"] == "PENDING_EVIDENCE"


@pytest.mark.asyncio
async def test_supervisor_cancels_siblings_before_propagating_worker_failure() -> None:
    service = object.__new__(IntelligenceService)
    service.stop_event = asyncio.Event()
    cancelled = asyncio.Event()

    async def sibling() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def broken() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="service worker crashed: broken"):
        await service._supervise({"sibling": sibling(), "broken": broken()})
    assert cancelled.is_set()
    assert service.stop_event.is_set()


@pytest.mark.asyncio
async def test_discord_and_intelligence_share_failure_domain() -> None:
    class Service:
        def __init__(self) -> None:
            self.stopped = False

        async def run(self) -> None:
            await asyncio.sleep(0)
            raise RuntimeError("service failure")

        def stop(self) -> None:
            self.stopped = True

    class Client:
        def __init__(self) -> None:
            self.closed = False
            self.wait = asyncio.Event()

        async def start(self, _token: str) -> None:
            await self.wait.wait()

        async def close(self) -> None:
            self.closed = True
            self.wait.set()

    service = Service()
    client = Client()
    with pytest.raises(RuntimeError, match="intelligence service crashed"):
        await _run_coupled_runtime(service, client, "token")
    assert service.stopped
    assert client.closed


@pytest.mark.asyncio
async def test_discord_exit_stops_intelligence_service() -> None:
    class Service:
        def __init__(self) -> None:
            self.stopped = False
            self.stop_event = asyncio.Event()

        async def run(self) -> None:
            await self.stop_event.wait()

        def stop(self) -> None:
            self.stopped = True
            self.stop_event.set()

    class Client:
        async def start(self, _token: str) -> None:
            return

        async def close(self) -> None:
            return

    service = Service()
    await _run_coupled_runtime(service, Client(), "token")
    assert service.stopped


def test_provider_error_redaction_removes_query_and_path_secrets() -> None:
    message = (
        "GET https://mainnet.example/v2/alchemy-secret?api-key=helius-secret "
        "token=telegram-secret x-api-key: birdeye-secret"
    )
    safe = sanitize_provider_error(message)
    for secret in ("alchemy-secret", "helius-secret", "telegram-secret", "birdeye-secret"):
        assert secret not in safe
    assert safe.count("<redacted>") >= 4


@pytest.mark.asyncio
async def test_bsc_cursor_is_committed_only_after_successful_batch_delivery() -> None:
    saved: list[tuple[str, str, dict]] = []

    class Client:
        async def request(self, _url, _method, payload):
            if payload["method"] == "eth_blockNumber":
                return {"result": "0x10"}
            if payload["method"] == "eth_getBlockByNumber":
                return {"result": {"timestamp": "0x65000000"}}
            return {
                "result": [
                    {
                        "address": "0xfactory",
                        "blockNumber": "0x10",
                        "transactionHash": "0xtx",
                        "logIndex": "0x1",
                        "topics": [
                            "0xevent",
                            "0x" + "0" * 24 + "1234567890abcdef1234567890abcdef12345678",
                        ],
                    }
                ]
            }

    source = EvmFactoryLaunchSource(
        "https://bsc.example",
        ["0xfactory"],
        ["0xevent"],
        Client(),
        save_cursor=lambda name, cursor, meta: saved.append((name, cursor, meta)),
    )
    events = await source.poll_once()
    assert events and saved == []
    source.commit_cursor()
    assert saved and saved[-1][1] == "17"


def test_launch_event_hash_preserves_solana_case_and_normalizes_bsc() -> None:
    first = LaunchEvent.deterministic(
        "source", "solana", "AbCdBase58", "2026-09-18T06:00:00+00:00"
    )
    second = LaunchEvent.deterministic(
        "source", "solana", "aBcDBase58", "2026-09-18T06:00:00+00:00"
    )
    assert first.event_key != second.event_key
    evm_a = LaunchEvent.deterministic(
        "source", "bsc", "0xAbCd", "2026-09-18T06:00:00+00:00"
    )
    evm_b = LaunchEvent.deterministic(
        "source", "BSC", "0xabcd", "2026-09-18T06:00:00+00:00"
    )
    assert evm_a.event_key == evm_b.event_key


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_timeout_seconds", 0),
        ("provider_max_retries", -1),
        ("provider_circuit_failures", 0),
        ("provider_circuit_cooldown_seconds", 0),
        ("launch_source_reconnect_seconds", 0),
        ("candidate_retry_backoff", 0.5),
        ("health_port", 0),
        ("radar_board_port", 70000),
    ],
)
def test_invalid_runtime_config_fails_fast(field: str, value: float) -> None:
    config = Settings()
    setattr(config, field, value)
    with pytest.raises(ValueError):
        config.validate()

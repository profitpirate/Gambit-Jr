from __future__ import annotations

import asyncio
import base64
import struct
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from memecoin_bot.config import Settings
from memecoin_bot.database import Store
from memecoin_bot.historical.backfill import BackfillEngine
from memecoin_bot.historical.dune_registry import DuneQueryRegistry
from memecoin_bot.historical.evaluation import (
    EvaluationUniverse,
    evaluation_universe_hash,
    require_same_universe,
)
from memecoin_bot.historical.providers import DuneMonthHistoricalProvider
from memecoin_bot.historical.runner_models import TargetSpecificRunnerResearch
from memecoin_bot.historical.store import HistoricalWarehouse
from memecoin_bot.models import DiscoveryEvent, MarketSnapshot
from memecoin_bot.providers.base import ProviderError
from memecoin_bot.realtime import CanonicalEvent, CanonicalEventFabric, CanonicalEventType
from memecoin_bot.realtime import providers as realtime_providers
from memecoin_bot.realtime.decision import RouteState, RunnerDecisionEngine
from memecoin_bot.realtime.features import RealtimeFeatureProjector
from memecoin_bot.realtime.lanes import TokenLaneExecutor
from memecoin_bot.realtime.outcomes import DecisionOutcomeLedger
from memecoin_bot.realtime.providers import HeliusCuratedSource, NativePumpFunSource
from memecoin_bot.realtime.pumpfun import CREATE_EVENT_DISCRIMINATOR, PUMP_PROGRAM_ID

# Keep source-time fixtures behind the process clock so canonical event
# validation remains deterministic regardless of the test runner's timezone.
NOW = datetime.now(UTC) - timedelta(minutes=1)


@pytest.fixture
def architecture_store(tmp_path: Path):
    store = Store(tmp_path / "architecture.db", Path("migrations"))
    store.migrate()
    try:
        yield store
    finally:
        store.close()


def _token(store: Store, address: str = "AuthoritativeToken111") -> int:
    token_id, _ = store.upsert_discovery(
        DiscoveryEvent(
            token_address=address,
            chain="solana",
            discovered_at=(NOW - timedelta(minutes=5)).isoformat(),
            source="fixture",
        )
    )
    return token_id


def _decision(store: Store, token_id: int, **overrides):
    values = {
        "token_id": token_id,
        "token_address": "AuthoritativeToken111",
        "chain": "solana",
        "decision_at": NOW.isoformat(),
        "stage": "BONDING",
        "thesis": {
            "thesis_type": "SELL_ABSORPTION_V2",
            "heuristic_runner_score": 0.91,
            "supporting_evidence": [{"evidence": "post-sell capital recovered"}],
        },
        "v15_control": {
            "signal_tier": "STRONG",
            "evidence_coverage": 0.8,
            "runner_score": 88,
            "failure_score": 12,
            "critical_unknowns": [],
        },
        "legacy_control": {"classification": "HIGH_CONVICTION", "authority": "CONTROL_ONLY"},
        "waiting_reasons": [],
        "hard_rejections": [],
        "entry_state": {
            "state": "EARLY",
            "decision_price": 1.0,
            "decision_market_cap": 100.0,
        },
        "provider_health": {"helius": {"state": "HEALTHY"}},
        "provenance": [
            {
                "field_name": "helius_trade",
                "source_timestamp": (NOW - timedelta(milliseconds=20)).isoformat(),
                "received_timestamp": (NOW - timedelta(milliseconds=10)).isoformat(),
                "available_timestamp": NOW.isoformat(),
                "freshness_sla_seconds": 2,
            }
        ],
        "latency": {
            "source_to_decision_ms": 20.0,
            "timestamps": {
                "source": (NOW - timedelta(milliseconds=20)).isoformat(),
                "decision": NOW.isoformat(),
            },
        },
    }
    values.update(overrides)
    return RunnerDecisionEngine(store).decide(**values)


def test_runner_decision_is_single_authority_and_never_labels_heuristics_as_probability(
    architecture_store: Store,
) -> None:
    token_id = _token(architecture_store)
    held = _decision(architecture_store, token_id)
    assert held.route_state == RouteState.HOLD
    assert held.runner_probabilities == {
        "p_2x": None,
        "p_5x": None,
        "p_10x": None,
        "p_20x": None,
        "p_50x": None,
    }
    assert held.heuristic_scores["runner_thesis_score"] == 0.91
    assert held.model_versions["runner"] == "UNAVAILABLE"
    assert held.evidence_freshness["derived"] is True
    assert held.evidence_freshness["all_evidence_available_by_decision"] is True

    zero_token_id = _token(architecture_store, "ZeroHeuristicToken111")
    zero = _decision(
        architecture_store,
        zero_token_id,
        thesis={
            "thesis_type": "CONTROL",
            "heuristic_runner_score": 0.0,
            "runner_probability": 0.99,
        },
    )
    assert zero.heuristic_scores["runner_thesis_score"] == 0.0

    later = (NOW + timedelta(seconds=1)).isoformat()
    routed = _decision(
        architecture_store,
        token_id,
        decision_at=later,
        provenance=[],
        public_alerts_enabled=True,
    )
    assert routed.route_state == RouteState.PUBLIC_ALERT
    latest = RunnerDecisionEngine(architecture_store).latest(token_id)
    assert latest and latest["decision_id"] == routed.decision_id
    assert latest["champion"] == "CONTROL_V15"


def test_routed_decision_latency_is_completed_at_enqueue_and_discord(
    architecture_store: Store,
) -> None:
    token_id = _token(architecture_store)
    decision = _decision(
        architecture_store,
        token_id,
        public_alerts_enabled=True,
    )
    engine = RunnerDecisionEngine(architecture_store)
    engine.mark_enqueued(decision.decision_id, (NOW + timedelta(milliseconds=100)).isoformat())
    engine.mark_discord_delivered(
        decision.decision_id,
        (NOW + timedelta(milliseconds=250)).isoformat(),
    )
    latest = engine.latest(token_id)
    assert latest is not None
    assert latest["latency"]["decision_to_enqueue_ms"] == 100
    assert latest["latency"]["enqueue_to_discord_ms"] == 150
    assert latest["latency"]["source_to_discord_ms"] == 270
    assert latest["latency"]["discord_delivery"]["state"] == "DELIVERED"


def test_calibrated_target_probabilities_are_nested_and_unvalidated_values_are_ignored(
    architecture_store: Store,
) -> None:
    token_id = _token(architecture_store)
    unvalidated = _decision(
        architecture_store,
        token_id,
        calibrated_models={"calibration_state": "DEVELOPMENT_ONLY", "p_2x": 0.9},
    )
    assert unvalidated.runner_probabilities["p_2x"] is None
    validated = _decision(
        architecture_store,
        token_id,
        decision_at=(NOW + timedelta(seconds=2)).isoformat(),
        provenance=[],
        calibrated_models={
            "calibration_state": "VALIDATED_CHRONOLOGICAL",
            "approval_state": "APPROVED_HUMAN_GATED",
            "evaluation_universe_hash": "a" * 64,
            "p_2x": 0.7,
            "p_5x": 0.8,
            "p_10x": 0.3,
            "p_20x": 0.2,
            "p_50x": 0.1,
        },
    )
    assert list(validated.runner_probabilities.values()) == [0.7, 0.7, 0.3, 0.2, 0.1]


def test_outcomes_start_at_decision_not_discovery(architecture_store: Store) -> None:
    token_id = _token(architecture_store)
    decision = _decision(architecture_store, token_id)
    for seconds, price, market_cap in ((10, 1.5, 150), (20, 2.5, 250), (30, 2.0, 200)):
        architecture_store.save_snapshot(
            token_id,
            MarketSnapshot(
                token_address="AuthoritativeToken111",
                chain="solana",
                captured_at=(NOW + timedelta(seconds=seconds)).isoformat(),
                source="fixture",
                price_usd=price,
                market_cap_usd=market_cap,
                liquidity_usd=50,
            ),
        )
    assert (
        DecisionOutcomeLedger(architecture_store).refresh_token(token_id, mature_after_seconds=20)
        == 1
    )
    row = architecture_store.conn.execute(
        "SELECT * FROM decision_outcomes_v15 WHERE decision_id=?", (decision.decision_id,)
    ).fetchone()
    assert row["decision_market_cap"] == 100
    assert row["peak_multiple_from_decision"] == 2.5
    assert row["time_to_2x_from_decision"] == 20


def test_evaluation_hash_refuses_cross_universe_comparison() -> None:
    universe = EvaluationUniverse(
        "2025-01-01T00:00:00+00:00",
        "2025-06-01T00:00:00+00:00",
        30,
        "quality-v1",
        86_400,
        "top-10pct",
        "peak_multiple_from_decision",
        "tradeable-v1",
        "pumpfun-all-launches-v1",
    )
    rows = [
        {"entity_key": "A", "decision_at": NOW.isoformat(), "outcome_available_at": NOW.isoformat()}
    ]
    first = evaluation_universe_hash(universe, rows)
    assert first == evaluation_universe_hash(universe, list(reversed(rows)))
    assert (
        require_same_universe(
            {"evaluation_universe_hash": first}, {"evaluation_universe_hash": first}
        )
        == first
    )
    with pytest.raises(ValueError, match="hashes differ"):
        require_same_universe(
            {"evaluation_universe_hash": first}, {"evaluation_universe_hash": "b" * 64}
        )


def test_repository_dune_sql_registry_has_strict_contracts() -> None:
    registry = DuneQueryRegistry()
    assert set(registry.names()) == {
        "creator_activity",
        "migrations",
        "monthly_universe",
        "outcome_reconstruction",
        "pumpfun_launches",
        "pumpfun_trades",
        "pumpswap_trades",
        "wallet_activity",
    }
    sql = registry.render(
        "monthly_universe",
        {"month_start": "2024-01-01T00:00:00+00:00", "month_end": "2024-02-01T00:00:00+00:00"},
    )
    assert "{{" not in sql
    assert "solana.transactions" in sql
    registry.validate_columns("monthly_universe", [])
    for query_name in registry.names():
        spec = registry.spec(query_name)
        rendered = registry.render(
            query_name,
            {
                "month_start": "2024-04-01T00:00:00+00:00",
                "month_end": "2024-05-01T00:00:00+00:00",
            },
        )
        assert "{{" not in rendered
        assert spec.sql_sha256
        registry.validate_columns(
            query_name,
            [{column: None for column in spec.expected_columns}],
        )
        with pytest.raises(ValueError, match="missing contract columns"):
            registry.validate_columns(query_name, [{"unexpected": 1}])
    with pytest.raises(ValueError, match="requires exactly"):
        registry.render("monthly_universe", {"month_start": "2024-01-01"})
    with pytest.raises(ValueError, match="outside"):
        registry.render(
            "monthly_universe",
            {"month_start": "1999-01-01", "month_end": "2024-02-01"},
        )


class _DirectDuneClient:
    def __init__(self):
        self.sql: list[str] = []

    async def execute_sql(self, sql: str):
        self.sql.append(sql)
        return "direct-execution"

    async def wait(self, execution_id: str):
        return {"state": "QUERY_STATE_COMPLETED", "execution_id": execution_id}

    async def results(self, execution_id: str, offset: int, limit: int):
        assert execution_id == "direct-execution"
        rows = [
            {
                "token_address": f"Token{offset}",
                "observed_at": "2024-01-02 03:04:05",
                "creator": "Creator1",
                "tx_id": f"Tx{offset}",
                "block_slot": 100 + offset,
                "source": "fixture",
            }
        ]
        return {
            "result": {
                "rows": rows if offset < 2 else [],
                "metadata": {"total_row_count": 2},
            }
        }


@pytest.mark.asyncio
async def test_dune_direct_sql_paginates_resumes_writes_parquet_and_compact_tables(
    tmp_path: Path,
) -> None:
    warehouse = HistoricalWarehouse(tmp_path / "warehouse.db", tmp_path / "archive")
    client = _DirectDuneClient()
    provider = DuneMonthHistoricalProvider(
        None,
        "2024-01",
        None,
        page_size=1,
        client=client,
        parquet_root=tmp_path / "parquet",
    )
    warehouse.register_dataset(
        {
            "dataset_id": provider.dataset_id,
            "dataset_version": "owned-sql-test-v1",
            "provider": provider.name,
            "chain": "solana",
            "acquisition_method": "repository_owned_direct_sql",
            "refresh_method": "month_partition",
            "timestamp_precision": "block_time",
            "reliability": "FIXTURE",
            "history_kind": "TRUE_HISTORICAL",
            "point_in_time_safe": True,
        }
    )
    first = await BackfillEngine(warehouse).run(provider, maximum_pages=1)
    assert first["state"] == "RUNNING"
    final = await BackfillEngine(warehouse).run(provider, job_id=first["job_id"])
    assert final["state"] == "COMPLETE"
    assert len(client.sql) == 1
    assert warehouse.conn.execute("SELECT COUNT(*) FROM historical_tokens_v15").fetchone()[0] == 2
    assert warehouse.conn.execute("SELECT COUNT(*) FROM historical_launches_v15").fetchone()[0] == 2
    partition = warehouse.conn.execute("SELECT * FROM dune_partition_state_v15").fetchone()
    assert partition["state"] == "COMPLETE"
    assert list((tmp_path / "parquet").rglob("*.parquet"))
    warehouse.close()


class _RetryDuneClient(_DirectDuneClient):
    def __init__(self, *, duplicate: bool = False):
        super().__init__()
        self.result_calls = 0
        self.duplicate = duplicate

    async def results(self, execution_id: str, offset: int, limit: int):
        self.result_calls += 1
        if not self.duplicate and self.result_calls == 1:
            raise RuntimeError("temporary Dune result failure")
        if self.duplicate:
            row = {
                "token_address": "DuplicateToken",
                "observed_at": "2024-01-02 03:04:05",
                "creator": "Creator1",
                "tx_id": "DuplicateTx",
                "block_slot": 100,
                "source": "fixture",
            }
            return {"result": {"rows": [row, dict(row)], "metadata": {"total_row_count": 2}}}
        return await super().results(execution_id, offset, limit)


@pytest.mark.asyncio
async def test_dune_retry_and_duplicate_rows_are_bounded_and_idempotent(tmp_path: Path) -> None:
    for name, client, expected_rows in (
        ("retry", _RetryDuneClient(), 2),
        ("duplicate", _RetryDuneClient(duplicate=True), 1),
    ):
        warehouse = HistoricalWarehouse(tmp_path / f"{name}.db", tmp_path / f"{name}-archive")
        provider = DuneMonthHistoricalProvider(
            None,
            "2024-01",
            None,
            page_size=2,
            client=client,
        )
        warehouse.register_dataset(
            {
                "dataset_id": provider.dataset_id,
                "dataset_version": f"{name}-v1",
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
        result = await BackfillEngine(
            warehouse,
            max_retries=1,
            maximum_rate_limit_sleep_seconds=0,
        ).run(provider)
        assert result["state"] == "COMPLETE"
        assert (
            warehouse.conn.execute("SELECT COUNT(*) FROM raw_evidence").fetchone()[0]
            == expected_rows
        )
        assert (
            warehouse.conn.execute("SELECT COUNT(*) FROM historical_launches_v15").fetchone()[0]
            == expected_rows
        )
        assert warehouse.conn.execute("SELECT COUNT(*) FROM data_quality_v15").fetchone()[0] == 1
        warehouse.close()


def _borsh_string(value: str) -> bytes:
    encoded = value.encode()
    return struct.pack("<I", len(encoded)) + encoded


def _create_event_bytes() -> bytes:
    pubkeys = [bytes([value]) * 32 for value in range(1, 6)]
    return b"".join(
        (
            CREATE_EVENT_DISCRIMINATOR,
            _borsh_string("Runner"),
            _borsh_string("RUN"),
            _borsh_string("https://example.invalid"),
            *pubkeys[:4],
            struct.pack("<q", int(NOW.timestamp())),
            struct.pack("<QQQQ", 1, 2, 3, 4),
            pubkeys[4],
            b"\0\0",
            bytes(32),
            struct.pack("<Q", 1),
        )
    )


class _NoRpcClient:
    timeout = 1

    async def request(self, *_args, **_kwargs):
        raise AssertionError("fast decoded logs must not call getTransaction")


@pytest.mark.asyncio
async def test_pumpfun_fast_path_emits_before_transaction_enrichment() -> None:
    source = NativePumpFunSource("https://example.invalid", _NoRpcClient())
    log = "Program data: " + base64.b64encode(_create_event_bytes()).decode()
    payload = {
        "params": {
            "result": {
                "context": {"slot": 123},
                "value": {
                    "signature": "FastSignature",
                    "err": None,
                    "logs": [
                        f"Program {PUMP_PROGRAM_ID} invoke [1]",
                        log,
                        f"Program {PUMP_PROGRAM_ID} success",
                    ],
                },
            }
        }
    }
    events = await source.parse_notification(payload)
    assert [event.event_type for event in events] == [CanonicalEventType.TOKEN_CREATED]
    assert events[0].raw_provenance["fast_path"] is True


class _BackfillRpcClient:
    timeout = 1

    def __init__(self, primary: bool):
        self.primary = primary
        self.signature_calls: list[dict] = []

    async def request(self, _url, _method, body):
        if self.primary:
            raise ProviderError("429 primary exhausted")
        if body["method"] == "getSignaturesForAddress":
            options = body["params"][1]
            self.signature_calls.append(options)
            if "before" not in options:
                return {
                    "result": [
                        {"signature": "S103", "slot": 103, "err": None},
                        {"signature": "S102", "slot": 102, "err": None},
                    ]
                }
            return {
                "result": [
                    {"signature": "S101", "slot": 101, "err": None},
                    {"signature": "S100", "slot": 100, "err": None},
                ]
            }
        if body["method"] == "getTransaction":
            return {"result": {"meta": {"err": None, "logMessages": []}}}
        raise AssertionError(body)


@pytest.mark.asyncio
async def test_paginated_pump_backfill_uses_public_fallback_and_proves_gap_boundary() -> None:
    fallback = _BackfillRpcClient(False)
    source = NativePumpFunSource(
        "https://primary.invalid",
        _BackfillRpcClient(True),
        fallback_rpc_url="https://fallback.invalid",
        fallback_client=fallback,
        backfill_limit=2,
        backfill_max_pages=5,
    )
    source.last_slot = 100
    emitted = []
    assert await source.backfill(emitted.append) == 0
    assert len(fallback.signature_calls) == 2
    assert fallback.signature_calls[1]["before"] == "S102"
    assert source.gap_incomplete is False
    assert source.fallback_requests >= 2


@pytest.mark.asyncio
async def test_token_lanes_order_same_token_and_overlap_different_tokens() -> None:
    executor = TokenLaneExecutor(8, queue_size=8)
    token_a = "A"
    token_b = next(
        value
        for value in ("B", "C", "D", "E")
        if executor.lane_for(value) != executor.lane_for(token_a)
    )
    events = []
    for token, sequence in ((token_a, 1), (token_b, 1), (token_a, 2), (token_b, 2)):
        timestamp = (NOW + timedelta(seconds=sequence)).isoformat()
        events.append(
            CanonicalEvent.create(
                CanonicalEventType.TOKEN_TRADE,
                token,
                "solana",
                "pumpfun",
                "fixture",
                timestamp,
                source_event_id=f"{token}-{sequence}",
                payload={"side": "buy", "actor": token, "sol_amount": 1},
            )
        )
    claimed = False
    stop = asyncio.Event()
    wake = asyncio.Event()
    ordered: dict[str, list[int]] = {token_a: [], token_b: []}
    active = 0
    maximum_active = 0

    def claim(_limit: int):
        nonlocal claimed
        if claimed:
            return []
        claimed = True
        return events

    async def handle(event: CanonicalEvent):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        ordered[event.canonical_token].append(int(event.source_event_id.split("-")[-1]))
        active -= 1
        if sum(map(len, ordered.values())) == 4:
            stop.set()

    await executor.run(
        claim=claim,
        handle=handle,
        fail=lambda *_args: None,
        wake=wake,
        stop=stop,
        batch_size=10,
    )
    assert ordered == {token_a: [1, 2], token_b: [1, 2]}
    assert maximum_active >= 2


def test_incremental_hot_path_is_bounded_and_o1_per_event(architecture_store: Store) -> None:
    fabric = CanonicalEventFabric(architecture_store)
    created = CanonicalEvent.create(
        CanonicalEventType.TOKEN_CREATED,
        "HotToken111",
        "solana",
        "pumpfun",
        "fixture",
        NOW.isoformat(),
        source_event_id="created",
        payload={"creator": "Creator"},
    )
    fabric.publish(created)
    token_id, _ = fabric.project(created)
    assert token_id is not None
    projector = RealtimeFeatureProjector(architecture_store)
    projector.apply(token_id, created)
    started = time.perf_counter()
    for index in range(10_000):
        timestamp = (NOW + timedelta(milliseconds=index + 1)).isoformat()
        event = CanonicalEvent.create(
            CanonicalEventType.TOKEN_TRADE,
            "HotToken111",
            "solana",
            "pumpfun",
            "fixture",
            timestamp,
            transaction_signature=f"Signature{index}",
            source_event_id=f"{index}:0",
            payload={"side": "buy", "actor": f"Buyer{index}", "sol_amount": 0.001},
        )
        projector.apply(token_id, event)
    elapsed = time.perf_counter() - started
    row = architecture_store.conn.execute(
        "SELECT state_json FROM incremental_feature_state_v15 WHERE token_id=?", (token_id,)
    ).fetchone()
    assert len(row["state_json"]) < 20_000
    assert (
        architecture_store.conn.execute(
            "SELECT COUNT(*) FROM incremental_actor_state_v15 WHERE token_id=?", (token_id,)
        ).fetchone()[0]
        == 10_000
    )
    # Point-in-time availability is the wall-clock ingestion time, not merely
    # the chain event timestamp.
    feature = projector.compute(token_id, (datetime.now(UTC) + timedelta(seconds=1)).isoformat())
    assert feature["buyer_arrival"]["raw_buyers"] == 10_000
    assert feature["capital_efficiency"]["sol_gained_per_trade"] == pytest.approx(0.001)
    assert elapsed < 30


def test_sell_absorption_v2_tracks_bounded_multihorizon_and_second_sell(
    architecture_store: Store,
) -> None:
    fabric = CanonicalEventFabric(architecture_store)
    projector = RealtimeFeatureProjector(architecture_store)
    created = CanonicalEvent.create(
        CanonicalEventType.TOKEN_CREATED,
        "AbsorptionToken111",
        "solana",
        "pumpfun",
        "fixture",
        NOW.isoformat(),
        source_event_id="created",
        payload={"creator": "Creator"},
    )
    fabric.publish(created)
    token_id, _ = fabric.project(created)
    assert token_id is not None
    projector.apply(token_id, created)
    flow = [
        (1, "buy", "BuyerA", 2.0),
        (2, "buy", "BuyerB", 2.0),
        (3, "sell", "BuyerA", 0.5),
        (6, "buy", "BuyerC", 1.0),
        (9, "buy", "BuyerA", 0.5),
        (12, "sell", "BuyerB", 0.4),
        (14, "buy", "BuyerD", 0.8),
    ]
    for seconds, side, actor, amount in flow:
        event = CanonicalEvent.create(
            CanonicalEventType.TOKEN_TRADE,
            "AbsorptionToken111",
            "solana",
            "pumpfun",
            "fixture",
            (NOW + timedelta(seconds=seconds)).isoformat(),
            source_event_id=f"{seconds}:{side}",
            payload={"side": side, "actor": actor, "sol_amount": amount},
        )
        projector.apply(token_id, event)
    feature = projector.compute(
        token_id,
        (datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
    )
    absorption = feature["sell_absorption_v2"]
    assert absorption["authority"] == "RESEARCH_ONLY"
    assert absorption["responses_5_10_20_30_seconds"]["5"]["buy_sol"] == 1.0
    assert absorption["second_meaningful_sell"]["seller"] == "BuyerB"
    assert absorption["second_sell_absorption_ratio"] == 2.0
    assert absorption["seller_historical_behavior"] is None


def test_helius_is_derived_as_primary_without_entering_config_fingerprint() -> None:
    settings = Settings(
        helius_api_key="top-secret", solana_rpc_url="https://api.mainnet-beta.solana.com"
    )
    assert settings.effective_solana_rpc_url().startswith("https://mainnet.helius-rpc.com/")
    assert "top-secret" in settings.effective_solana_rpc_url()
    assert "top-secret" not in settings.config_fingerprint()


@pytest.mark.asyncio
async def test_helius_get_transaction_retries_429_with_a_bounded_budget(monkeypatch) -> None:
    source = HeliusCuratedSource("not-a-real-key", ["Wallet111"])

    class Response:
        def __init__(self, status: int):
            self.status = status
            self.headers = {"Retry-After": "-1"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def json(self):
            return {"result": {"transaction": {"signatures": ["Signature111"]}}}

    class Session:
        def __init__(self):
            self.statuses = iter((429, 429, 200))

        def post(self, *_args, **_kwargs):
            return Response(next(self.statuses))

    monkeypatch.setattr(realtime_providers.random, "random", lambda: 0.0)
    result = await source._transaction(Session(), "Signature111", 123)
    assert result is not None
    assert source.rpc_requests == 3
    assert source.rate_limits == 2


@pytest.mark.asyncio
async def test_helius_disconnect_is_reported_then_subscription_reconnects(monkeypatch) -> None:
    source = HeliusCuratedSource("not-a-real-key", ["Wallet111"])
    connection_attempts = 0

    class Stop:
        def __init__(self):
            self.stopped = False

        def is_set(self):
            return self.stopped

        def set(self):
            self.stopped = True

        async def wait(self):
            raise TimeoutError

    stop = Stop()

    class Websocket:
        async def send_json(self, _payload):
            return None

        async def receive_json(self):
            return {"result": 101}

    class WebsocketContext:
        async def __aenter__(self):
            nonlocal connection_attempts
            connection_attempts += 1
            if connection_attempts == 1:
                raise realtime_providers.aiohttp.ClientConnectionError("fixture disconnect")
            return Websocket()

        async def __aexit__(self, *_args):
            return False

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def ws_connect(self, *_args, **_kwargs):
            return WebsocketContext()

    monkeypatch.setattr(realtime_providers.aiohttp, "ClientSession", lambda **_kwargs: Session())
    health_states = []

    async def emit(event):
        if event.event_type == CanonicalEventType.PROVIDER_HEALTH:
            health_states.append(event.payload["state"])
            if event.payload["state"] == "CONNECTED":
                stop.set()

    await source.run_events(emit, stop)
    assert connection_attempts == 2
    assert health_states == ["DISCONNECTED", "CONNECTED"]
    assert source.reconnects == 1


def test_nonlinear_target_models_are_research_only_and_same_universe() -> None:
    rows = []
    for index in range(90):
        decision = datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=index)
        absorption = float(index % 7) / 6
        peak = 6.0 if absorption > 0.65 else 1.2
        rows.append(
            {
                "entity_key": f"decision:{index}",
                "decision_at": decision.isoformat(),
                "outcome_available_at": (decision + timedelta(days=2)).isoformat(),
                "peak_multiple_from_decision": peak,
                "features": {
                    "sell_absorption_5s": absorption,
                    "capital_replacement_ratio": absorption**2,
                    "sequence_recovery": float(absorption > 0.5),
                },
                "control_score": absorption,
                "terminal_failure": absorption < 0.2,
                "copyable": index % 3 != 0,
            }
        )
    universe = EvaluationUniverse(
        rows[60]["decision_at"],
        rows[-1]["decision_at"],
        30,
        "all-launches-v1",
        172_800,
        "top-10pct",
        "peak_multiple_from_decision",
        "copyability-v1",
        "fixture-v1",
    )
    result = TargetSpecificRunnerResearch().run(rows[:60], rows[60:], universe)
    assert result["status"] == "OFFLINE_VALIDATION_COMPLETE"
    assert result["approved"] is False
    assert result["targets"]["2x"]["status"] == "EVALUATED"
    assert result["evaluation_universe_hash"] == evaluation_universe_hash(universe, rows[60:])
    independent = TargetSpecificRunnerResearch().independent_failure_actionability_run(
        rows[:60], rows[60:], universe
    )
    assert independent["approved"] is False
    assert independent["targets_are_independent"] is True
    assert independent["tasks"]["failure"]["status"] == "EVALUATED_INDEPENDENTLY"
    assert independent["tasks"]["actionability"]["status"] == "EVALUATED_INDEPENDENTLY"

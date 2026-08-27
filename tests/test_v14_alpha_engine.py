from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest

from memecoin_bot.alpha_engine import (
    AlphaState,
    BoundedLaunchQueue,
    CreatorQuality,
    EntryState,
    LaunchEvent,
    PayoffGrade,
    SurvivalGrade,
    WalletBuy,
    WalletGraphEngine,
    buyer_cohort_metrics,
    capital_rotation,
    creator_quality,
    enrichment_level,
    evaluation_stage,
    filter_as_of,
    latency_summary,
    liquidity_quality,
    maximum_adverse_excursion,
    miss_analysis,
    narrative_election,
    payoff_engine,
    promotion_decision,
    provider_consensus,
    right_tail_metrics,
    survival_engine,
    t0_decision,
)
from memecoin_bot.discord.cards import compare_card, menu_card, scan_card
from memecoin_bot.models import MarketSnapshot, SafetyAssessment, iso
from memecoin_bot.providers.launch_events import (
    EvmFactoryLaunchSource,
    SolanaProgramLaunchSource,
    extract_new_mint,
)
from memecoin_bot.service import IntelligenceService
from tests.helpers import settings, store, temp_db_path


def launch(index: int = 1, *, chain: str = "solana") -> LaunchEvent:
    observed = (datetime.now(UTC) - timedelta(milliseconds=25)).isoformat()
    return LaunchEvent.deterministic(
        "fixture_launch",
        chain,
        f"Token{index:08d}",
        observed,
        launchpad="pumpfun" if chain == "solana" else "fourmeme",
        metadata={"buyer_count": 12, "bonding_curve_progress_percent": 8},
    )


def test_migration_006_is_additive_idempotent_and_reconciles_aliases():
    with temp_db_path() as path:
        db = store(path)
        event = launch()
        token_id, _ = db.upsert_discovery(
            __import__("memecoin_bot.models", fromlist=["DiscoveryEvent"]).DiscoveryEvent(
                token_address=event.token_address, source=event.source
            )
        )
        candidate_id, _ = db.ensure_candidate(token_id, event.source_received_at, "v1.3.1")
        db.conn.execute(
            "UPDATE candidates SET attempt_count=3,consecutive_missing_pair_count=2,"
            "consecutive_provider_failure_count=1 WHERE id=?",
            (candidate_id,),
        )
        db.conn.commit()
        db.reconcile_v14_state()
        row = db.conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        assert row["retry_count"] == 3
        assert row["consecutive_pair_missing"] == 2
        assert row["consecutive_provider_failures"] == 1
        expected = {
            "launch_events",
            "evaluation_stages_v14",
            "immutable_call_snapshots",
            "wallet_clusters",
            "creator_profiles_v14",
            "narratives_v14",
            "watchlists",
            "manual_scans",
            "performance_benchmarks_v14",
        }
        tables = {
            row[0] for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert expected <= tables
        before = list(db.conn.execute("SELECT version FROM schema_migrations"))
        db.migrate()
        assert before == list(db.conn.execute("SELECT version FROM schema_migrations"))
        db.close()


def test_t0_genesis_is_explicitly_uncertain_and_unknown_is_preserved():
    decision = t0_decision({"launch_event_verified": True, "age_seconds": 20, "buyer_count": 12})
    assert decision.state == AlphaState.GENESIS_RADAR
    assert decision.entry_state == EntryState.VERY_EARLY
    assert decision.confidence < 0.60
    assert "liquidity_usd" in decision.unknowns


def test_t0_terminal_safety_never_promotes():
    decision = t0_decision(
        {"launch_event_verified": True, "age_seconds": 5, "mint_authority_active": True}
    )
    assert decision.state == AlphaState.REJECTED_UNSAFE


def test_genesis_hot_priority_qualified_path_and_late_guard():
    state = AlphaState.GENESIS_RADAR
    state = promotion_decision(
        state,
        score=68,
        confidence=0.55,
        entry=EntryState.EARLY,
        survival=SurvivalGrade.ACCEPTABLE,
        payoff=PayoffGrade.CONVEX,
        independent_pillars=2,
    )
    assert state == AlphaState.HOT_RADAR
    state = promotion_decision(
        state,
        score=78,
        confidence=0.65,
        entry=EntryState.EARLY,
        survival=SurvivalGrade.STRONG,
        payoff=PayoffGrade.CONVEX,
        independent_pillars=3,
    )
    assert state == AlphaState.PRIORITY_RADAR
    state = promotion_decision(
        state,
        score=90,
        confidence=0.80,
        entry=EntryState.EARLY,
        survival=SurvivalGrade.STRONG,
        payoff=PayoffGrade.EXCEPTIONAL,
        independent_pillars=5,
    )
    assert state == AlphaState.QUALIFIED_SIGNAL
    assert (
        promotion_decision(
            state,
            score=10,
            confidence=0.1,
            entry=EntryState.EXTENDED,
            survival=SurvivalGrade.WEAK,
            payoff=PayoffGrade.POOR,
            independent_pillars=0,
        )
        == AlphaState.QUALIFIED_SIGNAL
    )
    assert (
        promotion_decision(
            AlphaState.GENESIS_RADAR,
            score=99,
            confidence=1,
            entry=EntryState.LATE,
            survival=SurvivalGrade.STRONG,
            payoff=PayoffGrade.EXCEPTIONAL,
            independent_pillars=6,
        )
        != AlphaState.QUALIFIED_SIGNAL
    )


def test_wallet_graph_fixtures_and_false_positive_avoidance():
    engine = WalletGraphEngine()
    independent = engine.analyze(
        [WalletBuy(f"w{i}", f"2026-01-01T00:00:{i:02d}+00:00", 100) for i in range(10)]
    )
    assert independent.clusters == []
    assert independent.warnings == []
    connected = engine.analyze(
        [
            WalletBuy("a", "2026-01-01T00:00:00+00:00", 100, "funder"),
            WalletBuy("b", "2026-01-01T00:00:03+00:00", 100, "funder"),
            WalletBuy("c", "2026-01-01T00:00:05+00:00", 100, "funder", True),
            WalletBuy("d", "2026-01-01T00:00:06+00:00", 100),
        ]
    )
    assert connected.clusters == [["a", "b", "c"]]
    assert connected.coordinated
    assert connected.deployer_linked_wallets == 1


def test_wallet_cluster_persistence_and_restart_memory():
    with temp_db_path() as path:
        db = store(path)
        result = WalletGraphEngine().analyze(
            [
                WalletBuy("a", iso(), 10, "f"),
                WalletBuy("b", iso(), 10, "f"),
            ]
        )
        ids = db.save_wallet_graph("solana", "mint", result)
        assert len(ids) == 1
        db.close()
        reopened = store(path)
        assert reopened.wallet_report("a")["clusters"][0]["id"] == ids[0]
        reopened.close()


@pytest.mark.parametrize(
    ("history", "expected"),
    [
        ([], CreatorQuality.UNKNOWN),
        ([{"outcome": "RUG"}, {"outcome": "FAILED"}], CreatorQuality.TOXIC),
        (
            [
                {"outcome": "RUNNER", "peak_multiple": 8},
                {"outcome": "RUNNER", "peak_multiple": 12},
                {"outcome": "SURVIVED", "peak_multiple": 2},
            ],
            CreatorQuality.PROVEN,
        ),
    ],
)
def test_creator_quality(history, expected):
    assert creator_quality(history)["quality"] == expected


def test_narrative_leader_copycat_saturation_decay_and_false_correlation():
    rows = [
        {
            "token_address": f"t{i}",
            "detected_at": f"2026-01-01T00:{i:02d}:00+00:00",
            "traction": 100 - i,
            "clone_similarity": 0 if i == 0 else 0.9,
        }
        for i in range(8)
    ]
    result = narrative_election(rows)
    assert result["leader"] == "t0"
    assert result["saturation"] == "SATURATED"
    assert any(row["role"] == "COPYCAT" and row["clone_penalty"] > 0 for row in result["members"])
    assert narrative_election([])["leader"] is None


def test_survival_and_payoff_engines_keep_unknown_separate_from_bad():
    assert survival_engine({})["grade"] == SurvivalGrade.UNKNOWN
    rug = survival_engine(
        {
            "liquidity_usd": 1_000,
            "connected_wallet_percent": 90,
            "creator_quality": "TOXIC",
            "sell_pressure": 0.9,
        }
    )
    assert rug["grade"] == SurvivalGrade.HIGH_RISK
    convex = payoff_engine({"age_seconds": 30, "market_cap_usd": 30_000}, SurvivalGrade.STRONG)
    assert convex["grade"] == PayoffGrade.EXCEPTIONAL


def test_right_tail_recall_precision_misses_and_small_sample():
    rows = [
        {"token_address": "a", "peak_multiple": 20, "highest_tier": "QUALIFIED_SIGNAL"},
        {"token_address": "b", "peak_multiple": 10, "highest_tier": "PRIORITY_RADAR"},
        {"token_address": "c", "peak_multiple": 5, "highest_tier": "DISCOVERED"},
        {"token_address": "d", "peak_multiple": 0.2, "highest_tier": "QUALIFIED_SIGNAL"},
    ]
    result = right_tail_metrics(rows, min_sample=30)
    assert result["recall_10x"] == 100
    assert result["qualified_2x_precision"] == 50
    assert result["missed_runners"] == ["c"]
    assert result["small_sample"]


def test_no_lookahead_boundary_excludes_future_outcome():
    rows = [
        {"observed_at": "2026-01-01T00:00:00+00:00", "price": 1},
        {"observed_at": "2026-01-01T00:05:00+00:00", "price": 10},
    ]
    assert filter_as_of(rows, "2026-01-01T00:01:00+00:00") == rows[:1]


def test_staged_enrichment_conflicts_cohorts_mae_exitability_and_miss_attribution():
    assert [evaluation_stage(value) for value in (0, 30, 120, 300, 900)] == [
        "T0",
        "T+30S",
        "T+2M",
        "T+5M",
        "LATER",
    ]
    assert enrichment_level({"market": {}, "safety": {}}) == 1
    assert provider_consensus({"a": 1, "b": 2})["state"] == "CONFLICTED"
    assert provider_consensus({"a": None, "b": None})["state"] == "UNKNOWN"
    cohorts = buyer_cohort_metrics(
        [
            WalletBuy(
                f"w{i}",
                f"2026-01-01T00:00:{i:02d}+00:00",
                sold_percent=0 if i < 8 else 100,
            )
            for i in range(10)
        ]
    )
    assert cohorts[0]["retained_count"] == 8 and cohorts[1]["state"] == "UNKNOWN"
    mae = maximum_adverse_excursion(
        [
            {"observed_at": "2026-01-01T00:00:00+00:00", "price": 0.8},
            {"observed_at": "2026-01-01T00:01:00+00:00", "price": 2.0},
        ],
        1,
        2,
    )
    assert mae["maximum_adverse_excursion"] == pytest.approx(0.2)
    assert liquidity_quality(2_000)["guaranteed_fill"] is False
    assert miss_analysis({"discovered": True, "score": 60, "threshold": 65})["category"] == (
        "THRESHOLD_FALSE_NEGATIVE"
    )


def test_capital_rotation_foundation():
    assert capital_rotation({"cats": 100, "dogs": 20}, {"cats": 40, "dogs": 80}) == [
        {"from": "cats", "to": "dogs", "strength": 60}
    ]


@pytest.mark.asyncio
async def test_solana_pump_creation_fixture_extracts_new_mint():
    transaction = {
        "result": {
            "meta": {
                "err": None,
                "preTokenBalances": [],
                "postTokenBalances": [{"mint": "new-mint"}],
            }
        }
    }
    assert extract_new_mint(transaction) == "new-mint"

    class Client:
        timeout = 1

        async def request(self, *_args, **_kwargs):
            return transaction

    source = SolanaProgramLaunchSource("https://rpc.example", ["program"], Client())
    event = await source.parse_notification(
        {
            "params": {
                "result": {
                    "context": {"slot": 7},
                    "value": {
                        "signature": "sig",
                        "err": None,
                        "logs": ["Program log: Instruction: Create"],
                    },
                }
            }
        },
        "program",
    )
    assert event and event.token_address == "new-mint" and event.slot_or_block == "7"


@pytest.mark.asyncio
async def test_bnb_factory_polling_fixture_and_source_ordering():
    token = "1234567890abcdef1234567890abcdef12345678"

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
                        "topics": ["0xevent", "0x" + "0" * 24 + token],
                    }
                ]
            }

    source = EvmFactoryLaunchSource("https://bsc.example", ["0xfactory"], ["0xevent"], Client())
    events = await source.poll_once()
    assert events[0].token_address == "0x" + token
    assert events[0].metadata["timestamp_source"] == "block_timestamp"
    replayed = LaunchEvent.deterministic(
        source.name,
        "bsc",
        events[0].token_address,
        iso(),
        transaction_id="0xtx",
    )
    assert replayed.event_key == events[0].event_key
    assert source.next_block == 17


@pytest.mark.asyncio
async def test_fourmeme_tokencreate_abi_derived_fixture_and_log_dedupe():
    """Synthetic ABI-derived fixture; this is not represented as a mainnet receipt."""
    creator = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    token = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    word = lambda address: "0" * 24 + address

    class Client:
        async def request(self, _url, _method, payload):
            if payload["method"] == "eth_blockNumber":
                return {"result": "0x20"}
            if payload["method"] == "eth_getBlockByNumber":
                return {"result": {"timestamp": "0x65000000"}}
            return {
                "result": [
                    {
                        "address": "0xfactory",
                        "blockNumber": "0x20",
                        "transactionHash": "0xtx",
                        "logIndex": "0x3",
                        "topics": ["0xtokencreate"],
                        "data": "0x" + word(creator) + word(token),
                    }
                ]
            }

    source = EvmFactoryLaunchSource(
        "https://bsc.example",
        ["0xfactory"],
        ["0xtokencreate"],
        Client(),
        token_data_word_index=1,
        creator_data_word_index=0,
    )
    event = (await source.poll_once())[0]
    assert event.token_address == "0x" + token
    assert event.creator_address == "0x" + creator
    assert event.transaction_id == "0xtx:0x3"
    assert event.metadata["address_encoding"] == "abi_event_data"


@pytest.mark.asyncio
async def test_event_to_genesis_exactly_one_alert_and_two_restarts():
    class Null:
        async def send(self, _content):
            return "ok"

    with temp_db_path() as path:
        db = store(path)
        config = settings(path)
        service = IntelligenceService(config, db, object(), object(), object(), Null())
        event = launch()
        assert await service.handle_launch_event(event) == "GENESIS_RADAR"
        assert await service.handle_launch_event(event) == "DUPLICATE"
        assert db.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
        assert db.conn.execute("SELECT COUNT(*) FROM immutable_call_snapshots").fetchone()[0] == 1
        candidate_id = db.conn.execute("SELECT id FROM candidates").fetchone()[0]
        staged = t0_decision({"launch_event_verified": True, "age_seconds": 20})
        staged.stage = "T+30S"
        db.record_v14_decision(candidate_id, None, staged, config)
        assert {
            row[0]
            for row in db.conn.execute("SELECT DISTINCT metric FROM latency_observations_v14")
        } >= {"T0_DECISION", "STAGED_DECISION"}
        db.close()
        for _ in range(2):
            db = store(path)
            service = IntelligenceService(config, db, object(), object(), object(), Null())
            assert await service.handle_launch_event(event) == "DUPLICATE"
            assert db.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
            assert db.reconcile_v14_state()["difference"] == 0
            db.close()


@pytest.mark.asyncio
async def test_manual_scan_is_parallel_read_only_and_watchlist_is_separate():
    class Market:
        async def market_snapshot(self, address, chain):
            await asyncio.sleep(0.01)
            return MarketSnapshot(
                address,
                iso(),
                "fixture",
                chain=chain,
                symbol="TST",
                market_cap_usd=25_000,
                liquidity_usd=15_000,
                volume_5m_usd=10_000,
            )

    class Safety:
        async def safety(self, chain, _address):
            await asyncio.sleep(0.01)
            return SafetyAssessment(iso(), "fixture", chain=chain)

    with temp_db_path() as path:
        db = store(path)
        service = IntelligenceService(settings(path), db, object(), Market(), Safety(), object())
        result = await service.manual_scan("mint", "solana", 1, 2)
        assert result["state"] == "FOUND"
        assert db.conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM manual_scans").fetchone()[0] == 1
        assert db.add_watch(1, 2, "solana", "mint")
        assert not db.add_watch(1, 2, "solana", "mint")
        assert len(db.user_watchlist(1, 2)) == 1
        assert db.remove_watch(1, 2, "solana", "mint")
        db.close()


def test_discord_cards_are_branded_mobile_safe_and_have_no_raw_json():
    scan = {
        "token_address": "mint",
        "chain": "solana",
        "state": "FOUND",
        "entry_state": "EARLY",
        "market": {"symbol": "TST", "market_cap_usd": 10_000, "liquidity_usd": 8_000},
        "survival": {"grade": "ACCEPTABLE"},
        "payoff": {"grade": "CONVEX"},
        "providers": {"market": {"state": "HEALTHY"}},
        "unknowns": [],
    }
    cards = [menu_card(), scan_card(scan), compare_card(scan, scan)]
    for value in cards:
        embed = value["embed"]
        assert embed["color"] == 0xD96B1D
        assert len(embed["title"]) <= 256
        assert len(embed.get("description", "")) <= 4096
        assert len(embed.get("fields", [])) <= 25
        assert "{" not in embed.get("description", "")


def test_1000_candidate_scheduler_prevents_fresh_starvation():
    with temp_db_path() as path:
        db = store(path)
        now = iso()
        with db.conn:
            db.conn.executemany(
                "INSERT INTO tokens(id,chain,token_address,source,first_discovered_at) VALUES(?,?,?,?,?)",
                [
                    (index, "solana" if index % 2 else "bsc", f"token-{index}", "stress", now)
                    for index in range(1, 1002)
                ],
            )
            db.conn.executemany(
                "INSERT INTO candidates(id,token_id,state,reason,first_discovered_at,scoring_version,created_at,"
                "updated_at,attempt_count,consecutive_missing_pair_count,next_retry_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        index,
                        index,
                        "PENDING_EVIDENCE" if index <= 1000 else "DISCOVERED",
                        "PAIR_NOT_AVAILABLE",
                        now,
                        "stress",
                        now,
                        now,
                        9 if index <= 1000 else 0,
                        9 if index <= 1000 else 0,
                        None,
                    )
                    for index in range(1, 1002)
                ],
            )
        started = time.perf_counter()
        chosen = db.active_candidates(25, 25, fresh_reserved=5)
        elapsed_ms = (time.perf_counter() - started) * 1000
        assert any(row["token_address"] == "token-1001" for row in chosen)
        assert len(chosen) <= 25
        assert elapsed_ms < 1_000
        db.close()


def test_10000_event_database_dedupe_is_bounded_and_responsive():
    with temp_db_path() as path:
        db = store(path)
        events = [launch(index) for index in range(10_000)]
        started = time.perf_counter()
        first = db.record_launch_events(events)
        second = db.record_launch_events(events)
        elapsed = time.perf_counter() - started
        assert first == {"inserted": 10_000, "duplicates": 0}
        assert second == {"inserted": 0, "duplicates": 10_000}
        assert db.conn.execute("SELECT COUNT(*) FROM launch_events").fetchone()[0] == 10_000
        assert elapsed < 10
        db.close()


def test_10000_event_queue_backpressure_has_no_memory_runaway():
    queue = BoundedLaunchQueue(512)
    results = [queue.offer(launch(index)) for index in range(10_000)]
    assert results.count("QUEUED") == 512
    assert results.count("BACKPRESSURE") == 9_488
    assert queue.queue.qsize() == queue.queue.maxsize == 512


def test_latency_percentiles_are_real_measurements():
    result = latency_summary([1, 2, 3, 4, 100])
    assert result == {"count": 5, "p50_ms": 3, "p95_ms": pytest.approx(80.8), "max_ms": 100}

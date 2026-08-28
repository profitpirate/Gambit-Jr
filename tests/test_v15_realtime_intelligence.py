from __future__ import annotations

import asyncio
import base64
import json
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from memecoin_bot.config import Settings
from memecoin_bot.database import Store
from memecoin_bot.providers.dexscreener import DexScreenerProvider
from memecoin_bot.providers.launch_events import EvmFactoryLaunchSource
from memecoin_bot.realtime import CanonicalEvent, CanonicalEventFabric, CanonicalEventType
from memecoin_bot.realtime.features import RealtimeFeatureProjector
from memecoin_bot.realtime.learning import AdaptiveLearningLab
from memecoin_bot.realtime.providers import (
    EvmFactoryRealtimeSource,
    HeliusCuratedSource,
    NativePumpFunSource,
    PumpCurveAccountSource,
    PumpPortalSource,
    _rate_limited,
)
from memecoin_bot.realtime.pumpfun import (
    BONDING_CURVE_DISCRIMINATOR,
    CREATE_EVENT_DISCRIMINATOR,
    PUMP_PROGRAM_ID,
    TRADE_EVENT_DISCRIMINATOR,
    BorshDecodeError,
    anchor_events_from_logs,
    b58encode,
    decode_anchor_event,
    decode_bonding_curve_account,
    jito_tip_evidence,
)
from memecoin_bot.service import IntelligenceService

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


@pytest.fixture
def realtime_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "realtime.db", Path("migrations"))
    store.migrate()
    yield store
    store.close()


def _event(
    kind: CanonicalEventType,
    *,
    source: str = "native",
    token: str = "TokenRealtime111",
    seconds: float = 0,
    signature: str | None = None,
    source_id: str | None = None,
    payload: dict | None = None,
    slot: int | None = None,
) -> CanonicalEvent:
    timestamp = (NOW + timedelta(seconds=seconds)).isoformat()
    return CanonicalEvent.create(
        kind,
        token,
        "solana",
        "pumpfun",
        source,
        timestamp,
        received_timestamp=timestamp,
        available_timestamp=timestamp,
        transaction_signature=signature,
        source_event_id=source_id,
        slot_or_block=slot,
        raw_provenance={"fixture": True},
        payload=payload or {},
    )


def _borsh_string(value: str) -> bytes:
    encoded = value.encode()
    return struct.pack("<I", len(encoded)) + encoded


def _pubkey(seed: int) -> bytes:
    return bytes([seed]) * 32


def _create_event_bytes() -> bytes:
    return b"".join(
        (
            CREATE_EVENT_DISCRIMINATOR,
            _borsh_string("Runner"),
            _borsh_string("RUN"),
            _borsh_string("https://example.invalid/metadata.json"),
            _pubkey(1),
            _pubkey(2),
            _pubkey(3),
            _pubkey(4),
            struct.pack("<q", int(NOW.timestamp())),
            struct.pack("<Q", 1_073_000_000_000_000),
            struct.pack("<Q", 30_000_000_000),
            struct.pack("<Q", 793_100_000_000_000),
            struct.pack("<Q", 1_000_000_000_000_000),
            _pubkey(5),
            b"\0\0",
            bytes(32),
            struct.pack("<Q", 30_000_000_000),
        )
    )


def _trade_event_bytes() -> bytes:
    return b"".join(
        (
            TRADE_EVENT_DISCRIMINATOR,
            _pubkey(1),
            struct.pack("<Q", 2_000_000_000),
            struct.pack("<Q", 100_000_000),
            b"\1",
            _pubkey(9),
            struct.pack("<q", int((NOW + timedelta(seconds=5)).timestamp())),
            struct.pack("<Q", 32_000_000_000),
            struct.pack("<Q", 1_000_000_000_000_000),
            struct.pack("<Q", 2_000_000_000),
            struct.pack("<Q", 700_000_000_000_000),
        )
    )


def test_current_and_legacy_pump_curve_decoding_preserves_real_vs_virtual_units() -> None:
    legacy = b"".join(
        (
            BONDING_CURVE_DISCRIMINATOR,
            struct.pack("<QQQQQ", 1_000, 2_000, 3_000, 4_000, 5_000),
            b"\0",
            _pubkey(7),
        )
    )
    decoded = decode_bonding_curve_account(legacy)
    assert decoded["account_layout"] == "LEGACY_SOL_V1"
    assert decoded["virtual_sol_reserves"] == 2_000
    assert decoded["real_sol_reserves"] == 4_000
    assert decoded["real_token_reserves"] == 3_000

    non_sol_quote = _pubkey(8)
    current = legacy + b"\1\0" + non_sol_quote
    decoded = decode_bonding_curve_account(current)
    assert decoded["account_layout"] == "QUOTE_AWARE_V3"
    assert decoded["quote_mint"] == b58encode(non_sol_quote)
    assert decoded["real_quote_reserves"] == 4_000
    assert decoded["real_sol_reserves"] is None
    assert decoded["virtual_sol_reserves"] is None


@pytest.mark.parametrize(
    "raw",
    [b"", b"short", bytes(80), b"wrong!!!" + bytes(100)],
)
def test_pump_curve_decoder_rejects_invalid_accounts(raw: bytes) -> None:
    with pytest.raises(BorshDecodeError):
        decode_bonding_curve_account(raw)


def test_current_anchor_create_and_trade_prefixes_decode() -> None:
    create = decode_anchor_event(_create_event_bytes())
    assert create and create["anchor_event"] == "CreateEvent"
    assert create["mint"] == b58encode(_pubkey(1))
    assert create["creator"] == b58encode(_pubkey(4))
    assert create["real_sol_reserves"] == 0
    trade = decode_anchor_event(_trade_event_bytes())
    assert trade and trade["anchor_event"] == "TradeEvent"
    assert trade["is_buy"] is True
    assert trade["real_sol_reserves"] == 2_000_000_000
    assert trade["unparsed_tail_bytes"] == 0


def test_native_transaction_parser_emits_launch_trade_wallet_curve_and_bundle_events() -> None:
    source = NativePumpFunSource("https://example.invalid", object())
    create_log = "Program data: " + base64.b64encode(_create_event_bytes()).decode()
    trade_log = "Program data: " + base64.b64encode(_trade_event_bytes()).decode()
    logs = [
        f"Program {PUMP_PROGRAM_ID} invoke [1]",
        create_log,
        trade_log,
        f"Program {PUMP_PROGRAM_ID} success",
    ]
    transaction = {
        "result": {
            "blockTime": int(NOW.timestamp()),
            "meta": {"err": None, "logMessages": logs},
            "transaction": {
                "message": {
                    "accountKeys": [{"pubkey": b58encode(_pubkey(9)), "signer": True}],
                    "instructions": [
                        {
                            "parsed": {
                                "info": {
                                    "source": b58encode(_pubkey(9)),
                                    "destination": "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
                                    "lamports": 100_000,
                                }
                            }
                        }
                    ],
                }
            },
        }
    }
    events = source.events_from_transaction(transaction, "Signature111", 500)
    kinds = [event.event_type for event in events]
    assert kinds.count(CanonicalEventType.TOKEN_CREATED) == 1
    assert CanonicalEventType.TOKEN_TRADE in kinds
    assert CanonicalEventType.WALLET_BUY in kinds
    assert CanonicalEventType.BONDING_CURVE_STATE in kinds
    assert CanonicalEventType.BUNDLE_EVIDENCE in kinds
    trade = next(event for event in events if event.event_type == CanonicalEventType.TOKEN_TRADE)
    assert trade.payload["sol_amount"] == 2
    assert trade.payload["bundle_evidence_state"] == "PROBABILISTIC_TIP_EVIDENCE"
    assert trade.payload.get("exact_bundle_id") is None


def test_anchor_log_parser_rejects_wrong_program_partial_and_malformed_data() -> None:
    create_log = "Program data: " + base64.b64encode(_create_event_bytes()).decode()
    wrong_program = "WrongProgram111111111111111111111111111111"
    logs = [
        f"Program {PUMP_PROGRAM_ID} invoke [1]",
        "Program data: not-valid-base64!",
        f"Program {wrong_program} invoke [2]",
        create_log,
        f"Program {wrong_program} success",
        "Program data: " + base64.b64encode(CREATE_EVENT_DISCRIMINATOR).decode(),
        create_log,
        f"Program {PUMP_PROGRAM_ID} success",
    ]
    events = anchor_events_from_logs(logs, PUMP_PROGRAM_ID)
    assert [event["anchor_event"] for event in events] == ["CreateEvent"]


def test_native_transaction_parser_rejects_failed_transaction() -> None:
    source = NativePumpFunSource("https://example.invalid", object())
    transaction = {
        "result": {
            "meta": {
                "err": {"InstructionError": [0, "Custom"]},
                "logMessages": [
                    f"Program {PUMP_PROGRAM_ID} invoke [1]",
                    "Program data: " + base64.b64encode(_create_event_bytes()).decode(),
                ],
            }
        }
    }
    assert source.events_from_transaction(transaction, "failed-signature", 500) == []


def test_jito_evidence_ignores_non_mapping_parsed_instruction_shapes() -> None:
    transaction = {
        "result": {
            "transaction": {
                "message": {
                    "instructions": [
                        {"parsed": "unsupported"},
                        {"parsed": {"info": "unsupported"}},
                        "unsupported",
                    ]
                }
            }
        }
    }
    result = jito_tip_evidence(transaction, {"Tip111"})
    assert result["jito_tip_present"] is False
    assert result["bundle_evidence_state"] == "NO_TIP_OBSERVED"


def test_native_and_pumpportal_same_launch_reconcile_once(realtime_store: Store) -> None:
    fabric = CanonicalEventFabric(realtime_store)
    native = _event(
        CanonicalEventType.TOKEN_CREATED,
        source="solana_pumpfun_native",
        signature="native-signature",
        payload={"creator": "Creator111", "bonding_curve": "Curve111"},
    )
    portal = PumpPortalSource("test-key").parse_message(
        {
            "mint": native.canonical_token,
            "txType": "create",
            "signature": "portal-signature",
            "traderPublicKey": "Creator111",
            "bondingCurveKey": "Curve111",
            "timestamp": NOW.timestamp(),
        },
        NOW.isoformat(),
    )[0]
    first = fabric.publish(native)
    second = fabric.publish(portal)
    assert first.is_new is True
    assert second.status == "CONFIRMED"
    assert realtime_store.conn.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0] == 1
    assert (
        realtime_store.conn.execute("SELECT COUNT(*) FROM canonical_event_sources").fetchone()[0]
        == 2
    )


def test_conflicting_provider_payload_is_preserved_not_silently_overwritten(
    realtime_store: Store,
) -> None:
    fabric = CanonicalEventFabric(realtime_store)
    first = _event(
        CanonicalEventType.TOKEN_CREATED,
        source="native",
        payload={"creator": "CreatorA", "bonding_curve": "Curve111"},
    )
    conflict = _event(
        CanonicalEventType.TOKEN_CREATED,
        source="portal",
        payload={"creator": "CreatorB", "bonding_curve": "Curve111"},
    )
    fabric.publish(first)
    result = fabric.publish(conflict)
    assert result.conflict is True and result.status == "CONFLICT"
    assert realtime_store.conn.execute(
        "SELECT COUNT(*) FROM canonical_event_conflicts"
    ).fetchone()[0] == 1
    stored = realtime_store.conn.execute("SELECT payload_json FROM canonical_events").fetchone()[0]
    assert json.loads(stored)["creator"] == "CreatorA"


def test_out_of_order_curve_events_preserve_latest_state_and_pit_feature_time(
    realtime_store: Store,
) -> None:
    fabric = CanonicalEventFabric(realtime_store)
    create = _event(
        CanonicalEventType.TOKEN_CREATED,
        payload={
            "creator": "Creator111",
            "bonding_curve": "Curve111",
            "real_token_reserves": 1_000,
            "initial_real_token_reserves": 1_000,
        },
    )
    fabric.publish(create)
    token_id, _ = fabric.project(create)
    newer = _event(
        CanonicalEventType.BONDING_CURVE_STATE,
        seconds=20,
        slot=102,
        source_id="curve:102",
        payload={
            "real_token_reserves": 700,
            "real_sol_reserves": 3_000_000_000,
            "real_quote_reserves": 3_000_000_000,
        },
    )
    older = _event(
        CanonicalEventType.BONDING_CURVE_STATE,
        seconds=10,
        slot=101,
        source_id="curve:101",
        payload={
            "real_token_reserves": 900,
            "real_sol_reserves": 1_000_000_000,
            "real_quote_reserves": 1_000_000_000,
        },
    )
    for event in (newer, older):
        fabric.publish(event)
        fabric.project(event)
    state = realtime_store.conn.execute(
        "SELECT latest_real_token_reserves,latest_real_quote_reserves FROM token_realtime_state"
    ).fetchone()
    assert tuple(state) == (700, 3_000_000_000)
    assert token_id is not None
    features = RealtimeFeatureProjector(realtime_store).compute(
        token_id, (NOW + timedelta(seconds=20)).isoformat()
    )
    assert features["capital_trajectory"]["real_sol_reserve"] == 3
    assert features["capital_trajectory"]["real_sol_velocity"] == pytest.approx(0.2)
    assert features["capital_trajectory"]["curve_progress"] == pytest.approx(0.3)


def test_trade_sequence_builds_real_buyer_first_sell_absorption_and_temperature(
    realtime_store: Store,
) -> None:
    fabric = CanonicalEventFabric(realtime_store)
    create = _event(
        CanonicalEventType.TOKEN_CREATED,
        payload={
            "creator": "Creator111",
            "bonding_curve": "Curve111",
            "real_token_reserves": 1_000,
        },
    )
    fabric.publish(create)
    token_id, _ = fabric.project(create)
    sequence = [
        (2, "A", "buy", 1.0),
        (5, "B", "buy", 2.0),
        (10, "Creator111", "buy", 0.5),
        (20, "A", "sell", 0.2),
        (22, "C", "buy", 0.8),
        (25, "D", "buy", 0.9),
    ]
    for index, (seconds, actor, side, amount) in enumerate(sequence):
        event = _event(
            CanonicalEventType.TOKEN_TRADE,
            seconds=seconds,
            signature=f"sig-{index}",
            source_id=f"sig-{index}:0",
            payload={"actor": actor, "side": side, "sol_amount": amount},
        )
        fabric.publish(event)
        fabric.project(event)
    assert token_id is not None
    feature = RealtimeFeatureProjector(realtime_store).compute(
        token_id, (NOW + timedelta(seconds=30)).isoformat()
    )
    assert feature["buyer_arrival"]["raw_buyers"] == 5
    assert feature["buyer_arrival"]["adjusted_independent_buyers"] is None
    assert feature["buyer_arrival"]["linkage_state"] == "UNKNOWN"
    assert feature["first_sell"]["time_to_first_sell_seconds"] == 20
    assert feature["first_sell"]["buyers_after_first_meaningful_sell"] == 2
    assert feature["monitoring"]["state"] == "GENESIS"
    assert feature["activity_adjustment"]["creator_linked_events"] == 1


def test_helius_curated_wallet_buy_can_generate_candidate(realtime_store: Store) -> None:
    wallet = "WalletCurated111"
    source = HeliusCuratedSource("test-key", [wallet])
    payload = {
        "params": {
            "result": {
                "context": {"slot": 10},
                "value": {
                    "signature": "helius-signature",
                    "transaction": {"signatures": ["helius-signature"]},
                    "meta": {
                        "err": None,
                        "preTokenBalances": [
                            {"owner": wallet, "mint": "NewToken111", "uiTokenAmount": {"uiAmount": 0}}
                        ],
                        "postTokenBalances": [
                            {"owner": wallet, "mint": "NewToken111", "uiTokenAmount": {"uiAmount": 12}}
                        ],
                    },
                },
            }
        }
    }
    event = source.parse_message(payload, NOW.isoformat())[0]
    assert event.event_type == CanonicalEventType.WALLET_BUY
    fabric = CanonicalEventFabric(realtime_store)
    fabric.publish(event)
    token_id, candidate_id = fabric.project(event)
    assert token_id and candidate_id
    assert realtime_store.conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 1


def test_restart_claim_recovery_and_provider_health_truth(realtime_store: Store) -> None:
    fabric = CanonicalEventFabric(realtime_store)
    event = _event(CanonicalEventType.TOKEN_CREATED)
    fabric.publish(event)
    claimed = fabric.claim_pending()
    assert [row.event_id for row in claimed] == [event.event_id]
    realtime_store.conn.execute(
        "UPDATE canonical_events SET claimed_at=?",
        ((NOW - timedelta(minutes=10)).isoformat(),),
    )
    realtime_store.conn.commit()
    assert fabric.recover_stale_claims(120) == 1
    assert fabric.claim_pending()[0].event_id == event.event_id

    health = CanonicalEvent.create(
        CanonicalEventType.PROVIDER_HEALTH,
        "__provider__:pumpportal",
        "provider",
        "provider",
        "pumpportal",
        NOW.isoformat(),
        source_event_id="health:1",
        payload={
            "provider": "pumpportal",
            "state": "STALE",
            "error": "SILENCE_WATCHDOG_EXPIRED",
            "last_message_at": (NOW - timedelta(minutes=5)).isoformat(),
            "error_count": 1,
            "reconnect_attempts": 1,
        },
    )
    fabric.publish(health)
    fabric.project(health)
    row = realtime_store.conn.execute(
        "SELECT state,healthy,last_error FROM provider_health WHERE provider='pumpportal'"
    ).fetchone()
    assert tuple(row) == ("STALE", 0, "SILENCE_WATCHDOG_EXPIRED")


def test_rate_limit_detection_recognizes_rpc_and_http_429_without_false_positive() -> None:
    assert _rate_limited("HTTP 429 Too Many Requests") is True
    assert _rate_limited("RPC rate limit exceeded") is True
    assert _rate_limited("connection reset") is False


def test_hot_candidate_restart_restores_timeline_curve_target_and_dedupes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "hot-restart.db"
    store = Store(path, Path("migrations"))
    store.migrate()
    fabric = CanonicalEventFabric(store)
    create = _event(
        CanonicalEventType.TOKEN_CREATED,
        payload={
            "creator": "CreatorHot111",
            "bonding_curve": "CurveHot111",
            "real_token_reserves": 1_000,
            "initial_real_token_reserves": 1_000,
        },
    )
    fabric.publish(create)
    token_id, _ = fabric.project(create)
    assert token_id
    for index in range(14):
        trade = _event(
            CanonicalEventType.TOKEN_TRADE,
            seconds=2 + index * 8,
            signature=f"hot-trade-{index}",
            source_id=f"hot-trade-{index}:0",
            payload={"actor": f"Buyer{index}", "side": "buy", "sol_amount": 0.25},
        )
        fabric.publish(trade)
        fabric.project(trade)
    for seconds, real_token, real_sol in ((100, 800, 8), (125, 600, 12)):
        curve = _event(
            CanonicalEventType.BONDING_CURVE_STATE,
            seconds=seconds,
            source_id=f"curve:{seconds}",
            slot=seconds,
            payload={
                "bonding_curve": "CurveHot111",
                "real_token_reserves": real_token,
                "real_sol_reserves": real_sol * 1_000_000_000,
            },
        )
        fabric.publish(curve)
        fabric.project(curve)
    feature = RealtimeFeatureProjector(store).compute(
        token_id, (NOW + timedelta(seconds=130)).isoformat()
    )
    assert feature["monitoring"]["state"] == "HOT"
    counts = {
        "events": store.conn.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0],
        "timeline": store.conn.execute(
            "SELECT COUNT(*) FROM token_event_timeline_v15"
        ).fetchone()[0],
        "curves": store.conn.execute("SELECT COUNT(*) FROM curve_observations_v15").fetchone()[0],
    }
    store.close()

    restarted = Store(path, Path("migrations"))
    restarted.migrate()
    restarted_fabric = CanonicalEventFabric(restarted)
    duplicate = restarted_fabric.publish(create)
    restored = RealtimeFeatureProjector(restarted).latest(
        token_id, (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    )
    assert duplicate.status == "DUPLICATE"
    assert restored and restored["monitoring"]["state"] == "HOT"
    assert restarted.realtime_curve_targets()[0]["bonding_curve_address"] == "CurveHot111"
    assert restarted.conn.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0] == counts[
        "events"
    ]
    assert restarted.conn.execute(
        "SELECT COUNT(*) FROM token_event_timeline_v15"
    ).fetchone()[0] == counts["timeline"]
    assert restarted.conn.execute("SELECT COUNT(*) FROM curve_observations_v15").fetchone()[0] == counts[
        "curves"
    ]
    assert restarted.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0
    assert restarted.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    assert restarted.conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    restarted.close()


def test_service_consumes_persistent_event_without_public_v3_route(realtime_store: Store) -> None:
    settings = Settings(database_path=realtime_store.path, realtime_fabric_enabled=True)
    service = IntelligenceService(
        settings,
        realtime_store,
        discovery=object(),
        market=object(),
        safety_provider=object(),
        notifier=object(),
    )
    event = _event(
        CanonicalEventType.TOKEN_CREATED,
        payload={
            "creator": "Creator111",
            "bonding_curve": "Curve111",
            "real_token_reserves": 1_000,
        },
    )
    service.realtime_fabric.publish(event)
    assert asyncio.run(service.handle_realtime_event(event)) == "PROCESSED"
    row = realtime_store.conn.execute(
        "SELECT processing_status,token_id FROM canonical_events"
    ).fetchone()
    assert row["processing_status"] == "PROCESSED" and row["token_id"] is not None
    assert realtime_store.conn.execute("SELECT COUNT(*) FROM launch_events").fetchone()[0] == 1
    assert realtime_store.conn.execute(
        "SELECT COUNT(*) FROM trajectory_feature_snapshots_v15"
    ).fetchone()[0] == 1
    assert realtime_store.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0
    latency = realtime_store.realtime_latency_report()
    assert latency["provider"]["count"] == 1
    assert latency["source_to_feature"]["count"] == 1
    assert latency["model"]["count"] == 1
    assert latency["source_to_decision"]["count"] == 1


def test_10k_canonical_duplicate_soak_has_no_duplicate_keys_or_corruption(
    realtime_store: Store,
) -> None:
    fabric = CanonicalEventFabric(realtime_store)
    total = 10_050
    for index in range(total):
        event = _event(
            CanonicalEventType.TOKEN_TRADE,
            token=f"Token{index % 50}",
            seconds=index / 100,
            signature=f"signature-{index}",
            source_id=f"signature-{index}:0",
            payload={"actor": f"wallet-{index % 500}", "side": "buy", "sol_amount": 0.01},
        )
        assert fabric.publish(event).is_new is True
        assert fabric.publish(event).status == "DUPLICATE"
    assert realtime_store.conn.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0] == total
    assert fabric.reconcile()["duplicate_canonical_keys"] == 0
    assert realtime_store.conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_adaptive_scheduler_does_not_poll_before_temperature_due_time(
    realtime_store: Store,
) -> None:
    fabric = CanonicalEventFabric(realtime_store)
    event = _event(CanonicalEventType.TOKEN_CREATED)
    fabric.publish(event)
    token_id, _ = fabric.project(event)
    assert token_id
    realtime_store.conn.execute(
        "UPDATE candidates SET next_monitor_at=? WHERE token_id=?",
        ((NOW + timedelta(minutes=1)).isoformat(), token_id),
    )
    assert realtime_store.active_candidates(now=NOW.isoformat()) == []
    assert len(
        realtime_store.active_candidates(now=(NOW + timedelta(minutes=2)).isoformat())
    ) == 1


def test_migration_continuity_is_point_in_time_and_uses_pre_post_flow(
    realtime_store: Store,
) -> None:
    fabric = CanonicalEventFabric(realtime_store)
    create = _event(
        CanonicalEventType.TOKEN_CREATED,
        payload={"real_token_reserves": 1_000, "bonding_curve": "Curve111"},
    )
    fabric.publish(create)
    token_id, _ = fabric.project(create)
    assert token_id
    events = [
        _event(
            CanonicalEventType.TOKEN_TRADE,
            seconds=50,
            signature="pre-buy",
            source_id="pre-buy:0",
            payload={"actor": "A", "side": "buy", "sol_amount": 2},
        ),
        _event(
            CanonicalEventType.BONDING_CURVE_STATE,
            seconds=55,
            slot=55,
            source_id="curve:55",
            payload={"real_sol_reserves": 10_000_000_000},
        ),
        _event(
            CanonicalEventType.MIGRATION_STARTED,
            seconds=60,
            signature="migration",
            source_id="migration:0",
        ),
        _event(
            CanonicalEventType.TOKEN_TRADE,
            seconds=70,
            signature="post-buy",
            source_id="post-buy:0",
            payload={"actor": "A", "side": "buy", "sol_amount": 1},
        ),
        _event(
            CanonicalEventType.TOKEN_TRADE,
            seconds=75,
            signature="post-sell",
            source_id="post-sell:0",
            payload={"actor": "B", "side": "sell", "sol_amount": 0.5},
        ),
        _event(
            CanonicalEventType.BONDING_CURVE_STATE,
            seconds=80,
            slot=80,
            source_id="curve:80",
            payload={"real_sol_reserves": 9_000_000_000},
        ),
    ]
    for event in events:
        fabric.publish(event)
        fabric.project(event)
    feature = RealtimeFeatureProjector(realtime_store).compute(
        token_id, (NOW + timedelta(seconds=90)).isoformat()
    )
    migration = feature["migration_continuity"]
    assert migration["state"] == "MEASURED"
    assert migration["liquidity_continuity"] == pytest.approx(0.9)
    assert migration["flow_survival"] == pytest.approx(0.5)
    assert migration["buyer_retention"] == 1
    assert migration["sell_shock"] == pytest.approx(1 / 3)


class _JsonClient:
    def __init__(self, responses: list[object]):
        self.responses = list(responses)
        self.urls: list[str] = []
        self.timeout = 1

    async def request(self, url: str, method: str = "GET", payload: dict | None = None):
        self.urls.append(url)
        return self.responses.pop(0)


def test_dexscreener_batches_up_to_30_and_maps_only_matching_tokens() -> None:
    pairs = [
        {
            "chainId": "solana",
            "pairAddress": "pair-a",
            "baseToken": {"address": "A", "symbol": "A"},
            "liquidity": {"usd": 12_000},
            "marketCap": 20_000,
        }
    ]
    client = _JsonClient([pairs, []])
    provider = DexScreenerProvider("https://example.invalid", client)
    addresses = [chr(65 + (index % 26)) + str(index) for index in range(31)]
    addresses[0] = "A"
    snapshots = asyncio.run(provider.market_snapshots(addresses, "solana"))
    assert len(client.urls) == 2
    assert client.urls[0].count(",") == 29
    assert snapshots["A"] and snapshots["A"].market_cap_usd == 20_000
    assert snapshots[addresses[1]] is None


def test_curve_initial_account_read_produces_t0_state() -> None:
    raw = b"".join(
        (
            BONDING_CURVE_DISCRIMINATOR,
            struct.pack("<QQQQQ", 1_000, 2_000, 3_000, 4_000, 5_000),
            b"\0",
            _pubkey(7),
        )
    )
    client = _JsonClient(
        [
            {
                "jsonrpc": "2.0",
                "result": {
                    "context": {"slot": 42},
                    "value": {"data": [base64.b64encode(raw).decode(), "base64"]},
                },
            }
        ]
    )
    source = PumpCurveAccountSource(
        "https://example.invalid", client, list
    )
    event = asyncio.run(
        source.initial_event(
            {"token_address": "Token111", "bonding_curve_address": "Curve111"}
        )
    )
    assert event and event.slot_or_block == "42"
    assert event.payload["real_sol_reserves"] == 4_000


def test_bnb_websocket_log_is_canonical_and_cursor_compatible() -> None:
    header_client = _JsonClient(
        [{"jsonrpc": "2.0", "result": {"timestamp": hex(int(NOW.timestamp()))}}]
    )
    poller = EvmFactoryLaunchSource(
        "https://example.invalid",
        ["0x" + "11" * 20],
        ["0x" + "22" * 32],
        header_client,
    )
    source = EvmFactoryRealtimeSource(poller)
    token = "ab" * 20
    event = asyncio.run(
        source._event_from_log(
            {
                "address": "0x" + "11" * 20,
                "blockNumber": "0x10",
                "transactionHash": "0x" + "33" * 32,
                "logIndex": "0x2",
                "topics": ["0x" + "22" * 32, "0x" + "00" * 12 + token],
                "data": "0x",
            }
        )
    )
    assert event and event.event_type == CanonicalEventType.TOKEN_CREATED
    assert event.canonical_token == "0x" + token
    assert event.raw_provenance["transport"] == "eth_subscribe_logs"


def test_learning_lab_never_approves_or_routes_synthetic_challenger(
    realtime_store: Store,
) -> None:
    rows = []
    for index in range(40):
        rows.append(
            {
                "entity_key": f"token-{index}",
                "decision_at": (NOW + timedelta(days=index)).isoformat(),
                "peak_multiple": 3 if index % 2 == 0 else 0.5,
                "terminal_failure": index % 2 == 1,
                "features": {"buyer_velocity": 2 if index % 2 == 0 else -1},
                "control_score": float(index % 5),
                "v3_score": float(index % 3),
                "stage": "EARLY_CURVE",
                "copyable": True,
                "stage_a_selected": True,
                "stage_b_selected": index % 3 != 0,
            }
        )
    result = AdaptiveLearningLab(realtime_store).run(
        rows,
        development_end=(NOW + timedelta(days=25)).isoformat(),
        validation_end=(NOW + timedelta(days=41)).isoformat(),
    )
    assert result["public_route"] is False
    assert result["advancement"] != "APPROVED"
    assert realtime_store.conn.execute(
        "SELECT SUM(public_route) FROM challenger_runs_v15"
    ).fetchone()[0] == 0
    assert realtime_store.conn.execute(
        "SELECT COUNT(*) FROM hypothesis_registry_v15 WHERE status='APPROVED'"
    ).fetchone()[0] == 0

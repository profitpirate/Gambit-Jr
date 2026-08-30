from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from memecoin_bot.discord import bot_runtime
from memecoin_bot.models import DiscoveryEvent, MarketSnapshot, SafetyAssessment, iso
from memecoin_bot.momentum import MomentumEngine
from memecoin_bot.service import IntelligenceService
from memecoin_bot.v15_engine import (
    EntryStatus,
    SignalTier,
    Stage,
    V15Decision,
    evaluate_v15,
)
from tests.helpers import settings, store, temp_db_path
from tests.test_candidate_lifecycle import EmptyDiscovery
from tests.test_discord_command_center import (
    FakeInteraction,
    FakeService,
    FakeStore,
    capture_runtime,
    primary_payload,
)


class SafeSafety:
    name = "safe-safety"

    async def safety(self, chain: str, address: str) -> SafetyAssessment:
        return SafetyAssessment(
            checked_at=iso(),
            source=self.name,
            chain=chain,
            top10_percent=20,
            holder_count=100,
        )


class FixedMarket:
    name = "fixed-market"

    def __init__(self, address: str, *, fail_batch: bool = False):
        self.address = address
        self.fail_batch = fail_batch
        self.snapshot = MarketSnapshot(
            token_address=address,
            captured_at=iso(),
            source=self.name,
            chain="solana",
            pair_address="pair-1",
            symbol="PIPE",
            name="Pipeline",
            pair_created_at=(datetime.now(UTC) - timedelta(minutes=2)).isoformat(),
            market_cap_usd=25_000,
            price_usd=0.000025,
            liquidity_usd=30_000,
            volume_5m_usd=20_000,
            buys_5m=60,
            sells_5m=10,
            price_change_5m=25,
        )

    async def market_snapshot(self, address: str, chain: str = "solana") -> MarketSnapshot:
        self.snapshot.captured_at = iso()
        return self.snapshot

    async def market_snapshots(self, addresses: list[str], chain: str = "solana"):
        if self.fail_batch:
            raise RuntimeError("synthetic batch outage")
        return {address: await self.market_snapshot(address, chain) for address in addresses}


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int | None, dict]] = []

    async def send_to(self, channel_id: int, content: dict) -> str:
        self.sent.append((channel_id, content))
        return f"message-{len(self.sent)}"

    async def send(self, content: dict) -> str:
        self.sent.append((None, content))
        return f"message-{len(self.sent)}"


def strong_decision(*_args, **_kwargs) -> V15Decision:
    return V15Decision(
        stage=Stage.NEW,
        runner_score=82,
        runner_grade="HIGH",
        failure_score=10,
        failure_grade="LOW",
        survival_grade="HIGH",
        setup_conviction=82,
        evidence_coverage=86,
        entry_status=EntryStatus.OPEN,
        signal_tier=SignalTier.STRONG,
        why_now=["tradeable liquidity", "momentum acceleration"],
        feature_vector={},
    )


def build_service(path, *, route_enabled: bool, guild_destination: bool, fail_batch: bool = False):
    config = settings(path)
    config.operator_shadow_alerts_enabled = route_enabled
    config.public_alerts_enabled = False
    config.min_snapshots_for_momentum = 1
    config.launch_source_reconnect_seconds = 0.01
    address = "Pipeline111111111111111111111111111111111"
    database = store(path)
    if guild_destination:
        database.set_guild_settings(
            101,
            202,
            True,
            "QUALIFIED_ONLY",
            303,
            False,
            ["solana"],
        )
    notifier = RecordingNotifier()
    service = IntelligenceService(
        config,
        database,
        EmptyDiscovery(),
        FixedMarket(address, fail_batch=fail_batch),
        SafeSafety(),
        notifier,
    )
    return service, database, notifier, address


def test_momentum_minimum_one_handles_empty_history_without_crashing() -> None:
    snapshot = MarketSnapshot(
        token_address="MomentumEmpty111111111111111111111111111111",
        captured_at=iso(),
        source="test",
        market_cap_usd=10_000,
        price_usd=0.00001,
        liquidity_usd=20_000,
        volume_5m_usd=5_000,
        buys_5m=20,
        sells_5m=5,
    )
    result = MomentumEngine().assess_history(snapshot, [], minimum=1)
    assert result["score"] is None
    assert result["reason"] == "ROLLING_HISTORY_NOT_YET_AVAILABLE"
    assert result["snapshots_required"] == 1


def test_critical_unknown_blocks_an_otherwise_routable_strong_call() -> None:
    from memecoin_bot.service import authoritative_signal_qualified

    assert not authoritative_signal_qualified(
        SignalTier.STRONG,
        ["CONCENTRATION_UNKNOWN"],
        [],
    )
    assert not authoritative_signal_qualified(
        SignalTier.STRONG,
        ["SELL_RESTRICTIONS_UNKNOWN"],
        [],
    )
    assert authoritative_signal_qualified(SignalTier.STRONG, [], [])


def test_four_of_seven_stage_lanes_is_a_strict_majority_for_strong() -> None:
    from memecoin_bot.v15_engine import STAGE_FEATURES

    required = STAGE_FEATURES[Stage.NEW]
    features = {
        name: 75 if index < 4 else None
        for index, name in enumerate(required)
    }
    features.update(call_market_cap=10_000, current_market_cap=11_000, age_minutes=5)
    result = evaluate_v15(Stage.NEW, features)
    assert result.evidence_coverage == pytest.approx(57.14, abs=0.01)
    assert result.signal_tier == SignalTier.STRONG


def test_age_backed_payoff_is_known_without_optional_price_change() -> None:
    from memecoin_bot.alpha_engine import SurvivalGrade, payoff_engine

    result = payoff_engine(
        {
            "market_cap_usd": 30_000,
            "liquidity_usd": 20_000,
            "price_change_from_launch_percent": None,
            "age_seconds": 120,
        },
        SurvivalGrade.STRONG,
    )
    assert result["score"] is not None
    assert str(result["grade"]) in {"CONVEX", "EXCEPTIONAL"}


def test_all_registered_commands_defer_before_work() -> None:
    assert bot_runtime.DEFERRED_COMMANDS == bot_runtime.EXPECTED_COMMAND_NAMES


def test_low_coverage_high_partial_score_is_silent_watch() -> None:
    from memecoin_bot.v15_engine import STAGE_FEATURES

    required = STAGE_FEATURES[Stage.NEW]
    features = {
        name: 95 if index < 3 else None
        for index, name in enumerate(required)
    }
    features.update(call_market_cap=10_000, current_market_cap=11_000, age_minutes=5)
    result = evaluate_v15(Stage.NEW, features)
    assert result.evidence_coverage < 55
    assert result.signal_tier == SignalTier.SILENT_WATCH
    assert "EVIDENCE_COVERAGE_BELOW_ROUTE_MINIMUM" in result.critical_unknowns


@pytest.mark.asyncio
async def test_test_alert_card_survives_optional_audit_failure() -> None:
    with patch.object(FakeStore, "record_test_alert", side_effect=RuntimeError("audit locked")):
        tree, client, _store = await capture_runtime()
        channel = FakeInteraction(admin=True).channel
        client.get_channel = lambda _channel_id: channel
        interaction = FakeInteraction(admin=True)
        interaction.channel = channel
        await tree.get_command("test-alert").callback(interaction)
        payload = primary_payload(interaction)
        assert "delivered" in payload["content"].lower()
        assert len(channel.messages) == 1
        assert channel.messages[0]["embed"].title == "GAMBIT JR • TEST ALERT"


@pytest.mark.asyncio
async def test_slow_command_is_acknowledged_then_fails_safely(monkeypatch) -> None:
    async def slow_scan(self, *_args, **_kwargs):
        await asyncio.sleep(0.1)
        return {}

    monkeypatch.setattr(bot_runtime, "COMMAND_TIMEOUT_SECONDS", 0.01)
    with patch.object(FakeService, "manual_scan", slow_scan):
        tree, _client, _store = await capture_runtime()
        interaction = FakeInteraction()
        await tree.get_command("scan").callback(interaction, "So111", "solana")
        assert interaction.response.deferred_at is not None
        payload = primary_payload(interaction)
        assert "safe time limit" in payload["content"]


@pytest.mark.asyncio
async def test_candidate_card_never_presents_partial_100_as_final_score() -> None:
    with patch.object(
        FakeStore,
        "candidates_report",
        return_value=[
            {
                "name": "Sparse Candidate",
                "chain": "solana",
                "state": "PENDING_EVIDENCE",
                "normalized_score": 100.0,
                "reason": "INSUFFICIENT_EVIDENCE",
                "route_state": "HOLD",
            }
        ],
    ):
        tree, _client, _store = await capture_runtime()
        interaction = FakeInteraction()
        await tree.get_command("candidates").callback(interaction)
        description = primary_payload(interaction)["embed"].description
        assert "score 100" not in description.lower()
        assert "developing setup" in description.lower()


@pytest.mark.asyncio
async def test_worker_supervisor_restarts_and_recovers() -> None:
    with temp_db_path() as path:
        service, database, _notifier, _address = build_service(
            path, route_enabled=False, guild_destination=False
        )
        attempts = 0

        async def flaky_worker() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("synthetic worker crash")
            service._mark_worker_cycle("synthetic", {"recovered": True})
            service.stop()

        await asyncio.wait_for(
            service._supervise_worker("synthetic", flaky_worker), timeout=1
        )
        assert attempts == 2
        assert service.worker_state["synthetic"]["restart_count"] == 1
        assert service.worker_state["synthetic"]["cycles"] == 1
        database.close()


@pytest.mark.asyncio
async def test_batch_provider_failure_falls_back_without_killing_candidate_monitor() -> None:
    with temp_db_path() as path:
        service, database, _notifier, address = build_service(
            path, route_enabled=False, guild_destination=False, fail_batch=True
        )
        token_id, _ = database.upsert_discovery(
            DiscoveryEvent(token_address=address, source="test")
        )
        database.ensure_candidate(token_id, iso(), service.settings.scoring_version)
        with patch("memecoin_bot.service.evaluate_v15", side_effect=strong_decision):
            result = await service.monitor_candidates_once()
        assert sum(result.values()) == 1
        assert database.conn.execute("SELECT COUNT(*) FROM runner_decisions_v15").fetchone()[0] == 1
        database.close()


@pytest.mark.asyncio
async def test_qualified_call_persists_when_delivery_route_is_disabled() -> None:
    with temp_db_path() as path:
        service, database, notifier, address = build_service(
            path, route_enabled=False, guild_destination=False
        )
        with patch("memecoin_bot.service.evaluate_v15", side_effect=strong_decision):
            await service.evaluate(DiscoveryEvent(token_address=address, source="test"))
        assert database.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 1
        decision = database.conn.execute(
            "SELECT tier,route_state,decision_reason FROM runner_decisions_v15 "
            "ORDER BY decision_at DESC LIMIT 1"
        ).fetchone()
        assert decision["tier"] == "STRONG"
        assert decision["route_state"] == "HOLD"
        assert decision["decision_reason"] == "ALERT_ROUTES_DISABLED"
        assert await service.flush_outbox() == 1
        assert notifier.sent == []
        outbox = database.conn.execute(
            "SELECT sent_at,remote_message_id,last_error FROM outbox WHERE event_type='SIGNAL'"
        ).fetchone()
        assert outbox["sent_at"] is not None
        assert outbox["remote_message_id"] == "route-suppressed:HOLD"
        assert outbox["last_error"] is None
        database.close()


@pytest.mark.asyncio
async def test_full_discovery_to_discord_pipeline_delivers_once() -> None:
    with temp_db_path() as path:
        # Explicit /setup-style guild configuration is sufficient consent for an
        # operator-shadow call even when the environment flag is false.
        service, database, notifier, address = build_service(
            path, route_enabled=False, guild_destination=True
        )
        with patch("memecoin_bot.service.evaluate_v15", side_effect=strong_decision):
            result = await service.evaluate(
                DiscoveryEvent(token_address=address, source="test")
            )
        assert result == "STRONG"
        assert database.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 1
        assert database.conn.execute(
            "SELECT route_state FROM runner_decisions_v15 ORDER BY decision_at DESC LIMIT 1"
        ).fetchone()[0] == "OPERATOR_SHADOW_ALERT"
        assert await service.flush_outbox() == 1
        assert len(notifier.sent) == 1
        assert notifier.sent[0][0] == 202
        assert await service.flush_outbox() == 0
        assert len(notifier.sent) == 1
        delivery = database.conn.execute(
            "SELECT status,attempts,remote_message_id FROM alert_deliveries_v131"
        ).fetchone()
        assert dict(delivery) == {
            "status": "SENT",
            "attempts": 1,
            "remote_message_id": "message-1",
        }
        diagnostics = database.status_stats(service.started_at)["pipeline"]
        assert diagnostics["enabled_alert_destinations"] == 1
        assert diagnostics["last_qualified"]["tier"] == "STRONG"
        database.close()

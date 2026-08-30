from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest

from memecoin_bot.config import Settings
from memecoin_bot.discord.command_center import MENU_PAGES, MenuView
from memecoin_bot.models import SafetyAssessment, iso
from memecoin_bot.providers.base import ProviderError
from memecoin_bot.providers.solana_rpc import SolanaSafetyFailoverProvider
from tests.test_discord_command_center import (
    FakeInteraction,
    FakeStore,
    capture_runtime,
    primary_payload,
)


@pytest.mark.asyncio
async def test_100_concurrent_status_commands_ack_before_slow_db_work() -> None:
    original = FakeStore.status_stats

    def slow_status(self, started_at):
        time.sleep(0.04)
        return original(self, started_at)

    with patch.object(FakeStore, "status_stats", slow_status):
        tree, _client, _store = await capture_runtime()
        command = tree.get_command("status")
        interactions = [FakeInteraction() for _ in range(100)]
        started = time.monotonic()
        await asyncio.gather(*(command.callback(item) for item in interactions))
        elapsed = time.monotonic() - started
        assert elapsed < 3.0
        assert all(item.response.deferred_at is not None for item in interactions)
        assert all("embed" in primary_payload(item) for item in interactions)


@pytest.mark.asyncio
async def test_menu_survives_250_repeated_component_navigations() -> None:
    tree, _client, _store = await capture_runtime()
    opened = FakeInteraction()
    await tree.get_command("menu").callback(opened)
    view = primary_payload(opened)["view"]
    assert isinstance(view, MenuView)
    assert view.timeout is None
    for index in range(250):
        interaction = FakeInteraction()
        page = MENU_PAGES[index % len(MENU_PAGES)]
        await view.navigate(interaction, page, "gambit:menu:navigate")
        assert interaction.response.deferred_at is not None
        assert len(interaction.edits) == 1
        assert not interaction.followup.messages


@pytest.mark.asyncio
async def test_50_test_alerts_deliver_exactly_once_each() -> None:
    tree, client, _store = await capture_runtime()
    channel = FakeInteraction(admin=True).channel
    client.get_channel = lambda _channel_id: channel
    command = tree.get_command("test-alert")
    for _ in range(50):
        interaction = FakeInteraction(admin=True)
        interaction.channel = channel
        await command.callback(interaction)
        assert "delivered" in primary_payload(interaction)["content"].lower()
    assert len(channel.messages) == 50
    assert all(message["embed"].title == "GAMBIT JR • TEST ALERT" for message in channel.messages)


class BrokenSafety:
    def __init__(self, name: str):
        self.name = name

    async def safety(self, _token_address: str):
        raise ProviderError(f"{self.name} unavailable")


class WorkingSafety:
    name = "working"

    async def safety(self, _token_address: str):
        return SafetyAssessment(
            checked_at=iso(),
            source=self.name,
            chain="solana",
            mint_authority=None,
            freeze_authority=None,
            top10_percent=20,
        )


@pytest.mark.asyncio
async def test_solana_safety_failover_recovers_after_two_rpc_failures() -> None:
    provider = SolanaSafetyFailoverProvider(
        [BrokenSafety("one"), BrokenSafety("two"), WorkingSafety()]  # type: ignore[list-item]
    )
    result = await provider.safety("So111")
    assert result.source == "working"
    assert "RPC_FAILOVER_USED:working" in result.warnings


def test_effective_solana_rpc_prefers_configured_nonpublic_sources(monkeypatch) -> None:
    config = Settings()
    config.solana_rpc_url = "https://api.mainnet-beta.solana.com"
    config.solana_tracker_rpc_url = "https://tracker.example/rpc"
    assert config.effective_solana_rpc_url() == "https://tracker.example/rpc"

    config.helius_api_key = "helius-key"
    assert "helius-rpc.com" in config.effective_solana_rpc_url()

    config.solana_rpc_url = "https://operator.example/rpc"
    assert config.effective_solana_rpc_url() == "https://operator.example/rpc"


def test_status_card_is_compact_and_surfaces_blockers() -> None:
    from memecoin_bot.discord.cards import status_card

    payload = status_card(
        {
            "status": "DEGRADED",
            "runtime": {"status": "HEALTHY"},
            "pending_evidence": 367,
            "active_signals": 0,
            "tokens_discovered": 35155,
            "tokens_evaluated": 22975,
            "signals": 0,
            "providers_healthy": 6,
            "providers_total": 7,
            "provider_status": [
                {"provider": "solana_rpc", "state": "CIRCUIT_OPEN"},
                {"provider": "helius_curated", "state": "NOT_CONFIGURED"},
            ],
            "discord_deliveries_failed": 0,
            "outbox_pending": 0,
            "last_alert_error": None,
            "pipeline": {
                "enabled_alert_destinations": 1,
                "last_decision": {
                    "tier": "SILENT_WATCH",
                    "decision_reason": "SAFETY_DATA_UNAVAILABLE",
                },
                "last_qualified": None,
                "top_blockers": [
                    {"reason": "SAFETY_DATA_UNAVAILABLE", "count": 199},
                    {"reason": "LIQUIDITY_TOO_LOW", "count": 80},
                ],
            },
        }
    )
    fields = payload["embed"]["fields"]
    assert len(fields) == 6
    text = " ".join(str(field["value"]) for field in fields)
    assert "SAFETY DATA UNAVAILABLE" in text.upper()
    assert "CIRCUIT OPEN" in text.upper()
    assert "helius curated" not in text.lower()

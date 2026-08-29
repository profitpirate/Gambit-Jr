from __future__ import annotations

import pytest

from memecoin_bot.discord import bot_runtime
from memecoin_bot.discord.cards import card
from memecoin_bot.discord.command_center import CommandCenterData, MenuView
from memecoin_bot.discord.product_policy import prepare_outbound_message
from memecoin_bot.signals import format_discord_event


def test_all_cards_include_made_by_jay() -> None:
    payload = card("TEST", "Evidence only")
    assert "Made by Jay" in payload["embed"]["footer"]["text"]


def test_legacy_radar_events_keep_contract_but_are_marked_internal_only() -> None:
    for event_type in (
        "GENESIS_RADAR",
        "EARLY_RADAR",
        "HOT_RADAR",
        "PRIORITY_RADAR",
        "RADAR_MILESTONE",
        "RADAR_RISK",
    ):
        payload = format_discord_event(event_type, {"token_address": "ExampleToken"})
        assert payload["_gambit_internal_event"] is True
        assert payload["event_type"] == event_type
        assert payload["embeds"]
        assert payload["content"]


def test_delivered_signal_uses_calls_first_language_and_human_text() -> None:
    raw = format_discord_event(
        "SIGNAL",
        {
            "v15_signal_tier": "STRONG",
            "classification": "STRONG",
            "name": "Example",
            "symbol": "EX",
            "token_address": "ExampleTokenAddress",
            "chain": "solana",
            "shadow": True,
            "confidence": 0.72,
            "evidence_coverage": 68,
            "runner_score": 88,
            "failure_score": 19,
            "why_now": [
                "VERIFIED_DIRECT_LAUNCH_EVENT",
                "bonding_curve_progress_percent",
                "buyer_count",
            ],
            "failure_reasons": ["liquidity_usd is still developing"],
        },
    )
    payload = prepare_outbound_message(raw)
    embed = payload["embeds"][0]
    names = {field["name"] for field in embed["fields"]}
    rendered = str(payload)

    assert embed["title"].startswith("GAMBIT JR — STRONG CALL")
    assert "Runner potential" not in names
    assert "Failure risk" not in names
    assert "Call category" in names
    assert "Evidence confidence" in names
    assert "Fresh launch confirmed on-chain" in rendered
    assert "bonding_curve_progress_percent" not in rendered
    assert "buyer_count" not in rendered
    assert "Made by Jay" in embed["footer"]["text"]


@pytest.mark.asyncio
async def test_menu_keeps_contract_and_refresh_has_no_invalid_emoji() -> None:
    view = MenuView(CommandCenterData(object(), object(), object()), timeout=900)
    refresh = next(
        child for child in view.children if child.custom_id == "gambit:menu:refresh"
    )
    assert view.timeout == 900
    assert refresh.emoji is None
    calls_option = next(
        option
        for child in view.children
        if hasattr(child, "options")
        for option in child.options
        if option.value == "radar"
    )
    assert calls_option.label == "Calls"


def test_all_commands_are_deferred_before_work() -> None:
    assert bot_runtime.DEFERRED_COMMANDS == bot_runtime.EXPECTED_COMMAND_NAMES

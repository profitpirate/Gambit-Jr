from __future__ import annotations

import logging
from types import SimpleNamespace

import discord
import pytest
from discord.webhook.async_ import async_context

from memecoin_bot.discord import bot_runtime
from memecoin_bot.discord.cards import (
    card,
    compare_card,
    creator_card,
    menu_card,
    narrative_card,
    performance_card,
    rows_card,
    scan_card,
    settings_card,
    smartmoney_card,
    status_card,
    token_card,
    wallet_card,
    watchlist_card,
)
from memecoin_bot.discord.cards import (
    test_alert_card as make_test_alert_card,
)
from memecoin_bot.discord.command_center import CommandCenterData, MenuView
from memecoin_bot.discord.responses import (
    SAFE_INTERNAL_ERROR,
    InteractionResponder,
    ResponseVisibility,
    respond_command_error,
)
from memecoin_bot.discord.validation import (
    DiscordPayloadValidationError,
    validate_card,
    validate_message,
    validate_view,
    validate_webhook_payload,
)
from memecoin_bot.signals import format_discord_event


def message_payload(data: dict | None = None) -> dict:
    data = data or {}
    return {
        "id": "555555555555555555",
        "channel_id": "222222222222222222",
        "author": {
            "id": "1539965607221395568",
            "username": "Gambit Jr",
            "discriminator": "0",
            "avatar": None,
        },
        "content": data.get("content") or "",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "edited_timestamp": None,
        "tts": False,
        "mention_everyone": False,
        "mentions": [],
        "mention_roles": [],
        "attachments": [],
        "embeds": data.get("embeds") or [],
        "pinned": False,
        "type": 20,
        "flags": data.get("flags") or 0,
        "components": data.get("components") or [],
        "webhook_id": "1539965607221395568",
    }


class StrictDiscordAdapter:
    """Discord transport boundary that validates actual discord.py serialized requests."""

    def __init__(self) -> None:
        self.acknowledged = False
        self.deferred = False
        self.ephemeral = False
        self.initial: list[dict] = []
        self.edits: list[dict] = []
        self.followups: list[dict] = []
        self.fail_edit: discord.HTTPException | None = None

    def validate_serialized(self, data: dict) -> None:
        assert set(data).issubset(
            {"content", "embeds", "components", "flags", "tts", "allowed_mentions", "attachments"}
        )
        content = data.get("content")
        if content is not None:
            assert isinstance(content, str) and 0 < len(content) <= 2000
        for embed in data.get("embeds") or []:
            validate_card({"embed": embed})
        rows = data.get("components") or []
        assert len(rows) <= 5
        ids = [
            component.get("custom_id")
            for row in rows
            for component in row.get("components", [])
            if component.get("custom_id")
        ]
        assert len(ids) == len(set(ids))
        assert content or data.get("embeds") or rows

    async def create_interaction_response(self, interaction_id, token, *, params, **_kwargs):
        assert token == "TEST_INTERACTION_TOKEN"
        assert not self.acknowledged, "Discord interactions can only be acknowledged once"
        payload = params.payload
        response_type = payload["type"]
        self.acknowledged = True
        if response_type == 5:
            self.deferred = True
            self.ephemeral = payload.get("data", {}).get("flags") == 64
        elif response_type == 4:
            data = payload["data"]
            self.validate_serialized(data)
            self.initial.append(data)
            self.ephemeral = bool(data.get("flags", 0) & 64)
        else:
            raise AssertionError(f"unexpected interaction response type {response_type}")
        return {
            "interaction": {
                "id": str(interaction_id),
                "response_message_loading": self.deferred,
                "response_message_ephemeral": self.ephemeral,
                "response_message_id": "555555555555555555",
            }
        }

    async def edit_original_interaction_response(self, application_id, token, *, payload, **_kwargs):
        assert application_id == 1539965607221395568
        assert token == "TEST_INTERACTION_TOKEN"
        assert self.acknowledged and self.deferred
        if self.fail_edit:
            raise self.fail_edit
        self.validate_serialized(payload)
        self.edits.append(payload)
        return message_payload(payload)

    async def execute_webhook(self, webhook_id, token, *, payload, wait, **_kwargs):
        assert webhook_id == 1539965607221395568
        assert token == "TEST_INTERACTION_TOKEN"
        assert self.acknowledged
        assert wait is True
        self.validate_serialized(payload)
        self.followups.append(payload)
        return message_payload(payload)


def actual_interaction() -> discord.Interaction:
    client = discord.Client(intents=discord.Intents.none())
    data = {
        "id": "123456789012345678",
        "application_id": "1539965607221395568",
        "type": 2,
        "token": "TEST_INTERACTION_TOKEN",
        "version": 1,
        "attachment_size_limit": 26214400,
        "locale": "en-GB",
        "data": {"id": "333333333333333333", "name": "menu", "type": 1},
        "user": {
            "id": "444444444444444444",
            "username": "tester",
            "discriminator": "0",
            "avatar": None,
            "global_name": "Tester",
        },
    }
    return discord.Interaction(data=data, state=client._connection)


class EmptyStore:
    def status_stats(self, _started):
        return {}

    def radar_board(self, _limit):
        return []

    def performance(self, *_args):
        return {}


class EmptyService:
    started_at = "2026-01-01T00:00:00+00:00"


@pytest.fixture
def responder_parts():
    interaction = actual_interaction()
    adapter = StrictDiscordAdapter()
    token = async_context.set(adapter)
    try:
        yield interaction, adapter
    finally:
        async_context.reset(token)


@pytest.mark.asyncio
async def test_immediate_original_response_uses_real_discord_response_serialization(responder_parts):
    interaction, adapter = responder_parts
    responder = InteractionResponder(
        interaction, "help", ResponseVisibility.PRIVATE, logging.getLogger("test.discord")
    )
    await responder.primary_text("Private help")
    assert len(adapter.initial) == 1
    assert adapter.initial[0]["content"] == "Private help"
    assert adapter.initial[0]["flags"] == 64
    assert adapter.edits == [] and adapter.followups == []


@pytest.mark.asyncio
async def test_discord_py_271_rejects_explicit_none_view_on_initial_send(responder_parts):
    interaction, adapter = responder_parts
    with pytest.raises(AttributeError, match="is_finished"):
        await interaction.response.send_message(embed=validate_card(menu_card()), view=None)
    assert adapter.acknowledged is True
    assert len(adapter.initial) == 1


@pytest.mark.asyncio
async def test_discord_py_271_rejects_explicit_none_view_on_followup(responder_parts):
    interaction, adapter = responder_parts
    await interaction.response.defer(ephemeral=True)
    with pytest.raises(TypeError, match="expected view parameter"):
        await interaction.followup.send(
            embed=validate_card(menu_card()), view=None, ephemeral=True, wait=True
        )
    assert adapter.followups == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("visibility", "ephemeral"),
    [(ResponseVisibility.PUBLIC, False), (ResponseVisibility.PRIVATE, True)],
)
async def test_deferred_primary_edits_original_with_matching_visibility(
    responder_parts, visibility, ephemeral
):
    interaction, adapter = responder_parts
    responder = InteractionResponder(
        interaction, "status", visibility, logging.getLogger("test.discord")
    )
    await responder.defer()
    await responder.primary_card(menu_card())
    assert adapter.ephemeral is ephemeral
    assert len(adapter.edits) == 1
    assert adapter.edits[0]["embeds"][0]["title"] == "GAMBIT JR • COMMAND CENTER"
    assert "tts" not in adapter.edits[0]
    assert "content" not in adapter.edits[0]
    assert "components" not in adapter.edits[0]
    assert adapter.followups == []


@pytest.mark.asyncio
async def test_primary_embed_and_menu_view_serialize_through_discord_py(responder_parts):
    interaction, adapter = responder_parts
    settings = SimpleNamespace(
        scoring_version="v1.5-runner-failure",
        major_missed_runner_multiple=10,
        min_sample_for_edge_metrics=30,
    )
    view = MenuView(CommandCenterData(EmptyService(), EmptyStore(), settings), timeout=900)
    responder = InteractionResponder(
        interaction, "menu", ResponseVisibility.PRIVATE, logging.getLogger("test.discord")
    )
    await responder.defer()
    await responder.primary_card(menu_card(), view)
    payload = adapter.edits[0]
    assert len(payload["embeds"]) == 1
    assert len(payload["components"]) == 2
    assert view.timeout == 900


@pytest.mark.asyncio
async def test_secondary_followup_is_separate_from_deferred_primary(responder_parts):
    interaction, adapter = responder_parts
    responder = InteractionResponder(
        interaction, "watch", ResponseVisibility.PRIVATE, logging.getLogger("test.discord")
    )
    await responder.defer()
    await responder.primary_text("Watchlist updated.")
    await responder.followup_text("Added to your watchlist.")
    assert len(adapter.edits) == 1
    assert len(adapter.followups) == 1


@pytest.mark.asyncio
async def test_duplicate_primary_response_is_rejected_before_transport(responder_parts):
    interaction, adapter = responder_parts
    responder = InteractionResponder(
        interaction, "status", ResponseVisibility.PUBLIC, logging.getLogger("test.discord")
    )
    await responder.defer()
    await responder.primary_text("First")
    with pytest.raises(RuntimeError, match="already completed"):
        await responder.primary_text("Second")
    assert len(adapter.edits) == 1


@pytest.mark.asyncio
async def test_safe_error_before_acknowledgement_uses_initial_response(responder_parts):
    interaction, adapter = responder_parts
    await respond_command_error(interaction, SAFE_INTERNAL_ERROR)
    assert adapter.initial[0]["content"] == SAFE_INTERNAL_ERROR
    assert adapter.initial[0]["flags"] == 64


@pytest.mark.asyncio
async def test_safe_error_after_defer_edits_original_response(responder_parts):
    interaction, adapter = responder_parts
    responder = InteractionResponder(
        interaction, "status", ResponseVisibility.PUBLIC, logging.getLogger("test.discord")
    )
    await responder.defer()
    await respond_command_error(interaction, SAFE_INTERNAL_ERROR)
    assert adapter.edits[0]["content"] == SAFE_INTERNAL_ERROR
    assert adapter.followups == []


class FailedResponse:
    status = 400
    reason = "Bad Request"
    method = "PATCH"
    url = "https://discord.com/api/v10/webhooks/1539965607221395568/SECRET_TOKEN/messages/@original"


@pytest.mark.asyncio
async def test_http_exception_logs_actionable_sanitized_diagnostics(responder_parts, caplog):
    interaction, adapter = responder_parts
    adapter.fail_edit = discord.HTTPException(
        FailedResponse(),
        {
            "code": 50035,
            "message": "Invalid Form Body token=SECRET_TOKEN",
            "errors": {"embeds": {"0": {"_errors": [{"message": "bad embed"}]}}},
        },
    )
    responder = InteractionResponder(
        interaction, "menu", ResponseVisibility.PRIVATE, logging.getLogger("test.discord")
    )
    await responder.defer()
    with (
        caplog.at_level(logging.ERROR, logger="test.discord"),
        pytest.raises(discord.HTTPException),
    ):
        await responder.primary_card(menu_card())
    assert "discord_response_failed" in caplog.text
    assert "SECRET_TOKEN" not in caplog.text
    record = next(record for record in caplog.records if record.message == "discord_response_failed")
    assert record.fields["discord_code"] == 50035
    assert record.fields["http_status"] == 400
    assert "[REDACTED]" in record.fields["discord_message"]


@pytest.mark.asyncio
async def test_invalid_embed_is_rejected_before_http_request(responder_parts):
    interaction, adapter = responder_parts
    responder = InteractionResponder(
        interaction, "menu", ResponseVisibility.PRIVATE, logging.getLogger("test.discord")
    )
    await responder.defer()
    invalid = card("Invalid")
    invalid["embed"]["description"] = "x" * 4097
    with pytest.raises(DiscordPayloadValidationError, match="description"):
        await responder.primary_card(invalid)
    assert adapter.edits == []


def test_menu_and_scan_views_serialize_without_invalid_components():
    settings = SimpleNamespace(
        scoring_version="v1.5-runner-failure",
        major_missed_runner_multiple=10,
        min_sample_for_edge_metrics=30,
    )
    menu = MenuView(CommandCenterData(EmptyService(), EmptyStore(), settings), timeout=900)
    scan = bot_runtime.ScanView(EmptyService(), EmptyStore(), "So111", "solana", timeout=900)
    menu_rows = validate_view(menu)
    scan_rows = validate_view(scan)
    assert len(menu_rows) == 2
    assert len(scan_rows) == 1
    assert len({item.custom_id for item in menu.children}) == len(menu.children)


def test_signal_card_has_full_contract_and_five_chain_aware_actions():
    payload = format_discord_event(
        "SIGNAL",
        {
            "classification": "QUALIFIED",
            "v15_signal_tier": "PREMIUM",
            "name": "Example Token",
            "symbol": "EXM",
            "chain": "solana",
            "token_address": "So11111111111111111111111111111111111111111",
            "component_scores": {
                "narrative": 10,
                "social": 10,
                "onchain": 10,
                "developer": 10,
                "momentum": 10,
                "safety": 5,
            },
            "component_maxima": {
                "narrative": 25,
                "social": 20,
                "onchain": 20,
                "developer": 15,
                "momentum": 15,
                "safety": 5,
            },
            "confidence": 0.8,
        },
    )
    validate_webhook_payload(payload)
    assert payload["embeds"][0]["title"] == "PREMIUM • Example Token ($EXM)"
    contract = next(
        field for field in payload["embeds"][0]["fields"] if field["name"] == "Contract address"
    )
    assert contract["value"].startswith("So111")
    components = payload["components"][0]["components"]
    assert [component["label"] for component in components] == [
        "Copy CA",
        "DexScreener",
        "Open GMGN",
        "Solscan",
        "Watch",
    ]


def test_every_card_and_component_payload_is_json_serializable():
    scan = scan_card(
        {
            "token_address": "So111",
            "chain": "solana",
            "market": {},
            "survival": {},
            "payoff": {},
            "providers": {},
        }
    )
    token = {
        "token_address": "So111",
        "chain": "solana",
        "symbol": "TEST",
        "name": "Test",
        "wallet_intelligence": {},
    }
    cards = [
        menu_card(),
        status_card({}),
        scan,
        compare_card(scan, scan),
        watchlist_card([]),
        wallet_card({"wallet": "Wallet111"}),
        creator_card(None, "Creator111"),
        narrative_card([]),
        token_card(token),
        smartmoney_card(token),
        rows_card("ROWS", [], "No rows.", str),
        performance_card({}),
        settings_card(None),
        make_test_alert_card(),
    ]
    assert all(validate_message(card_payload=payload) is not None for payload in cards)


def test_automatic_signal_payload_passes_shared_webhook_validator():
    payload = {
        "classification": "WATCH",
        "chain": "solana",
        "token_address": "So111",
        "name": "Test",
        "symbol": "TEST",
        "component_scores": {name: 1 for name in ("narrative", "social", "onchain", "developer", "momentum", "safety")},
        "component_maxima": {name: 1 for name in ("narrative", "social", "onchain", "developer", "momentum", "safety")},
        "developer": {},
        "narrative": {},
        "social": {},
        "momentum": {},
        "v15_signal_tier": "STRONG",
        "runner_score": 80,
        "failure_score": 10,
        "evidence_coverage": 85,
        "entry_status": "OPEN",
        "survival_grade": "HIGH",
    }
    message = format_discord_event("SIGNAL", payload)
    validate_webhook_payload(message)
    assert any(field["name"] == "Tier" for field in message["embeds"][0]["fields"])

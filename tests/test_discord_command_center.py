from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import pytest

from memecoin_bot.config import Settings
from memecoin_bot.database import Store
from memecoin_bot.discord import bot_runtime
from memecoin_bot.discord.command_center import (
    MENU_PAGES,
    PAGE_TITLES,
    SAFE_ERROR,
    CommandCenterData,
    MenuView,
)
from memecoin_bot.signals import format_discord_event


class FakeResponse:
    def __init__(self) -> None:
        self.done = False
        self.deferred_at: float | None = None
        self.messages: list[dict] = []

    def is_done(self) -> bool:
        return self.done

    async def defer(self, *, ephemeral: bool = False) -> None:
        self.done = True
        self.deferred_at = time.monotonic()
        self.messages.append({"kind": "defer", "ephemeral": ephemeral})

    async def send_message(self, content=None, **kwargs) -> None:
        self.done = True
        self.messages.append({"content": content, **kwargs})


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})
        return SimpleNamespace(id=99, edit=AsyncMock())


class FakeInteraction:
    def __init__(self, title: str | None = PAGE_TITLES["home"], *, admin: bool = False):
        self.guild_id = 101
        self.channel_id = 202
        self.channel = SimpleNamespace(id=202)
        self.user = SimpleNamespace(
            id=303, guild_permissions=SimpleNamespace(manage_guild=admin)
        )
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.edits: list[dict] = []
        embeds = [discord.Embed(title=title)] if title else []
        self.message = SimpleNamespace(embeds=embeds)
        self.command = None

    async def edit_original_response(self, **kwargs):
        self.edits.append(kwargs)
        if kwargs.get("embed"):
            self.message.embeds = [kwargs["embed"]]
        return SimpleNamespace(id=99, edit=AsyncMock())

    async def original_response(self):
        return SimpleNamespace(id=99, edit=AsyncMock())


def primary_payload(interaction: FakeInteraction) -> dict:
    payloads = [
        *interaction.edits,
        *(row for row in interaction.response.messages if row.get("kind") != "defer"),
    ]
    assert len(payloads) == 1
    return payloads[0]


class FakeStore:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.setup_called = False

    def status_stats(self, _started_at):
        self.calls.append("status_stats")
        return {
            "early_radar": 2,
            "active_signals": 1,
            "candidates_watching": 3,
            "pending_evidence": 4,
            "pending_over_1h": 0,
            "pending_over_3h": 0,
            "stale_beyond_ttl": 0,
            "tokens_discovered": 11,
            "tokens_evaluated": 9,
            "signals": 1,
            "expired": 2,
            "provider_status": [],
            "outbox_pending": 0,
            "discord_deliveries_pending": 0,
            "discord_deliveries_failed": 0,
            "state_reconciliation": {"difference": 0},
        }

    def radar_board(self, _limit):
        self.calls.append("radar_board")
        return [
            {
                "name": "Runner",
                "symbol": "RUN",
                "chain": "solana",
                "state": "SIGNALLED",
                "max_multiple": 3.5,
            }
        ]

    def performance(self, *_args):
        self.calls.append("performance")
        return {
            "total_signals": 1,
            "failed": 0,
            "median_max_multiple": 3.5,
            "2x_rate": 100.0,
            "5x_rate": 0.0,
            "10x_rate": 0.0,
        }

    def candidates_report(self, _limit):
        self.calls.append("candidates_report")
        return [{"name": "Candidate", "state": "CANDIDATE"}]

    def rejection_report(self, _since):
        self.calls.append("rejection_report")
        return {"hard": [("UNSAFE", 1)], "temporary": []}

    def missed_report(self, *_args):
        self.calls.append("missed_report")
        return []

    def cluster_report(self, _limit):
        self.calls.append("cluster_report")
        return [{"chain": "solana", "risk_state": "LOW", "member_count": 2}]

    def narrative_report(self, _query, _limit=10):
        self.calls.append("narrative_report")
        return [{"label": "AI", "freshness": "FRESH"}]

    def user_watchlist(self, _guild_id, _user_id):
        self.calls.append("user_watchlist")
        return [{"chain": "solana", "token_address": "So111"}]

    def token_intelligence(self, address):
        self.calls.append("token_intelligence")
        return {
            "token_address": address,
            "chain": "solana",
            "symbol": "TEST",
            "name": "Test Token",
            "state": "SIGNALLED",
            "signal_status": "ACTIVE",
            "confidence": 0.8,
            "wallet_intelligence": {},
        }

    def wallet_report(self, address):
        self.calls.append("wallet_report")
        return {"wallet": address, "nodes": [], "edges": [], "clusters": []}

    def creator_report(self, address):
        self.calls.append("creator_report")
        return {"creator": address, "quality": "UNKNOWN", "launches": 0}

    def right_tail_performance(self, _minimum):
        self.calls.append("right_tail_performance")
        return {"qualified_2x_precision": 0.5}

    def v15_performance(self, _minimum):
        self.calls.append("v15_performance")
        return {"decisions": 1}

    def v14_health(self):
        self.calls.append("v14_health")
        return {"event_queue_persisted": 1, "wallet_clusters": 1}

    def guild_settings(self, _guild_id):
        self.calls.append("guild_settings")
        return {
            "alerts_enabled": 1,
            "alert_channel_id": "202",
            "alert_tier": "HOT_PLUS",
            "daily_report_enabled": 0,
            "enabled_chains": ["solana", "bsc"],
            "updated_at": "now",
        }

    def set_guild_settings(self, *_args):
        self.setup_called = True

    def add_watch(self, *_args):
        self.calls.append("add_watch")
        return True

    def remove_watch(self, *_args):
        self.calls.append("remove_watch")
        return True

    def record_test_alert(self, *_args):
        self.calls.append("record_test_alert")


class FakeService:
    started_at = "2026-01-01T00:00:00+00:00"
    launch_queue = SimpleNamespace(stats=lambda: {"size": 0, "maxsize": 100})

    def __init__(self) -> None:
        self.stopped = False
        self.scans = 0

    async def run(self) -> None:
        return None

    def stop(self) -> None:
        self.stopped = True

    async def manual_scan(self, address, chain, guild_id, user_id):
        self.scans += 1
        return {
            "token_address": address,
            "chain": chain,
            "state": "OBSERVED",
            "entry_state": "UNKNOWN",
            "market": {},
            "survival": {},
            "payoff": {},
            "providers": {},
            "unknowns": [],
        }


@pytest.fixture
def command_center():
    settings = SimpleNamespace(
        scoring_version="v1.5-runner-failure",
        major_missed_runner_multiple=10,
        min_sample_for_edge_metrics=30,
    )
    store = FakeStore()
    service = FakeService()
    data = CommandCenterData(service, store, settings)
    return MenuView(data), data, store, service


def item(view: discord.ui.View, custom_id: str):
    return next(child for child in view.children if child.custom_id == custom_id)


def test_menu_is_persistent_mobile_component_tree_with_stable_ids(command_center):
    view, _, _, _ = command_center
    assert view.timeout == 900
    assert not view.is_persistent()
    assert MenuView(view.data, timeout=None).is_persistent()
    assert len(view.children) == 4
    assert [child.custom_id for child in view.children] == [
        "gambit:menu:home",
        "gambit:menu:back",
        "gambit:menu:refresh",
        "gambit:menu:navigate",
    ]
    select = item(view, "gambit:menu:navigate")
    assert [option.value for option in select.options] == list(MENU_PAGES)
    assert all(child.row in {0, 1} for child in view.children)


@pytest.mark.asyncio
async def test_every_category_navigates_with_ack_and_one_message_edit(command_center):
    view, _, _, _ = command_center
    for page in MENU_PAGES:
        interaction = FakeInteraction()
        await view.navigate(interaction, page, "gambit:menu:navigate")
        assert interaction.response.deferred_at is not None
        assert len(interaction.edits) == 1
        assert interaction.edits[0]["embed"].title == PAGE_TITLES[page]
        assert interaction.followup.messages == []


@pytest.mark.asyncio
async def test_select_callback_performs_real_navigation(command_center):
    view, _, _, _ = command_center
    select = item(view, "gambit:menu:navigate")
    select._values = ["radar"]
    interaction = FakeInteraction()
    await select.callback(interaction)
    assert interaction.edits[0]["embed"].title == PAGE_TITLES["radar"]


@pytest.mark.asyncio
async def test_home_back_and_refresh_edit_without_channel_spam(command_center):
    view, _, _, _ = command_center
    overview = FakeInteraction(PAGE_TITLES["overview"])
    await item(view, "gambit:menu:refresh").callback(overview)
    assert overview.edits[0]["embed"].title == PAGE_TITLES["overview"]

    back = FakeInteraction(PAGE_TITLES["radar"])
    await item(view, "gambit:menu:back").callback(back)
    assert back.edits[0]["embed"].title == PAGE_TITLES["home"]

    home = FakeInteraction(PAGE_TITLES["performance"])
    await item(view, "gambit:menu:home").callback(home)
    assert home.edits[0]["embed"].title == PAGE_TITLES["home"]
    assert not back.followup.messages and not home.followup.messages


@pytest.mark.asyncio
async def test_authoritative_store_methods_feed_pages_and_missing_values_are_unknown(command_center):
    view, _, store, _ = command_center
    for page in ("overview", "radar", "intelligence", "watchlist", "performance", "system", "settings"):
        await view.navigate(FakeInteraction(), page, "gambit:menu:navigate")
    assert {
        "status_stats",
        "radar_board",
        "performance",
        "candidates_report",
        "cluster_report",
        "narrative_report",
        "user_watchlist",
        "guild_settings",
    }.issubset(store.calls)

    store.status_stats = lambda _started: {}
    interaction = FakeInteraction()
    await view.navigate(interaction, "overview", "gambit:menu:navigate")
    assert "UNKNOWN" in interaction.edits[0]["embed"].to_dict()["fields"][0]["value"]


@pytest.mark.asyncio
async def test_slow_menu_data_is_deferred_before_work(command_center):
    view, data, _, _ = command_center
    render_started = None

    async def slow_render(page, interaction):
        nonlocal render_started
        render_started = time.monotonic()
        await asyncio.sleep(0.01)
        return await data._home(interaction)

    data.render = slow_render
    interaction = FakeInteraction()
    await view.navigate(interaction, "overview", "gambit:menu:navigate")
    assert interaction.response.deferred_at is not None
    assert interaction.response.deferred_at <= render_started


@pytest.mark.asyncio
async def test_callback_failure_is_safe_and_deferred_uses_followup(command_center, caplog):
    view, data, _, _ = command_center

    async def fail(_page, _interaction):
        raise RuntimeError("DISCORD_TOKEN=never-log-this")

    data.render = fail
    interaction = FakeInteraction()
    with caplog.at_level(logging.INFO, logger="memecoin_bot.discord"):
        await view.navigate(interaction, "overview", "gambit:menu:navigate")
    assert interaction.response.is_done()
    assert interaction.followup.messages[0]["content"] == SAFE_ERROR
    assert interaction.followup.messages[0]["ephemeral"] is True
    assert "never-log-this" not in caplog.text


@pytest.mark.asyncio
async def test_scan_refresh_and_watch_acknowledge_and_remain_functional(command_center):
    _, _, store, service = command_center
    store.add_watch = lambda *_args: True
    view = bot_runtime.ScanView(service, store, "So111", "solana")

    refresh = FakeInteraction()
    await item(view, "gambit:scan:refresh").callback(refresh)
    assert refresh.response.deferred_at is not None
    assert len(refresh.edits) == 1
    assert service.scans == 1

    watch = FakeInteraction()
    await item(view, "gambit:scan:watch").callback(watch)
    assert watch.response.deferred_at is not None
    assert watch.followup.messages[0]["content"] == "Added to your watchlist."


async def capture_runtime(*, start_hook=None):
    store = FakeStore()
    service = FakeService()
    settings = SimpleNamespace(
        discord_token="credential-must-not-be-logged",
        scoring_version="v1.5-runner-failure",
        missed_runner_multiple=3,
        major_missed_runner_multiple=10,
        min_sample_for_edge_metrics=30,
    )
    trees = []
    clients = []
    original_tree = bot_runtime.app_commands.CommandTree

    def tree_factory(client):
        tree = original_tree(client)
        trees.append(tree)
        clients.append(client)
        return tree

    async def fake_start(client, _token):
        if start_hook:
            await start_hook(client, trees[0])

    with (
        patch("discord.Client.start", new=fake_start),
        patch.object(bot_runtime.app_commands, "CommandTree", side_effect=tree_factory),
    ):
        await bot_runtime.run_discord_bot(service, store, settings)
    return trees[0], clients[0], store


@pytest.mark.asyncio
async def test_menu_command_sends_actual_ephemeral_view_and_all_commands_remain_registered():
    tree, _client, _store = await capture_runtime()
    interaction = FakeInteraction()
    await tree.get_command("menu").callback(interaction)
    sent = primary_payload(interaction)
    assert isinstance(sent["view"], MenuView)
    assert sent["view"].timeout == 900
    assert sent["embed"].title == PAGE_TITLES["home"]
    assert interaction.followup.messages == []
    assert len(tree.get_commands()) == 24


@pytest.mark.asyncio
async def test_registered_menu_e2e_sequence_has_no_duplicate_responses():
    tree, _client, _store = await capture_runtime()
    opened = FakeInteraction()
    await tree.get_command("menu").callback(opened)
    view = primary_payload(opened)["view"]

    title = PAGE_TITLES["home"]
    for page in ("overview", "radar", "performance", "system", "home"):
        interaction = FakeInteraction(title)
        await view.navigate(interaction, page, f"gambit:menu:{page}")
        assert interaction.response.deferred_at is not None
        assert len(interaction.edits) == 1
        assert interaction.followup.messages == []
        title = interaction.edits[0]["embed"].title
    assert title == PAGE_TITLES["home"]


@pytest.mark.asyncio
async def test_registered_scan_e2e_refresh_then_watch():
    tree, _client, store = await capture_runtime()
    opened = FakeInteraction()
    await tree.get_command("scan").callback(opened, "So111", "solana")
    scan_view = opened.edits[0]["view"]
    assert isinstance(scan_view, bot_runtime.ScanView)

    refresh = FakeInteraction()
    await item(scan_view, "gambit:scan:refresh").callback(refresh)
    assert len(refresh.edits) == 1

    watch = FakeInteraction()
    await item(scan_view, "gambit:scan:watch").callback(watch)
    assert watch.followup.messages[0]["content"] == "Added to your watchlist."
    assert "add_watch" in store.calls


@pytest.mark.asyncio
async def test_scan_persistent_router_recovers_target_after_simulated_restart():
    tree, client, store = await capture_runtime()
    opened = FakeInteraction()
    await tree.get_command("scan").callback(opened, "So111", "solana")
    scan_embed = opened.edits[0]["embed"]
    router = next(
        view
        for view in client.persistent_views
        if isinstance(view, bot_runtime.ScanView)
    )

    refresh = FakeInteraction()
    refresh.message.embeds = [scan_embed]
    await item(router, "gambit:scan:refresh").callback(refresh)
    assert refresh.edits[0]["embed"].title.startswith("SCAN")

    watch = FakeInteraction()
    watch.message.embeds = [scan_embed]
    await item(router, "gambit:scan:watch").callback(watch)
    assert watch.followup.messages[0]["content"] == "Added to your watchlist."
    assert "add_watch" in store.calls


@pytest.mark.asyncio
async def test_persistent_token_copy_and_watch_use_exact_full_contract_after_restart():
    _tree, client, store = await capture_runtime()
    address = "So11111111111111111111111111111111111111111"
    payload = format_discord_event(
        "EARLY_RADAR",
        {
            "name": "Example",
            "symbol": "EXM",
            "chain": "solana",
            "token_address": address,
            "reasons": ["TEST"],
        },
    )
    router = next(
        view for view in client.persistent_views if isinstance(view, bot_runtime.TokenActionView)
    )

    copied = FakeInteraction()
    copied.message.embeds = [discord.Embed.from_dict(payload["embeds"][0])]
    await item(router, "gambit:token:copy_ca").callback(copied)
    assert address in copied.response.messages[0]["content"]

    watched = FakeInteraction()
    watched.message.embeds = [discord.Embed.from_dict(payload["embeds"][0])]
    await item(router, "gambit:token:watch").callback(watched)
    assert watched.followup.messages[0]["content"] == "Added to your watchlist."
    assert "add_watch" in store.calls


@pytest.mark.asyncio
async def test_all_24_registered_commands_complete_one_primary_response():
    tree, _client, _store = await capture_runtime()
    calls = {
        "status": (),
        "menu": (),
        "help": (),
        "performance": ("all",),
        "scan": ("So111", "solana"),
        "compare": ("So111", "So222", "solana"),
        "watch": ("So111", "solana"),
        "unwatch": ("So111", "solana"),
        "watchlist": (),
        "candidates": (),
        "rejections": (),
        "missed": ("24h",),
        "radar": (),
        "runners": (),
        "failed": (),
        "token": ("So111",),
        "smartmoney": ("So111",),
        "wallet": ("Wallet111",),
        "clusters": (),
        "creator": ("Creator111",),
        "narrative": ("AI",),
        "setup": (),
        "server-settings": (),
        "test-alert": (),
    }
    assert set(calls) == bot_runtime.EXPECTED_COMMAND_NAMES
    for name, arguments in calls.items():
        interaction = FakeInteraction(admin=True)
        await tree.get_command(name).callback(interaction, *arguments)
        primary_payload(interaction)
        assert interaction.followup.messages == [], name


@pytest.mark.asyncio
async def test_persistent_view_is_registered_again_after_restart():
    _tree1, client1, _store1 = await capture_runtime()
    _tree2, client2, _store2 = await capture_runtime()
    assert sum(isinstance(view, MenuView) for view in client1.persistent_views) == 1
    assert sum(isinstance(view, MenuView) for view in client2.persistent_views) == 1
    assert sum(isinstance(view, bot_runtime.ScanView) for view in client1.persistent_views) == 1
    assert sum(isinstance(view, bot_runtime.ScanView) for view in client2.persistent_views) == 1


@pytest.mark.asyncio
async def test_clean_database_restart_keeps_command_center_operational(tmp_path):
    database_path = tmp_path / "restart.db"
    settings = Settings(database_path=database_path)

    first = Store(database_path)
    first.migrate()
    first_view = MenuView(CommandCenterData(FakeService(), first, settings), timeout=None)
    assert first_view.is_persistent()
    first.close()

    restarted = Store(database_path)
    restarted.migrate()
    second_view = MenuView(CommandCenterData(FakeService(), restarted, settings), timeout=None)
    interaction = FakeInteraction()
    await second_view.navigate(interaction, "system", "gambit:menu:system")
    assert interaction.edits[0]["embed"].title == PAGE_TITLES["system"]
    assert restarted.conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert restarted.conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 8
    restarted.close()


@pytest.mark.asyncio
async def test_unauthorized_setup_is_rejected_before_store_mutation():
    tree, _client, store = await capture_runtime()
    interaction = FakeInteraction(admin=False)
    await tree.get_command("setup").callback(interaction)
    assert store.setup_called is False
    assert primary_payload(interaction)["content"] == "Manage Server permission is required."
    assert interaction.followup.messages == []


@pytest.mark.asyncio
async def test_global_tree_error_uses_safe_followup_after_defer(caplog):
    tree, _client, _store = await capture_runtime()
    interaction = FakeInteraction()
    await interaction.response.defer(ephemeral=True)
    error = bot_runtime.app_commands.AppCommandError("DISCORD_TOKEN=not-visible")
    with caplog.at_level(logging.INFO, logger="memecoin_bot.discord"):
        await tree.on_error(interaction, error)
    assert interaction.edits[0]["content"] == SAFE_ERROR
    assert "embed" not in interaction.edits[0]
    assert interaction.followup.messages == []
    assert "not-visible" not in caplog.text


@pytest.mark.asyncio
async def test_sync_is_idempotent_and_failure_is_visible_without_credentials(caplog):
    calls = 0

    async def start_hook(client, tree):
        nonlocal calls

        async def fail_sync():
            nonlocal calls
            calls += 1
            raise RuntimeError("credential-must-not-be-logged")

        tree.sync = fail_sync
        await client.on_ready()

    with caplog.at_level(logging.INFO, logger="memecoin_bot.discord"):
        await capture_runtime(start_hook=start_hook)
    assert calls == 1
    assert "command_sync_failure" in caplog.text
    assert "credential-must-not-be-logged" not in caplog.text


@pytest.mark.asyncio
async def test_successful_sync_runs_once_across_reconnects(caplog):
    calls = 0

    async def start_hook(client, tree):
        nonlocal calls

        async def sync():
            nonlocal calls
            calls += 1
            return tree.get_commands()

        tree.sync = sync
        await client.on_ready()
        await client.on_ready()

    with caplog.at_level(logging.INFO, logger="memecoin_bot.discord"):
        await capture_runtime(start_hook=start_hook)
    assert calls == 1
    assert "command_sync_success" in caplog.text
    assert "command_sync_skipped" in caplog.text

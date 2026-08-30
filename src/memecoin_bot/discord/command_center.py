from __future__ import annotations

import asyncio
import logging
import time
import traceback
from collections.abc import Awaitable, Callable
from typing import Any

import discord

from memecoin_bot.discord.cards import (
    card,
    performance_card,
    settings_card,
    status_card,
    watchlist_card,
)
from memecoin_bot.discord.responses import (
    SAFE_INTERNAL_ERROR,
    edit_deferred_original_exact,
    log_discord_http_failure,
    respond_component_error,
)
from memecoin_bot.discord.validation import component_count, validate_message
from memecoin_bot.observability.logging import event
from memecoin_bot.v15_engine import operator_model_status

SAFE_ERROR = SAFE_INTERNAL_ERROR
MENU_PAGES = (
    "overview",
    "scanner",
    "radar",
    "intelligence",
    "watchlist",
    "performance",
    "system",
    "settings",
)
COMPONENT_ACK_TIMEOUT_SECONDS = 2.5
COMPONENT_TIMEOUT_SECONDS = 30.0

PAGE_TITLES = {
    "home": "GAMBIT JR • COMMAND CENTER",
    "overview": "COMMAND CENTER • OVERVIEW",
    "scanner": "COMMAND CENTER • SCANNER",
    "radar": "COMMAND CENTER • RADAR",
    "intelligence": "COMMAND CENTER • INTELLIGENCE",
    "watchlist": "COMMAND CENTER • WATCHLIST",
    "performance": "COMMAND CENTER • PERFORMANCE",
    "system": "COMMAND CENTER • SYSTEM",
    "settings": "COMMAND CENTER • SETTINGS",
}


def _field(name: str, value: Any) -> dict[str, Any]:
    return {
        "name": name,
        "value": "UNKNOWN" if value is None or value == "" else str(value)[:1024],
        "inline": False,
    }


def _known(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    return "UNKNOWN" if value is None else str(value)


def _safe_failure_log(
    logger: logging.Logger, message: str, error: BaseException, **fields: Any
) -> None:
    frames = traceback.extract_tb(error.__traceback__)
    event(
        logger,
        logging.ERROR,
        message,
        error_type=type(error).__name__,
        traceback=[f"{frame.name}:{frame.lineno}" for frame in frames[-8:]],
        **fields,
    )


class CommandCenterData:
    """Read-only adapter from authoritative Store/Service methods to mobile cards."""

    def __init__(self, service: object, store: object, settings: object):
        self.service = service
        self.store = store
        self.settings = settings

    async def render(self, page: str, interaction: discord.Interaction) -> dict[str, Any]:
        renderer = getattr(self, f"_{page}", None)
        if renderer is None:
            page = "home"
            renderer = self._home
        return await renderer(interaction)

    async def _home(self, _interaction: discord.Interaction) -> dict[str, Any]:
        return card(
            PAGE_TITLES["home"],
            "Private, read-only intelligence. Choose a section below.",
            fields=[
                _field("QUICK START", "`/scan` a contract • `/compare` two setups"),
                _field("NAVIGATION", "Use the section picker. Home, Back and Refresh stay here."),
                _field("SAFETY", "No trades are executed. Missing evidence is shown as UNKNOWN."),
            ],
        )

    async def _overview(self, _interaction: discord.Interaction) -> dict[str, Any]:
        stats, radar, performance = await asyncio.gather(
            asyncio.to_thread(self.store.status_stats, self.service.started_at),
            asyncio.to_thread(self.store.radar_board, 25),
            asyncio.to_thread(
                self.store.performance,
                self.settings.scoring_version,
                None,
                self.settings.major_missed_runner_multiple,
            ),
        )
        runners = sorted(
            (row for row in radar if row.get("max_multiple") is not None),
            key=lambda row: float(row["max_multiple"]),
            reverse=True,
        )[:3]
        runner_lines = (
            "\n".join(
                f"**{row.get('name') or row.get('symbol') or 'UNKNOWN'}** • "
                f"{float(row['max_multiple']):.2f}x"
                for row in runners
            )
            or "No measured runners available."
        )
        return card(
            PAGE_TITLES["overview"],
            "Live operational and measured intelligence snapshot.",
            fields=[
                _field(
                    "ACTIVE NOW",
                    f"Radar: **{_known(stats, 'early_radar')}**\n"
                    f"Signals: **{_known(stats, 'active_signals')}**\n"
                    f"Watching: **{_known(stats, 'candidates_watching')}**",
                ),
                _field("RECENT RUNNERS", runner_lines),
                _field(
                    "PERFORMANCE",
                    f"Signals: **{_known(performance, 'total_signals')}**\n"
                    f"2x rate: **{_known(performance, '2x_rate')}**\n"
                    f"Median peak: **{_known(performance, 'median_max_multiple')}**",
                ),
            ],
        )

    async def _scanner(self, _interaction: discord.Interaction) -> dict[str, Any]:
        return card(
            PAGE_TITLES["scanner"],
            "Run focused read-only intelligence from slash commands.",
            fields=[
                _field("SCAN TOKEN", "`/scan address:<CA> chain:<solana|bsc>`"),
                _field("COMPARE", "`/compare address_a:<CA> address_b:<CA> chain:<chain>`"),
                _field("DEEP INTELLIGENCE", "`/token` • `/smartmoney` • `/wallet`"),
            ],
        )

    async def _radar(self, _interaction: discord.Interaction) -> dict[str, Any]:
        radar, candidates = await asyncio.gather(
            asyncio.to_thread(self.store.radar_board, 8),
            asyncio.to_thread(self.store.candidates_report, 5),
        )
        radar_lines = (
            "\n".join(
                f"**{row.get('name') or row.get('symbol') or 'UNKNOWN'}** • "
                f"{row.get('state') or 'UNKNOWN'} • {row.get('chain') or 'UNKNOWN'}"
                for row in radar[:5]
            )
            or "No active Radar evidence."
        )
        candidate_lines = (
            "\n".join(
                f"**{row.get('name') or row.get('symbol') or 'UNKNOWN'}** • "
                f"{row.get('state') or 'UNKNOWN'}"
                for row in candidates[:3]
            )
            or "No active candidates."
        )
        return card(
            PAGE_TITLES["radar"],
            "Current Store-backed Radar and candidate state.",
            fields=[
                _field("ACTIVE RADAR", radar_lines),
                _field("CANDIDATES", candidate_lines),
                _field("EXPLORE", "`/runners` • `/failed` • `/rejections` • `/missed`"),
            ],
        )

    async def _intelligence(self, _interaction: discord.Interaction) -> dict[str, Any]:
        clusters, narratives = await asyncio.gather(
            asyncio.to_thread(self.store.cluster_report, 5),
            asyncio.to_thread(self.store.narrative_report, None, 5),
        )
        cluster_lines = (
            "\n".join(
                f"**{str(row.get('chain') or 'UNKNOWN').upper()}** • "
                f"{row.get('risk_state') or 'UNKNOWN'} • "
                f"{row.get('member_count') if row.get('member_count') is not None else 'UNKNOWN'} wallets"
                for row in clusters[:3]
            )
            or "No wallet clusters observed."
        )
        narrative_lines = (
            "\n".join(
                f"**{row.get('label') or 'UNKNOWN'}** • {row.get('freshness') or 'UNKNOWN'}"
                for row in narratives[:3]
            )
            or "No narrative evidence available."
        )
        return card(
            PAGE_TITLES["intelligence"],
            "Graph, creator and narrative context from stored evidence.",
            fields=[
                _field("WALLET CLUSTERS", cluster_lines),
                _field("NARRATIVES", narrative_lines),
                _field("INSPECT", "`/wallet` • `/clusters` • `/creator` • `/narrative`"),
            ],
        )

    async def _watchlist(self, interaction: discord.Interaction) -> dict[str, Any]:
        rows = await asyncio.to_thread(
            self.store.user_watchlist, interaction.guild_id, interaction.user.id
        )
        payload = watchlist_card(rows)
        payload["embed"]["title"] = PAGE_TITLES["watchlist"]
        payload["embed"]["fields"] = [
            _field("MANAGE", "Use `/watch` and `/unwatch` with a contract address.")
        ]
        return payload

    async def _performance(self, _interaction: discord.Interaction) -> dict[str, Any]:
        report = await asyncio.to_thread(
            self.store.performance,
            self.settings.scoring_version,
            None,
            self.settings.major_missed_runner_multiple,
        )
        report["right_tail"] = await asyncio.to_thread(
            self.store.right_tail_performance, self.settings.min_sample_for_edge_metrics
        )
        report["v15"] = await asyncio.to_thread(
            self.store.v15_performance, self.settings.min_sample_for_edge_metrics
        )
        sample = report.get("total_signals")
        report["small_sample"] = (
            sample is not None and int(sample) < self.settings.min_sample_for_edge_metrics
        )
        payload = performance_card(report)
        payload["embed"]["title"] = PAGE_TITLES["performance"]
        payload["embed"]["description"] += "\nUse `/performance period:7d|30d|all` for a window."
        return payload

    async def _system(self, _interaction: discord.Interaction) -> dict[str, Any]:
        stats = await asyncio.to_thread(self.store.status_stats, self.service.started_at)
        stats["v14"] = await asyncio.to_thread(self.store.v14_health)
        queue = getattr(self.service, "launch_queue", None)
        stats["event_queue"] = queue.stats() if queue else None
        historical = getattr(self.service, "historical_context", None)
        stats["historical_context"] = historical.status() if historical else {"enabled": False}
        stats["model"] = operator_model_status(self.settings)
        runtime_health = getattr(self.service, "runtime_health", None)
        stats["runtime"] = runtime_health() if callable(runtime_health) else {}
        payload = status_card(stats)
        payload["embed"]["title"] = PAGE_TITLES["system"]
        return payload

    async def _settings(self, interaction: discord.Interaction) -> dict[str, Any]:
        current = await asyncio.to_thread(self.store.guild_settings, interaction.guild_id)
        payload = settings_card(current)
        payload["embed"]["title"] = PAGE_TITLES["settings"]
        payload["embed"]["fields"].append(
            _field("ADMIN SETUP", "Manage Server permission is required for `/setup`.")
        )
        return payload


class MenuSelect(discord.ui.Select):
    def __init__(self, view: MenuView):
        self.menu_view = view
        options = [
            discord.SelectOption(label=page.title(), value=page, description=description)
            for page, description in (
                ("overview", "Live status and measured snapshot"),
                ("scanner", "Scan and compare guidance"),
                ("radar", "Candidates, calls and outcomes"),
                ("intelligence", "Wallet, creator and narrative context"),
                ("watchlist", "Your private watched contracts"),
                ("performance", "Measured result windows"),
                ("system", "Providers, pipeline and delivery"),
                ("settings", "Server alert configuration"),
            )
        ]
        super().__init__(
            placeholder="Choose a command-center section",
            custom_id="gambit:menu:navigate",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.menu_view.navigate(interaction, self.values[0], self.custom_id)


class MenuView(discord.ui.View):
    """Stateless persistent navigation; current page is read from the message embed."""

    def __init__(
        self,
        data: CommandCenterData,
        logger: logging.Logger | None = None,
        *,
        timeout: float | None = None,
    ):
        super().__init__(timeout=timeout)
        self.data = data
        self.log = logger or logging.getLogger("memecoin_bot.discord")
        self.add_item(MenuSelect(self))

    def current_page(self, interaction: discord.Interaction) -> str:
        message = getattr(interaction, "message", None)
        embeds = getattr(message, "embeds", []) if message else []
        title = getattr(embeds[0], "title", None) if embeds else None
        return next((page for page, expected in PAGE_TITLES.items() if title == expected), "home")

    async def navigate(self, interaction: discord.Interaction, page: str, custom_id: str) -> None:
        started = time.monotonic()
        event(
            self.log,
            logging.INFO,
            "component_received",
            custom_id=custom_id,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            user_id=getattr(interaction.user, "id", None),
        )
        try:
            if not interaction.response.is_done():
                await asyncio.wait_for(
                    interaction.response.defer(), timeout=COMPONENT_ACK_TIMEOUT_SECONDS
                )
            payload = await asyncio.wait_for(
                self.data.render(page, interaction), timeout=COMPONENT_TIMEOUT_SECONDS
            )
            embed = validate_message(card_payload=payload, view=self)
            await edit_deferred_original_exact(interaction, embed=embed, view=self)
        except discord.HTTPException as error:
            log_discord_http_failure(
                self.log,
                error,
                command_name="menu",
                interaction=interaction,
                response_state="deferred_component_update",
                defer_occurred=True,
                ephemeral=True,
                payload_kind="embed_view",
                has_content=False,
                embed_count=1,
                components=component_count(self),
                started=started,
            )
            await respond_component_error(interaction, SAFE_ERROR)
            return
        except Exception as error:  # noqa: BLE001 - component boundary must contain callbacks
            _safe_failure_log(
                self.log,
                "component_failed",
                error,
                custom_id=custom_id,
                duration_ms=round((time.monotonic() - started) * 1000, 1),
                result="failure",
            )
            await respond_component_error(interaction, SAFE_ERROR)
            return
        event(
            self.log,
            logging.INFO,
            "component_completed",
            custom_id=custom_id,
            duration_ms=round((time.monotonic() - started) * 1000, 1),
            result="success",
        )

    @discord.ui.button(
        label="Home",
        emoji="🏠",
        style=discord.ButtonStyle.secondary,
        custom_id="gambit:menu:home",
        row=1,
    )
    async def home(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.navigate(interaction, "home", "gambit:menu:home")

    @discord.ui.button(
        label="Back",
        style=discord.ButtonStyle.secondary,
        custom_id="gambit:menu:back",
        row=1,
    )
    async def back(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.navigate(interaction, "home", "gambit:menu:back")

    @discord.ui.button(
        label="Refresh",
        style=discord.ButtonStyle.primary,
        custom_id="gambit:menu:refresh",
        row=1,
    )
    async def refresh(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.navigate(interaction, self.current_page(interaction), "gambit:menu:refresh")

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item[Any]
    ) -> None:
        _safe_failure_log(
            self.log,
            "component_callback_failed",
            error,
            custom_id=getattr(item, "custom_id", "UNKNOWN"),
            result="failure",
        )
        await respond_component_error(interaction, SAFE_ERROR)


async def run_component_callback(
    interaction: discord.Interaction,
    custom_id: str,
    logger: logging.Logger,
    callback: Callable[[], Awaitable[None]],
    *,
    ephemeral_defer: bool = False,
) -> None:
    started = time.monotonic()
    event(
        logger,
        logging.INFO,
        "component_received",
        custom_id=custom_id,
        guild_id=interaction.guild_id,
        channel_id=interaction.channel_id,
        user_id=getattr(interaction.user, "id", None),
    )
    try:
        if not interaction.response.is_done():
            await asyncio.wait_for(
                interaction.response.defer(ephemeral=ephemeral_defer),
                timeout=COMPONENT_ACK_TIMEOUT_SECONDS,
            )
        await asyncio.wait_for(callback(), timeout=COMPONENT_TIMEOUT_SECONDS)
    except discord.HTTPException as error:
        log_discord_http_failure(
            logger,
            error,
            command_name="scan",
            interaction=interaction,
            response_state="deferred_component_update",
            defer_occurred=True,
            ephemeral=ephemeral_defer,
            payload_kind="component_action",
            has_content=False,
            embed_count=0,
            components=0,
            started=started,
        )
        await respond_component_error(interaction, SAFE_ERROR)
        return
    except Exception as error:  # noqa: BLE001 - component boundary must contain callbacks
        _safe_failure_log(
            logger,
            "component_failed",
            error,
            custom_id=custom_id,
            duration_ms=round((time.monotonic() - started) * 1000, 1),
            result="failure",
        )
        await respond_component_error(interaction, SAFE_ERROR)
        return
    event(
        logger,
        logging.INFO,
        "component_completed",
        custom_id=custom_id,
        duration_ms=round((time.monotonic() - started) * 1000, 1),
        result="success",
    )

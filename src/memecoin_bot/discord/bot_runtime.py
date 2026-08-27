from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

try:
    import discord
    from discord import app_commands
except ImportError:  # pragma: no cover - deployment dependency guard
    discord = None
    app_commands = None

from memecoin_bot.discord.cards import (
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
    test_alert_card,
    token_card,
    wallet_card,
    watchlist_card,
)
from memecoin_bot.discord.command_center import (
    CommandCenterData,
    MenuView,
    _safe_failure_log,
    run_component_callback,
)
from memecoin_bot.discord.responses import (
    SAFE_INTERNAL_ERROR,
    InteractionResponder,
    ResponseVisibility,
    respond_command_error,
    respond_component_error,
)
from memecoin_bot.discord.validation import validate_message
from memecoin_bot.observability.logging import event

CommandCallback = TypeVar("CommandCallback", bound=Callable[..., Awaitable[None]])
EXPECTED_COMMAND_NAMES = frozenset(
    {
        "candidates",
        "clusters",
        "compare",
        "creator",
        "failed",
        "help",
        "menu",
        "missed",
        "narrative",
        "performance",
        "radar",
        "rejections",
        "runners",
        "scan",
        "server-settings",
        "setup",
        "smartmoney",
        "status",
        "test-alert",
        "token",
        "unwatch",
        "wallet",
        "watch",
        "watchlist",
    }
)
PRIVATE_COMMANDS = frozenset(
    {
        "compare",
        "creator",
        "help",
        "menu",
        "scan",
        "narrative",
        "server-settings",
        "setup",
        "smartmoney",
        "token",
        "unwatch",
        "wallet",
        "watch",
        "watchlist",
    }
)

if discord is not None:

    class ScanView(discord.ui.View):
        def __init__(
            self,
            service: object,
            store: object,
            address: str | None,
            chain: str | None,
            logger: logging.Logger | None = None,
            *,
            timeout: float | None = 900,
        ):
            super().__init__(timeout=timeout)
            self.service = service
            self.store = store
            self.address = address
            self.chain = chain
            self.log = logger or logging.getLogger("memecoin_bot.discord")
            self.message: discord.InteractionMessage | None = None

        async def on_timeout(self) -> None:
            for item in self.children:
                if hasattr(item, "disabled"):
                    item.disabled = True
            if self.message is not None:
                try:
                    await self.message.edit(view=self)
                except discord.HTTPException:
                    pass

        async def on_error(
            self,
            interaction: discord.Interaction,
            error: Exception,
            item: discord.ui.Item[Any],
        ) -> None:
            _safe_failure_log(
                self.log,
                "component_callback_failed",
                error,
                custom_id=getattr(item, "custom_id", "UNKNOWN"),
                result="failure",
            )
            await respond_component_error(interaction, SAFE_INTERNAL_ERROR)

        def target(self, interaction: discord.Interaction) -> tuple[str, str]:
            if self.address and self.chain:
                return self.address, self.chain
            message = getattr(interaction, "message", None)
            embeds = getattr(message, "embeds", []) if message else []
            if not embeds:
                raise ValueError("Scan session metadata is unavailable; run /scan again.")
            embed = embeds[0]
            description = str(getattr(embed, "description", "") or "")
            address = description.split("`", 2)[1] if "`" in description else ""
            chain_field = next(
                (field for field in getattr(embed, "fields", []) if field.name == "Chain"), None
            )
            chain = str(chain_field.value).lower() if chain_field else ""
            if not address or chain not in {"solana", "bsc"}:
                raise ValueError("Scan session metadata is invalid; run /scan again.")
            return address, chain

        @discord.ui.button(
            label="Refresh", style=discord.ButtonStyle.primary, custom_id="gambit:scan:refresh"
        )
        async def refresh(
            self, interaction: discord.Interaction, _button: discord.ui.Button
        ) -> None:
            async def refresh_scan() -> None:
                address, chain = self.target(interaction)
                result = await self.service.manual_scan(
                    address, chain, interaction.guild_id, interaction.user.id
                )
                embed = validate_message(card_payload=scan_card(result), view=self)
                await interaction.edit_original_response(
                    embed=embed, view=self
                )

            await run_component_callback(
                interaction, "gambit:scan:refresh", self.log, refresh_scan
            )

        @discord.ui.button(
            label="Watch", style=discord.ButtonStyle.secondary, custom_id="gambit:scan:watch"
        )
        async def watch(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
            async def add_watch() -> None:
                address, chain = self.target(interaction)
                created = await asyncio.to_thread(
                    self.store.add_watch,
                    interaction.guild_id,
                    interaction.user.id,
                    chain,
                    address,
                )
                await interaction.followup.send(
                    "Added to your watchlist." if created else "Already on your watchlist.",
                    ephemeral=True,
                )

            await run_component_callback(
                interaction,
                "gambit:scan:watch",
                self.log,
                add_watch,
                ephemeral_defer=True,
            )

else:  # pragma: no cover - deployment dependency guard
    ScanView = object


async def run_discord_bot(service: object, store: object, settings: object) -> None:
    if discord is None or app_commands is None:
        raise RuntimeError("discord.py is required for slash commands")

    intents = discord.Intents.none()
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)
    log = logging.getLogger("memecoin_bot.discord")
    menu_data = CommandCenterData(service, store, settings)
    client.add_view(MenuView(menu_data, log, timeout=None))
    client.add_view(ScanView(service, store, None, None, log, timeout=None))
    response_sessions: dict[int, InteractionResponder] = {}
    active_command_names: dict[int, str] = {}

    def track_command(callback: CommandCallback) -> CommandCallback:
        @functools.wraps(callback)
        async def tracked(interaction: discord.Interaction, *args: Any, **kwargs: Any) -> None:
            started = time.monotonic()
            name = callback.__name__.removesuffix("_command").replace("_", "-")
            active_command_names[id(interaction)] = name
            event(
                log,
                logging.INFO,
                "command_received",
                command_name=name,
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                user_id=getattr(interaction.user, "id", None),
            )
            try:
                await callback(interaction, *args, **kwargs)
            except Exception as error:
                _safe_failure_log(
                    log,
                    "command_completed",
                    error,
                    command_name=name,
                    duration_ms=round((time.monotonic() - started) * 1000, 1),
                    result="failure",
                )
                raise
            finally:
                response_sessions.pop(id(interaction), None)
                active_command_names.pop(id(interaction), None)
            event(
                log,
                logging.INFO,
                "command_completed",
                command_name=name,
                duration_ms=round((time.monotonic() - started) * 1000, 1),
                result="success",
            )

        return tracked  # type: ignore[return-value]

    def command_allowed(interaction: discord.Interaction) -> bool:
        """Commands are guild-wide; automatic-alert destination is a separate policy."""
        return interaction.guild_id is not None and interaction.channel_id is not None

    async def send_card(
        interaction: discord.Interaction,
        payload: dict,
        ephemeral: bool = False,
        view: discord.ui.View | None = None,
    ) -> discord.InteractionMessage | None:
        session = response_sessions.get(id(interaction))
        if session is None:
            raise RuntimeError("Discord primary response has no active command session")
        if ephemeral != session.visibility.ephemeral:
            raise RuntimeError(
                f"response visibility mismatch for {session.command_name}: "
                f"deferred={session.visibility.value}, requested={'private' if ephemeral else 'public'}"
            )
        message = await session.primary_card(payload, view)
        if isinstance(view, ScanView):
            view.message = message
        return message

    async def send_text(
        interaction: discord.Interaction, message: str, ephemeral: bool = True
    ) -> None:
        session = response_sessions.get(id(interaction))
        if session is None:
            raise RuntimeError("Discord primary response has no active command session")
        if ephemeral != session.visibility.ephemeral:
            raise RuntimeError(
                f"response visibility mismatch for {session.command_name}: "
                f"deferred={session.visibility.value}, requested={'private' if ephemeral else 'public'}"
            )
        await session.primary_text(message)

    async def require_guild(interaction: discord.Interaction) -> bool:
        if command_allowed(interaction):
            command = active_command_names.get(id(interaction)) or getattr(
                getattr(interaction, "command", None), "name", "unknown"
            )
            visibility = (
                ResponseVisibility.PRIVATE
                if command in PRIVATE_COMMANDS
                else ResponseVisibility.PUBLIC
            )
            session = InteractionResponder(interaction, command, visibility, log)
            response_sessions[id(interaction)] = session
            await session.defer()
            return True
        await interaction.response.send_message(
            "This command is available in Discord server text channels.", ephemeral=True
        )
        return False

    @tree.command(
        name="status",
        description="Show truthful live scanner, pipeline, provider, and lifetime state",
    )
    @track_command
    async def status_command(interaction: discord.Interaction) -> None:
        if await require_guild(interaction):
            stats = store.status_stats(service.started_at)
            stats["v14"] = store.v14_health()
            queue = getattr(service, "launch_queue", None)
            stats["event_queue"] = queue.stats() if queue else {}
            await send_card(interaction, status_card(stats))

    @tree.command(name="menu", description="Open the complete Gambit Jr command center")
    @track_command
    async def menu_command(interaction: discord.Interaction) -> None:
        if await require_guild(interaction):
            payload = await menu_data.render("home", interaction)
            await send_card(interaction, payload, True, MenuView(menu_data, log, timeout=900))

    @tree.command(name="help", description="Explain Gambit Jr commands and intelligence flow")
    @track_command
    async def help_command(interaction: discord.Interaction) -> None:
        if await require_guild(interaction):
            await send_card(interaction, menu_card(), True)

    @tree.command(name="performance", description="Show measured historical shadow performance")
    @app_commands.describe(period="7d, 30d, or all")
    @track_command
    async def performance_command(interaction: discord.Interaction, period: str = "all") -> None:
        if not await require_guild(interaction):
            return
        days = {"7d": 7, "30d": 30}.get(period.lower())
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat() if days else None
        report = store.performance(
            settings.scoring_version, since, settings.major_missed_runner_multiple
        )
        report["right_tail"] = store.right_tail_performance(settings.min_sample_for_edge_metrics)
        report["v15"] = store.v15_performance(settings.min_sample_for_edge_metrics)
        report["small_sample"] = (
            int(report.get("total_signals") or 0) < settings.min_sample_for_edge_metrics
        )
        await send_card(interaction, performance_card(report))

    @tree.command(name="scan", description="Run a parallel read-only scan for any supported CA")
    @app_commands.describe(address="Token contract address", chain="solana or bsc")
    @track_command
    async def scan_command(
        interaction: discord.Interaction, address: str, chain: str = "solana"
    ) -> None:
        if not await require_guild(interaction):
            return
        chain = chain.lower()
        if chain not in {"solana", "bsc"}:
            await send_text(interaction, "Chain must be solana or bsc.")
            return
        result = await service.manual_scan(
            address.strip(), chain, interaction.guild_id, interaction.user.id
        )
        await send_card(
            interaction,
            scan_card(result),
            True,
            ScanView(service, store, address.strip(), chain, log, timeout=900),
        )

    @tree.command(name="compare", description="Compare two token setups side by side")
    @track_command
    async def compare_command(
        interaction: discord.Interaction,
        address_a: str,
        address_b: str,
        chain: str = "solana",
    ) -> None:
        if not await require_guild(interaction):
            return
        left, right = await asyncio.gather(
            service.manual_scan(
                address_a.strip(), chain, interaction.guild_id, interaction.user.id
            ),
            service.manual_scan(
                address_b.strip(), chain, interaction.guild_id, interaction.user.id
            ),
        )
        await send_card(interaction, compare_card(left, right), True)

    @tree.command(name="watch", description="Add a token to your private server watchlist")
    @track_command
    async def watch_command(
        interaction: discord.Interaction, address: str, chain: str = "solana"
    ) -> None:
        if not await require_guild(interaction):
            return
        created = store.add_watch(
            interaction.guild_id, interaction.user.id, chain.lower(), address.strip()
        )
        await send_text(
            interaction,
            "Added to your watchlist." if created else "Already on your watchlist.", ephemeral=True
        )

    @tree.command(name="unwatch", description="Remove a token from your watchlist")
    @track_command
    async def unwatch_command(
        interaction: discord.Interaction, address: str, chain: str = "solana"
    ) -> None:
        if not await require_guild(interaction):
            return
        removed = store.remove_watch(
            interaction.guild_id, interaction.user.id, chain.lower(), address.strip()
        )
        await send_text(
            interaction,
            "Removed from your watchlist." if removed else "Token was not on your watchlist.",
            ephemeral=True,
        )

    @tree.command(name="watchlist", description="Show your watched tokens")
    @track_command
    async def watchlist_command(interaction: discord.Interaction) -> None:
        if await require_guild(interaction):
            await send_card(
                interaction,
                watchlist_card(store.user_watchlist(interaction.guild_id, interaction.user.id)),
                True,
            )

    @tree.command(name="candidates", description="Show strongest active pre-signal candidates")
    @track_command
    async def candidates_command(interaction: discord.Interaction) -> None:
        if not await require_guild(interaction):
            return
        await send_card(
            interaction,
            rows_card(
                "ACTIVE CANDIDATES",
                store.candidates_report(10),
                "No active candidates.",
                lambda r: (
                    f"**{r.get('name') or r.get('symbol')}** • {r.get('chain')} • {r.get('state')} • score {float(r.get('normalized_score') or 0):.1f}"
                ),
            ),
        )

    @tree.command(name="rejections", description="Show hard and temporary rejection reasons")
    @track_command
    async def rejections_command(interaction: discord.Interaction) -> None:
        if not await require_guild(interaction):
            return
        report = store.rejection_report((datetime.now(UTC) - timedelta(hours=24)).isoformat())
        rows = [
            {"kind": kind, "reason": reason, "count": count}
            for kind, values in report.items()
            for reason, count in values
        ]
        await send_card(
            interaction,
            rows_card(
                "REJECTIONS / BLOCKERS • 24H",
                rows,
                "No rejection evidence.",
                lambda r: f"**{r['count']}×** {r['reason']} ({r['kind']})",
                "amber",
            ),
        )

    @tree.command(name="missed", description="Show observed runners without a qualified signal")
    @app_commands.describe(period="24h, 7d, or 30d")
    @track_command
    async def missed_command(interaction: discord.Interaction, period: str = "24h") -> None:
        if not await require_guild(interaction):
            return
        hours = {"24h": 24, "7d": 168, "30d": 720}.get(period.lower(), 24)
        rows = store.missed_report(
            (datetime.now(UTC) - timedelta(hours=hours)).isoformat(),
            settings.missed_runner_multiple,
            8,
        )
        await send_card(
            interaction,
            rows_card(
                f"MISSED RUNNERS • {period.upper()}",
                rows,
                "No missed runners.",
                lambda r: (
                    f"**{r.get('name') or r.get('symbol')}** • {float(r.get('max_multiple_from_discovery') or 0):.2f}x • {r.get('non_signal_reason') or r.get('reason') or 'UNKNOWN'}"
                ),
                "amber",
            ),
        )

    async def token_reply(
        interaction: discord.Interaction, address: str, smart_only: bool = False
    ) -> None:
        if not await require_guild(interaction):
            return
        data = store.token_intelligence(address)
        if not data:
            await send_text(interaction, "Token is not tracked.")
            return
        await send_card(
            interaction, smartmoney_card(data) if smart_only else token_card(data), True
        )

    @tree.command(name="radar", description="Show active Radar calls and ongoing outcomes")
    @track_command
    async def radar_command(interaction: discord.Interaction) -> None:
        if not await require_guild(interaction):
            return
        await send_card(
            interaction,
            rows_card(
                "RADAR • ACTIVE INTELLIGENCE",
                store.radar_board(10),
                "No Radar calls.",
                lambda r: (
                    f"**{r.get('name') or r.get('symbol')}** • {r.get('chain')} • {r.get('state')} • Radar {float(r.get('radar_score') or 0):.1f} • {float(r.get('max_multiple') or 0):.2f}x"
                ),
            ),
        )

    @tree.command(name="runners", description="Show Radar or signal entities at 2x or greater")
    @track_command
    async def runners_command(interaction: discord.Interaction) -> None:
        if not await require_guild(interaction):
            return
        rows = [r for r in store.radar_board(100) if (r.get("max_multiple") or 0) >= 2]
        await send_card(
            interaction,
            rows_card(
                "RUNNERS • 2X+",
                rows,
                "No runners.",
                lambda r: (
                    f"🔥 **{r.get('name') or r.get('symbol')}** • {float(r.get('max_multiple') or 0):.2f}x • {r.get('state')}"
                ),
                "green",
            ),
        )

    @tree.command(name="failed", description="Show failed calls with observed outcomes")
    @track_command
    async def failed_command(interaction: discord.Interaction) -> None:
        if not await require_guild(interaction):
            return
        rows = [r for r in store.radar_board(100) if r.get("signal_status") == "FAILED"]
        await send_card(
            interaction,
            rows_card(
                "FAILED CALLS",
                rows,
                "No failures.",
                lambda r: (
                    f"🔴 **{r.get('name') or r.get('symbol')}** • peak {float(r.get('max_multiple') or 0):.2f}x"
                ),
                "red",
            ),
        )

    @tree.command(name="token", description="Show current token intelligence")
    @track_command
    async def token_command(interaction: discord.Interaction, address: str) -> None:
        await token_reply(interaction, address)

    @tree.command(name="smartmoney", description="Show labelled wallet evidence for a token")
    @track_command
    async def smartmoney_command(interaction: discord.Interaction, address: str) -> None:
        await token_reply(interaction, address, True)

    @tree.command(name="wallet", description="Inspect read-only wallet relationships and clusters")
    @track_command
    async def wallet_command(interaction: discord.Interaction, address: str) -> None:
        if await require_guild(interaction):
            await send_card(interaction, wallet_card(store.wallet_report(address.strip())), True)

    @tree.command(name="clusters", description="Show recently observed connected-wallet clusters")
    @track_command
    async def clusters_command(interaction: discord.Interaction) -> None:
        if await require_guild(interaction):
            await send_card(
                interaction,
                rows_card(
                    "WALLET CLUSTERS",
                    store.cluster_report(10),
                    "No connected-wallet clusters observed.",
                    lambda row: (
                        f"**{row.get('chain', 'UNKNOWN').upper()}** • {row.get('risk_state')} • "
                        f"{row.get('member_count', 0)} wallets"
                    ),
                ),
            )

    @tree.command(name="creator", description="Inspect creator/deployer launch history")
    @track_command
    async def creator_command(interaction: discord.Interaction, address: str) -> None:
        if await require_guild(interaction):
            await send_card(
                interaction,
                creator_card(store.creator_report(address.strip()), address.strip()),
                True,
            )

    @tree.command(
        name="narrative", description="Inspect narrative leaders, challengers, and saturation"
    )
    @track_command
    async def narrative_command(interaction: discord.Interaction, query: str = "") -> None:
        if await require_guild(interaction):
            await send_card(
                interaction,
                narrative_card(store.narrative_report(query or None), query or None),
                True,
            )

    @tree.command(name="setup", description="Admin: designate a channel for automatic alerts")
    @app_commands.default_permissions(manage_guild=True)
    @track_command
    async def setup_command(
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        alert_tier: str = "HOT_PLUS",
        daily_report: bool = False,
        chains: str = "solana,bsc",
    ) -> None:
        if not await require_guild(interaction):
            return
        permissions = getattr(interaction.user, "guild_permissions", None)
        if not permissions or not permissions.manage_guild:
            await send_text(interaction, "Manage Server permission is required.")
            return
        destination = channel or interaction.channel
        try:
            store.set_guild_settings(
                interaction.guild_id,
                destination.id,
                True,
                alert_tier,
                interaction.user.id,
                daily_report,
                [value.strip().lower() for value in chains.split(",") if value.strip()],
            )
        except ValueError as exc:
            await send_text(interaction, str(exc))
            return
        await send_card(
            interaction, settings_card(store.guild_settings(interaction.guild_id)), True
        )

    @tree.command(
        name="server-settings", description="Show this server's alert destination and noise tier"
    )
    @track_command
    async def server_settings_command(interaction: discord.Interaction) -> None:
        if await require_guild(interaction):
            await send_card(
                interaction, settings_card(store.guild_settings(interaction.guild_id)), True
            )

    @tree.command(
        name="test-alert", description="Admin: send a non-live card without creating intelligence"
    )
    @app_commands.default_permissions(manage_guild=True)
    @track_command
    async def test_alert_command(interaction: discord.Interaction) -> None:
        if not await require_guild(interaction):
            return
        permissions = getattr(interaction.user, "guild_permissions", None)
        if not permissions or not permissions.manage_guild:
            await send_text(interaction, "Manage Server permission is required.")
            return
        message = await send_card(interaction, test_alert_card())
        if message is None:
            message = await interaction.original_response()
        store.record_test_alert(
            interaction.guild_id, interaction.channel_id, interaction.user.id, str(message.id)
        )

    @tree.error
    async def on_tree_error(
        interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        underlying = getattr(error, "original", error)
        _safe_failure_log(
            log,
            "command_error",
            underlying,
            command_name=getattr(getattr(interaction, "command", None), "name", "UNKNOWN"),
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            user_id=getattr(interaction.user, "id", None),
            result="failure",
        )
        await respond_command_error(interaction, SAFE_INTERNAL_ERROR)

    sync_lock = asyncio.Lock()
    sync_complete = False
    registered_names = {command.name for command in tree.get_commands()}
    if registered_names != EXPECTED_COMMAND_NAMES:
        missing = sorted(EXPECTED_COMMAND_NAMES - registered_names)
        extra = sorted(registered_names - EXPECTED_COMMAND_NAMES)
        raise RuntimeError(f"Discord command contract mismatch; missing={missing}, extra={extra}")

    @client.event
    async def on_ready() -> None:
        nonlocal sync_complete
        user = client.user
        event(
            log,
            logging.INFO,
            "discord_ready",
            bot_username=str(user) if user else "UNKNOWN",
            bot_user_id=getattr(user, "id", None),
            application_id=client.application_id,
            guild_count=len(client.guilds),
        )
        async with sync_lock:
            if sync_complete:
                event(log, logging.INFO, "command_sync_skipped", reason="already_synced")
                return
            names = sorted(command.name for command in tree.get_commands())
            event(log, logging.INFO, "command_sync_start", command_names=names)
            try:
                synced = await tree.sync()
            except Exception as error:  # noqa: BLE001 - sync is a runtime isolation boundary
                _safe_failure_log(
                    log,
                    "command_sync_failure",
                    error,
                    command_names=names,
                    result="failure",
                )
                return
            sync_complete = True
            event(
                log,
                logging.INFO,
                "command_sync_success",
                synced_command_count=len(synced),
                command_names=sorted(command.name for command in synced),
                result="success",
            )

    service_task = asyncio.create_task(service.run(), name="intelligence-service")
    try:
        event(
            log,
            logging.INFO,
            "discord_connect_start",
            persistent_view_count=2,
            command_count=len(tree.get_commands()),
        )
        await client.start(settings.discord_token)
    finally:
        service.stop()
        await service_task

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

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

if discord is not None:

    class ScanView(discord.ui.View):
        def __init__(self, service: object, store: object, address: str, chain: str):
            super().__init__(timeout=300)
            self.service = service
            self.store = store
            self.address = address
            self.chain = chain

        @discord.ui.button(
            label="Refresh", style=discord.ButtonStyle.primary, custom_id="gambit:scan:refresh"
        )
        async def refresh(
            self, interaction: discord.Interaction, _button: discord.ui.Button
        ) -> None:
            await interaction.response.defer()
            result = await self.service.manual_scan(
                self.address, self.chain, interaction.guild_id, interaction.user.id
            )
            await interaction.edit_original_response(
                embed=discord.Embed.from_dict(scan_card(result)["embed"]), view=self
            )

        @discord.ui.button(
            label="Watch", style=discord.ButtonStyle.secondary, custom_id="gambit:scan:watch"
        )
        async def watch(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
            await interaction.response.defer(ephemeral=True)
            created = await asyncio.to_thread(
                self.store.add_watch,
                interaction.guild_id,
                interaction.user.id,
                self.chain,
                self.address,
            )
            await interaction.followup.send(
                "Added to your watchlist." if created else "Already on your watchlist.",
                ephemeral=True,
            )

else:  # pragma: no cover - deployment dependency guard
    ScanView = object


async def run_discord_bot(service: object, store: object, settings: object) -> None:
    if discord is None or app_commands is None:
        raise RuntimeError("discord.py is required for slash commands")

    intents = discord.Intents.none()
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)

    def command_allowed(interaction: discord.Interaction) -> bool:
        """Commands are guild-wide; automatic-alert destination is a separate policy."""
        return interaction.guild_id is not None and interaction.channel_id is not None

    async def send_card(
        interaction: discord.Interaction,
        payload: dict,
        ephemeral: bool = False,
        view: discord.ui.View | None = None,
    ) -> None:
        embed = discord.Embed.from_dict(payload["embed"])
        if payload.get("links"):
            view = view or discord.ui.View(timeout=None)
            for label, url in payload["links"]:
                view.add_item(discord.ui.Button(label=label, url=url))
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, view=view, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=ephemeral)

    async def send_text(
        interaction: discord.Interaction, message: str, ephemeral: bool = True
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(message, ephemeral=ephemeral)

    async def require_guild(interaction: discord.Interaction) -> bool:
        if command_allowed(interaction):
            # Discord requires an acknowledgement within three seconds.  Every
            # command enters this guard before database or provider work, so
            # defer centrally instead of relying on each handler to remember.
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            return True
        await interaction.response.send_message(
            "This command is available in Discord server text channels.", ephemeral=True
        )
        return False

    @tree.command(
        name="status",
        description="Show truthful live scanner, pipeline, provider, and lifetime state",
    )
    async def status_command(interaction: discord.Interaction) -> None:
        if await require_guild(interaction):
            stats = store.status_stats(service.started_at)
            stats["v14"] = store.v14_health()
            queue = getattr(service, "launch_queue", None)
            stats["event_queue"] = queue.stats() if queue else {}
            await send_card(interaction, status_card(stats))

    @tree.command(name="menu", description="Open the complete Gambit Jr command center")
    async def menu_command(interaction: discord.Interaction) -> None:
        if await require_guild(interaction):
            await send_card(interaction, menu_card(), True)

    @tree.command(name="help", description="Explain Gambit Jr commands and intelligence flow")
    async def help_command(interaction: discord.Interaction) -> None:
        if await require_guild(interaction):
            await send_card(interaction, menu_card(), True)

    @tree.command(name="performance", description="Show measured historical shadow performance")
    @app_commands.describe(period="7d, 30d, or all")
    async def performance_command(interaction: discord.Interaction, period: str = "all") -> None:
        if not await require_guild(interaction):
            return
        days = {"7d": 7, "30d": 30}.get(period.lower())
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat() if days else None
        report = store.performance(
            settings.scoring_version, since, settings.major_missed_runner_multiple
        )
        report["right_tail"] = store.right_tail_performance(settings.min_sample_for_edge_metrics)
        report["small_sample"] = (
            int(report.get("total_signals") or 0) < settings.min_sample_for_edge_metrics
        )
        await send_card(interaction, performance_card(report))

    @tree.command(name="scan", description="Run a parallel read-only scan for any supported CA")
    @app_commands.describe(address="Token contract address", chain="solana or bsc")
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
            ScanView(service, store, address.strip(), chain),
        )

    @tree.command(name="compare", description="Compare two token setups side by side")
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
    async def watchlist_command(interaction: discord.Interaction) -> None:
        if await require_guild(interaction):
            await send_card(
                interaction,
                watchlist_card(store.user_watchlist(interaction.guild_id, interaction.user.id)),
                True,
            )

    @tree.command(name="candidates", description="Show strongest active pre-signal candidates")
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
    async def token_command(interaction: discord.Interaction, address: str) -> None:
        await token_reply(interaction, address)

    @tree.command(name="smartmoney", description="Show labelled wallet evidence for a token")
    async def smartmoney_command(interaction: discord.Interaction, address: str) -> None:
        await token_reply(interaction, address, True)

    @tree.command(name="wallet", description="Inspect read-only wallet relationships and clusters")
    async def wallet_command(interaction: discord.Interaction, address: str) -> None:
        if await require_guild(interaction):
            await send_card(interaction, wallet_card(store.wallet_report(address.strip())), True)

    @tree.command(name="clusters", description="Show recently observed connected-wallet clusters")
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
    async def narrative_command(interaction: discord.Interaction, query: str = "") -> None:
        if await require_guild(interaction):
            await send_card(
                interaction,
                narrative_card(store.narrative_report(query or None), query or None),
                True,
            )

    @tree.command(name="setup", description="Admin: designate a channel for automatic alerts")
    @app_commands.default_permissions(manage_guild=True)
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
    async def server_settings_command(interaction: discord.Interaction) -> None:
        if await require_guild(interaction):
            await send_card(
                interaction, settings_card(store.guild_settings(interaction.guild_id)), True
            )

    @tree.command(
        name="test-alert", description="Admin: send a non-live card without creating intelligence"
    )
    @app_commands.default_permissions(manage_guild=True)
    async def test_alert_command(interaction: discord.Interaction) -> None:
        if not await require_guild(interaction):
            return
        permissions = getattr(interaction.user, "guild_permissions", None)
        if not permissions or not permissions.manage_guild:
            await send_text(interaction, "Manage Server permission is required.")
            return
        await send_card(interaction, test_alert_card())
        message = await interaction.original_response()
        store.record_test_alert(
            interaction.guild_id, interaction.channel_id, interaction.user.id, str(message.id)
        )

    @client.event
    async def on_ready() -> None:
        await tree.sync()

    service_task = asyncio.create_task(service.run(), name="intelligence-service")
    try:
        await client.start(settings.discord_token)
    finally:
        service.stop()
        await service_task

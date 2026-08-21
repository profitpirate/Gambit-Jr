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
    performance_card,
    rows_card,
    settings_card,
    smartmoney_card,
    status_card,
    test_alert_card,
    token_card,
)


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
        interaction: discord.Interaction, payload: dict, ephemeral: bool = False
    ) -> None:
        embed = discord.Embed.from_dict(payload["embed"])
        view = None
        if payload.get("links"):
            view = discord.ui.View(timeout=None)
            for label, url in payload["links"]:
                view.add_item(discord.ui.Button(label=label, url=url))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=ephemeral)

    async def require_guild(interaction: discord.Interaction) -> bool:
        if command_allowed(interaction):
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
            await send_card(interaction, status_card(store.status_stats(service.started_at)))

    @tree.command(name="performance", description="Show measured historical shadow performance")
    @app_commands.describe(period="7d, 30d, or all")
    async def performance_command(interaction: discord.Interaction, period: str = "all") -> None:
        if not await require_guild(interaction):
            return
        days = {"7d": 7, "30d": 30}.get(period.lower())
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat() if days else None
        await send_card(
            interaction,
            performance_card(
                store.performance(
                    settings.scoring_version, since, settings.major_missed_runner_multiple
                )
            ),
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
            await interaction.response.send_message("Token is not tracked.", ephemeral=True)
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

    @tree.command(name="setup", description="Admin: designate a channel for automatic alerts")
    @app_commands.default_permissions(manage_guild=True)
    async def setup_command(
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        alert_tier: str = "HOT",
    ) -> None:
        if not await require_guild(interaction):
            return
        permissions = getattr(interaction.user, "guild_permissions", None)
        if not permissions or not permissions.manage_guild:
            await interaction.response.send_message(
                "Manage Server permission is required.", ephemeral=True
            )
            return
        destination = channel or interaction.channel
        try:
            store.set_guild_settings(
                interaction.guild_id, destination.id, True, alert_tier, interaction.user.id
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
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
            await interaction.response.send_message(
                "Manage Server permission is required.", ephemeral=True
            )
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

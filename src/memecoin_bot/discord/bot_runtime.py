from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from memecoin_bot.analytics import format_performance, format_status


async def run_discord_bot(service: object, store: object, settings: object) -> None:
    try:
        import discord
        from discord import app_commands
    except ImportError as exc:
        raise RuntimeError("discord.py is required for slash commands") from exc

    intents = discord.Intents.none()
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)

    def allowed(interaction: discord.Interaction) -> bool:
        return interaction.channel_id == settings.discord_channel_id

    @tree.command(name="status", description="Show scanner health")
    async def status_command(interaction: discord.Interaction) -> None:
        if not allowed(interaction):
            await interaction.response.send_message("This bot uses one configured channel.", ephemeral=True)
            return
        await interaction.response.send_message(format_status(store.status_stats(service.started_at)))

    @tree.command(name="performance", description="Show measured signal performance")
    @app_commands.describe(period="7d, 30d, or all")
    async def performance_command(interaction: discord.Interaction, period: str = "all") -> None:
        if not allowed(interaction):
            await interaction.response.send_message("This bot uses one configured channel.", ephemeral=True)
            return
        days = {"7d": 7, "30d": 30}.get(period.lower())
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat() if days else None
        report = store.performance(settings.scoring_version, since)
        await interaction.response.send_message(format_performance(report))

    @client.event
    async def on_ready() -> None:
        await tree.sync()

    service_task = asyncio.create_task(service.run(), name="intelligence-service")
    try:
        await client.start(settings.discord_token)
    finally:
        service.stop()
        await service_task


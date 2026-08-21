from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from memecoin_bot.analytics import format_candidates, format_missed, format_performance, format_rejections, format_status


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
        configured = set(settings.discord_channel_ids or (() if settings.discord_channel_id is None else (settings.discord_channel_id,)))
        return interaction.channel_id in configured

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
        report = store.performance(settings.scoring_version, since, settings.major_missed_runner_multiple)
        await interaction.response.send_message(format_performance(report))

    @tree.command(name="candidates", description="Show strongest active pre-signal candidates")
    async def candidates_command(interaction: discord.Interaction) -> None:
        if not allowed(interaction):
            await interaction.response.send_message("This bot uses one configured channel.", ephemeral=True)
            return
        await interaction.response.send_message(format_candidates(store.candidates_report(10)))

    @tree.command(name="rejections", description="Show hard and temporary rejection reasons")
    async def rejections_command(interaction: discord.Interaction) -> None:
        if not allowed(interaction):
            await interaction.response.send_message("This bot uses one configured channel.", ephemeral=True)
            return
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        await interaction.response.send_message(format_rejections(store.rejection_report(since)))

    @tree.command(name="missed", description="Show recently observed runners without a qualified signal")
    @app_commands.describe(period="24h, 7d, or 30d")
    async def missed_command(interaction: discord.Interaction, period: str = "24h") -> None:
        if not allowed(interaction):
            await interaction.response.send_message("This bot uses one configured channel.", ephemeral=True)
            return
        hours = {"24h": 24, "7d": 168, "30d": 720}.get(period.lower(), 24)
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = store.missed_report(since, settings.missed_runner_multiple, 8)
        await interaction.response.send_message(format_missed(rows, hours))

    async def token_reply(interaction: discord.Interaction, address: str, smart_only: bool = False) -> None:
        if not allowed(interaction):
            await interaction.response.send_message("This channel is not authorized.", ephemeral=True)
            return
        data = store.token_intelligence(address)
        if not data:
            await interaction.response.send_message("Token is not tracked.", ephemeral=True)
            return
        selected = data.get("wallet_intelligence") if smart_only else {
            key: data.get(key) for key in ("name", "symbol", "chain", "token_address", "state",
                "radar_score", "normalized_score", "confidence", "radar_market_cap_usd",
                "current_market_cap_usd", "current_liquidity_usd", "max_multiple",
                "signal_status", "wallet_intelligence")
        }
        body = json.dumps(selected, indent=2, default=str)
        await interaction.response.send_message(f"```json\n{body[:1850]}\n```", ephemeral=True)

    @tree.command(name="radar", description="Show active Radar calls")
    async def radar_command(interaction: discord.Interaction) -> None:
        if not allowed(interaction):
            await interaction.response.send_message("This channel is not authorized.", ephemeral=True); return
        rows = store.radar_board(10)
        text = "\n".join(f"{r['name'] or r['symbol']} · {r['chain']} · {r['state']} · {r['radar_score'] or 0:.1f}" for r in rows)
        await interaction.response.send_message(text or "No Radar calls.")

    @tree.command(name="runners", description="Show active runners")
    async def runners_command(interaction: discord.Interaction) -> None:
        rows = [r for r in store.radar_board(100) if (r.get("max_multiple") or 0) >= 2]
        await interaction.response.send_message("\n".join(f"{r['name'] or r['symbol']} · {r['max_multiple']:.2f}x" for r in rows[:10]) or "No runners.")

    @tree.command(name="failed", description="Show failed calls")
    async def failed_command(interaction: discord.Interaction) -> None:
        rows = [r for r in store.radar_board(100) if r.get("signal_status") == "FAILED"]
        await interaction.response.send_message("\n".join(str(r['name'] or r['symbol']) for r in rows[:10]) or "No failures.")

    @tree.command(name="token", description="Show current token intelligence")
    async def token_command(interaction: discord.Interaction, address: str) -> None:
        await token_reply(interaction, address)

    @tree.command(name="smartmoney", description="Show wallet intelligence for a token")
    async def smartmoney_command(interaction: discord.Interaction, address: str) -> None:
        await token_reply(interaction, address, True)

    @client.event
    async def on_ready() -> None:
        await tree.sync()

    service_task = asyncio.create_task(service.run(), name="intelligence-service")
    try:
        await client.start(settings.discord_token)
    finally:
        service.stop()
        await service_task


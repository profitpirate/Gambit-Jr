"""Discord is the named phone/laptop UI. Only the configured owner has access."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import discord
from discord import app_commands

from .v12_prearmed_operator_backend import ControlBlocked
from .v12_prearmed_ops import OperationLedger


def readable(value):
    return {
        "STOP_REQUESTED": "Stop requested",
        "KILL_SWITCH": "Stopped by owner",
        "FEED_DISCONNECTED": "Market feed disconnected",
        "FEED_STALE": "Market data delayed",
        "BALANCE_STALE": "Wallet balance needs refreshing",
        "BALANCE_BELOW_RESERVE": "Low SOL balance",
        "UNRESOLVED_ORDER": "Waiting for transaction confirmation",
        "CREATOR_NOT_IN_FROZEN_MODEL": "Skipped: creator not approved",
        "CREATOR_NOT_RECOGNISED": "Skipped: creator not on an enabled list",
        "LIBRARY_SOCIAL_HISTORY_MISSING": "Skipped: promoted creator needs prior social history",
        "SOCIAL_STATUS_PATH_INVALID": "Skipped: invalid social post link",
        "RPC_HTTP_REJECTED": "Provider request failed",
        "COMPLETE": "Done",
        "UNKNOWN": "Needs attention",
        "RUNNING": "In progress",
        "PENDING": "Awaiting confirmation",
        "FAILED": "Failed",
        "CANCELLED": "Cancelled",
        "ACKNOWLEDGED": "Reviewed",
    }.get(value, str(value).replace("_", " ").lower().capitalize())


def status_text(report):
    status, database = report.get("status", {}), report.get("database", {})
    active = report.get("service", {}).get("ActiveState") == "active"
    age = report.get("status_age_seconds")
    current = active and isinstance(age, (int, float)) and 0 <= age <= 10
    allowed = current and status.get("entries_allowed") is True
    heading = (
        "🟢 Trading on" if allowed else ("🟠 Trading paused" if active else "🔴 Trading stopped")
    )
    if active and not current:
        heading = "🟠 Live status delayed"
    positions = len(database.get("positions", [])) if database.get("available") else "?"
    pending = len(database.get("pending_orders", [])) if database.get("available") else "?"
    lines = [f"**{heading}**", f"Positions: **{positions}** · Pending: **{pending}**"]
    if current:
        lines.append(
            f"Market feed: {'connected' if status.get('feed_connected') else 'disconnected'}"
        )
    if active and not current:
        lines.append("Live data is out of date. Check /bot logs.")
    elif active and not allowed:
        reasons = status.get("entry_blockers", [])
        lines.append("Why: " + ("; ".join(readable(x) for x in reasons[:2]) or "See /bot logs"))
    health = report.get("operator_health", {})
    if health.get("problems"):
        lines.append("⚠️ " + "; ".join(health["problems"][:2]))
    if report.get("errors"):
        lines.append("⚠️ Some checks failed. See /bot logs.")
    proof = report.get("execution_test", {})
    verified = (
        proof.get("state") == "COMPLETE"
        and proof.get("matches_installed_image") is True
        and proof.get("residual_tokens_raw") == 0
    )
    if verified:
        lines.append("Buy + sell test: **passed**")
    elif proof.get('state') == 'COMPLETE' and proof.get('residual_tokens_raw') == 0:
        lines.append("Buy + sell test: **previous release passed**")
    else:
        lines.append("Buy + sell test: **not verified**")
    if database.get("available"):
        lines.append(f"Strategy trades closed: **{database.get('state', {}).get('closed', 0)}**")
        realised = database.get('state', {}).get('realized_lamports')
        if type(realised) is int:
            lines.append(f"Closed-trade net P/L: **{realised/1e9:+.6f} SOL** (fees included)")
        lines.append("Daily P/L: 00:05 UK · manual activity and test trades excluded")
    library = status.get("creator_library", {})
    if current and library.get("state") == "CONNECTED":
        if library.get('promoted') == 0:
            lines.append("Creator rules: **original tested list** · /bot library")
        else:
            lines.append(f"Creator library: **{library.get('promoted', 0)} promoted** · /bot library")
    if not active:
        lines.append(
            "Start: /bot start" if positions == 0 and pending == 0 else "Recover: /bot recover"
        )
    return "\n".join(lines)


def library_text(report):
    library = report["library"]
    connected = report.get("loaded_by_live_bot") is True
    lines = [
        "**Creator library connected**"
        if connected
        else "**Library installed · live access not confirmed**"
    ]
    creator = report.get("creator")
    if creator:
        lines.append(f"`{creator['creator']}`")
        lines.append("Library: **" + readable(creator["library_status"]) + "**")
        lines.append(
            "Creator check: **passed**"
            if creator["creator_recognised"]
            else "Creator check: **not eligible**"
        )
        if (creator["creator_recognised"] and creator.get("historical_social_required", True)
                and not creator["prearmed_social_available"]):
            lines.append("Entry blocked: prior social history is missing.")
        elif creator["creator_recognised"]:
            lines.append("Fresh social post and all launch checks still required.")
    else:
        if library['promoted'] == 0:
            lines.append(f"**Original tested rules** · {library['recognised_creators']} creators")
            lines.append("Historical handle match and fresh social post required.")
            lines.append(f"Extra promotions disabled · {library['shortlisted']} research-only")
            return "\n".join(lines)
        lines.append(f"**{library['promoted']} promoted** · {library['shortlisted']} research-only")
        lines.append(f"{library['additional_creators']} additional creators recognised")
        if library.get("promoted_social_history_required") is False:
            lines.append("Promotion passes the creator check. Fresh social post still required.")
        elif library["promoted_missing_social_history"]:
            lines.append(f"{library['promoted_missing_social_history']} need prior social history before an entry can pass.")
    if not library["history_certified"]:
        lines.append("Historical data certification: pending.")
    return "\n".join(lines)


def result_text(operation, state, result):
    action = operation["action"]
    title = readable(action)
    if state == "UNKNOWN":
        return f"**⚠️ {title} not confirmed**\nCheck /bot operations before retrying."
    if state != "COMPLETE":
        return f"**❌ {title} failed**\n{result.get('message', 'Check /bot operations.')}"[:1000]
    messages = {
        "start": "Start accepted. Check /bot status.",
        "restart": "Restart accepted. Check /bot status.",
        "stop": "Trading stopped. No open positions or pending orders.",
        "check": "Checks passed. Trading stays stopped.",
        "configure": "Setting saved. Use /bot start to resume.",
        "update": "Update installed. Trading stays stopped.",
        "transfer": "Transfer confirmed. Trading stays stopped.",
        "live_test": "Buy + sell passed. No tokens left. Trading stays stopped.",
        "recover": "Recovery started. Check /bot status.",
        "reset_safety": "Safety trip reset. Loss limits still apply. Trading stays stopped.",
        "acknowledge": "Reviewed. Original action was not repeated.",
    }
    text = "✅ " + messages[action]
    signature = result.get("evidence", {}).get("signature")
    if signature:
        text += f"\n[View transaction](https://solscan.io/tx/{signature})"
    return text


class ConfirmOperation(discord.ui.View):
    def __init__(self, panel, intent):
        super().__init__(timeout=120)
        self.panel, self.intent = panel, intent

    async def interaction_check(self, interaction):
        return await self.panel.authorize(interaction)

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            operation = self.panel.ledger.claim(interaction.user.id, self.intent["id"])
        except (ValueError, RuntimeError, PermissionError) as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        for item in self.children:
            item.disabled = True
        self.stop()
        try:
            result = await asyncio.to_thread(self.panel.backend.execute, operation)
            state = result.get("state", "COMPLETE")
        except ControlBlocked as error:
            state = (
                "UNKNOWN"
                if operation["action"] in {"transfer", "live_test", "update"}
                else "FAILED"
            )
            result = {
                "message": error.public_description,
                "step": error.step,
                "exit_code": error.exit_code,
            }
        except Exception as error:  # noqa: BLE001 - Boundary records failure and preserves recovery state.
            # The subprocess may have made progress before its response was lost.
            state = "UNKNOWN"
            result = {
                "message": "Operation outcome is unknown. Check /bot operations before retrying.",
                "error_type": type(error).__name__,
            }
        self.panel.ledger.finish(operation["id"], state, result)
        if state == "COMPLETE" and result.get("acknowledged_operation_id"):
            self.panel.ledger.acknowledge(interaction.user.id, result["acknowledged_operation_id"])
        await interaction.followup.send(
            result_text(operation, state, result),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        # No backend action occurs; the intent also expires after two minutes.
        self.stop()
        for item in self.children:
            item.disabled = True
        self.panel.ledger.db.execute(
            "UPDATE operations SET state='CANCELLED' WHERE id=? AND state='PENDING'",
            (self.intent["id"],),
        )
        self.panel.ledger.event(self.intent["id"], "CANCELLED")
        await interaction.response.edit_message(content="Cancelled.", view=self)


class BotPanel(app_commands.Group):
    def __init__(self, ledger, backend):
        super().__init__(name="bot", description="Owner controls, logs and release status")
        self.ledger, self.backend = ledger, backend

    async def authorize(self, interaction):
        try:
            self.ledger.authorize(interaction.user.id)
            return True
        except PermissionError:
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return False

    async def interaction_check(self, interaction):
        return await self.authorize(interaction)

    async def proposal(self, interaction, action, payload=None):
        try:
            if action == "acknowledge":
                row = self.ledger.db.execute(
                    "SELECT state FROM operations WHERE id=? AND owner=?",
                    ((payload or {}).get("operation_id"), self.ledger.owner_id),
                ).fetchone()
                if not row or row["state"] != "UNKNOWN":
                    raise ValueError("Only a recorded operation in UNKNOWN state can be reviewed.")
            intent = self.ledger.propose(interaction.user.id, action, payload or {})
        except (ValueError, PermissionError) as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        explanation = {
            "start": "Run checks, then turn trading on.",
            "stop": "Stop new buys, close positions, then stop.",
            "restart": "Close positions, run checks, then restart.",
            "check": "Stop trading and run checks. Sends test alerts.",
            "transfer": "Stop trading and send SOL. Fees are extra; 0.03 SOL stays reserved.",
            "configure": "Save this setting. Trading will stop.",
            "update": "Install this update. Trading will stop.",
            "live_test": "Buy 0.001 SOL, then sell. Maximum cost: 0.01 SOL. Trading stays stopped.",
            "recover": "Confirm pending trades and close positions. No new buys.",
            "reset_safety": "Reset the safety trip. Loss limits stay active. Trading stays stopped.",
            "acknowledge": "Review this unresolved action after checking the wallet is clear. No repeat.",
        }[action]
        detail = intent["payload"]
        if action == "transfer":
            explanation += (
                f"\n**{detail['amount_lamports'] / 1e9:g} SOL** to\n`{detail['destination']}`"
            )
        elif action == "configure":
            setting = (
                "Position size" if detail["setting"] == "position_percent" else "Maximum positions"
            )
            explanation += f"\n**{setting}: {detail['value']}{'%' if detail['setting'] == 'position_percent' else ''}**"
        elif action == "update":
            explanation += f"\nRelease: `{detail['release_id'][:12]}`"
        elif action == "acknowledge":
            explanation += f"\nAction: `{detail['operation_id']}`"
        await interaction.response.send_message(
            f"**{readable(action)}?**\n{explanation}\nConfirm within 2 minutes.",
            view=ConfirmOperation(self, intent),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(description="Current health, positions and why trading is blocked")
    async def status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        report = await asyncio.to_thread(self.backend.status)
        await interaction.followup.send(status_text(report), ephemeral=True)

    @app_commands.command(description="See the connected creator library or check a creator wallet")
    async def library(self, interaction: discord.Interaction, creator: str = ""):
        await interaction.response.defer(ephemeral=True)
        from .v12_prearmed_library import validate_creator_query

        if creator:
            try:
                validate_creator_query(creator)
            except ValueError:
                await interaction.followup.send(
                    "Enter a full, valid creator wallet address.", ephemeral=True
                )
                return
        report = await asyncio.to_thread(self.backend.library, creator)
        await interaction.followup.send(
            library_text(report), ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    @app_commands.command(
        description="Download timestamped, sanitized activity and rejection details"
    )
    async def logs(
        self,
        interaction: discord.Interaction,
        hours: app_commands.Range[int, 1, 168] = 24,
        token: str = "",
    ):
        await interaction.response.defer(ephemeral=True)
        report = await asyncio.to_thread(self.backend.logs, hours, token)
        events = report["events"]
        lines = [
            f"**Gambit activity — last {hours} hours**",
            f"{report['matching_events']} events · UTC",
        ]
        for row in events[-6:]:
            payload = row["payload"]
            stamp = datetime.fromtimestamp(row["ts_ns"] / 1e9, UTC).strftime("%H:%M:%S")
            label = payload.get("reason") or row["event"]
            lines.append(f"**{stamp}** {readable(label)}")
        if report["truncated"]:
            lines.append(
                "File contains the latest 5,000 events. Use a shorter time range for more detail."
            )
        await interaction.followup.send(
            "\n".join(lines)[:1900],
            file=discord.File(
                io.BytesIO(json.dumps(report, indent=2).encode()), filename="gambit-activity.json"
            ),
            ephemeral=True,
        )

    @app_commands.command(description="See operation outcomes, including unresolved commands")
    async def operations(self, interaction: discord.Interaction):
        rows = self.ledger.history(interaction.user.id)
        lines = ["**Recent actions · UTC**"]
        for row in rows[:6]:
            stamp = datetime.fromtimestamp(row["created_ns"] / 1e9, UTC).strftime("%d %b %H:%M")
            lines.append(f"{stamp} · {readable(row['action'])} · **{readable(row['state'])}**")
            if row["state"] == "UNKNOWN":
                lines.append(f"Action ID: `{row['id']}`")
        await interaction.response.send_message(
            "\n".join(lines) if rows else "No actions recorded.",
            ephemeral=True,
            file=discord.File(
                io.BytesIO(json.dumps(rows, indent=2).encode()), filename="action-details.json"
            ),
        )

    @app_commands.command(description="Check and enable live trading")
    async def start(self, interaction: discord.Interaction):
        await self.proposal(interaction, "start")

    @app_commands.command(description="Block entries, finish exits, then stop")
    async def stop(self, interaction: discord.Interaction):
        await self.proposal(interaction, "stop")

    @app_commands.command(description="Drain safely, check and restart the installed release")
    async def restart(self, interaction: discord.Interaction):
        await self.proposal(interaction, "restart")

    @app_commands.command(description="Run technical checks with trading stopped")
    async def check(self, interaction: discord.Interaction):
        await self.proposal(interaction, "check")

    @app_commands.command(description="Preview and confirm a SOL transfer from the trading wallet")
    async def transfer(self, interaction: discord.Interaction, destination: str, amount_sol: str):
        await self.proposal(
            interaction, "transfer", {"destination": destination, "amount_sol": amount_sol}
        )

    @app_commands.command(description="Save a supported risk limit; leaves trading stopped")
    @app_commands.choices(
        setting=[
            app_commands.Choice(name="Position size percent (1–10)", value="position_percent"),
            app_commands.Choice(name="Maximum positions (1–2)", value="maximum_positions"),
        ]
    )
    async def configure(self, interaction: discord.Interaction, setting: str, value: int):
        await self.proposal(interaction, "configure", {"setting": setting, "value": value})

    @app_commands.command(description="List immutable releases staged by the deployment process")
    async def releases(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        rows = await asyncio.to_thread(self.backend.releases)
        await interaction.followup.send(
            (
                "**Available updates**\n"
                + "\n".join(f"Release `{row[:12]}`" for row in rows)
                + "\nUse /bot update and choose a release."
            )
            if rows
            else "No updates available.",
            ephemeral=True,
        )

    @app_commands.command(description="Install an exact staged release; leaves trading stopped")
    async def update(self, interaction: discord.Interaction, release_id: str):
        await self.proposal(interaction, "update", {"release_id": release_id})

    @update.autocomplete("release_id")
    async def release_options(self, interaction, current):
        self.ledger.authorize(interaction.user.id)
        rows = await asyncio.to_thread(self.backend.releases)
        return [
            app_commands.Choice(name="Release " + row[:12], value=row)
            for row in rows
            if row.startswith(current)
        ][:25]

    @app_commands.command(
        description="One controlled buy/sell verification, maximum 0.01 SOL budget"
    )
    async def live_test(self, interaction: discord.Interaction):
        await self.proposal(interaction, "live_test")

    @app_commands.command(description="Restore reconciliation and exits without opening new buys")
    async def recover(self, interaction: discord.Interaction):
        await self.proposal(interaction, "recover")

    @app_commands.command(
        description="Acknowledge a safety trip only after positions and orders are cleared"
    )
    async def reset_safety(self, interaction: discord.Interaction):
        await self.proposal(interaction, "reset_safety")

    @app_commands.command(
        description="Review an unresolved command after a stopped, flat-wallet check"
    )
    async def acknowledge(self, interaction: discord.Interaction, operation_id: str):
        await self.proposal(interaction, "acknowledge", {"operation_id": operation_id})


def main():
    from .v12_prearmed_operator_backend import OperatorBackend

    owner = os.environ.get("V12_DISCORD_USER_ID", "")
    ledger = OperationLedger(
        Path(
            os.getenv(
                "GAMBIT_OPS_DATABASE", "/opt/gambit-v12-staging/operator-data/operations.sqlite"
            )
        ),
        owner_id=owner,
        recover_interrupted=True,
    )
    backend = OperatorBackend()

    class Client(discord.Client):
        async def setup_hook(self):
            self.tree = app_commands.CommandTree(self)
            self.tree.add_command(BotPanel(ledger, backend))
            await self.tree.sync()

            async def on_error(interaction, error):
                print(
                    json.dumps(
                        {
                            "event": "DISCORD_COMMAND_FAILED",
                            "ts_ns": time.time_ns(),
                            "error_type": type(error).__name__,
                        }
                    ),
                    flush=True,
                )
                message = "Command failed. Check /bot operations before retrying."
                if interaction.response.is_done():
                    await interaction.followup.send(message, ephemeral=True)
                else:
                    await interaction.response.send_message(message, ephemeral=True)

            self.tree.on_error = on_error
            from .v12_prearmed_health import monitor

            self.health_task = asyncio.create_task(monitor(backend))

            def health_done(task):
                if not task.cancelled():
                    print(
                        json.dumps(
                            {
                                "event": "OPERATOR_HEALTH_WORKER_STOPPED",
                                "ts_ns": time.time_ns(),
                                "error_type": type(task.exception()).__name__,
                            }
                        ),
                        flush=True,
                    )
                    asyncio.create_task(self.close())

            self.health_task.add_done_callback(health_done)

        async def on_ready(self):
            print(
                json.dumps({"event": "DISCORD_CONTROLS_CONNECTED", "owner_configured": True}),
                flush=True,
            )

    # Discord logging must not serialize interaction tokens or operator secrets.
    logging.getLogger("discord").setLevel(logging.WARNING)
    client = Client(intents=discord.Intents.none(), allowed_mentions=discord.AllowedMentions.none())
    client.run(os.environ["DISCORD_TOKEN"], log_handler=None)


if __name__ == "__main__":
    main()

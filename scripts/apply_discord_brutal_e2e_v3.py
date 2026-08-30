from __future__ import annotations

import re
from pathlib import Path


def read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


def sub_once(text: str, pattern: str, replacement: str, label: str) -> str:
    new, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one regex match, found {count}")
    return new


# ---------------------------------------------------------------------------
# Discord runtime: isolate command reads from the hot writer connection, make
# menu state persistent, offload expensive SQLite reports, and make test-alert
# exercise the real configured channel rather than merely editing the command.
# ---------------------------------------------------------------------------
path = "src/memecoin_bot/discord/bot_runtime.py"
text = read(path)

text = replace_once(
    text,
    "from memecoin_bot.discord.validation import validate_message\n",
    "from memecoin_bot.database import Store\nfrom memecoin_bot.discord.validation import validate_message\n",
    "bot_runtime Store import",
)
text = replace_once(
    text,
    '        "menu",\n',
    "",
    "menu public visibility",
)
text = replace_once(
    text,
    '        "token",\n',
    '        "test-alert",\n        "token",\n',
    "test-alert private visibility",
)
text = replace_once(
    text,
    "COMMAND_TIMEOUT_SECONDS = 30.0\n",
    "COMMAND_TIMEOUT_SECONDS = 30.0\nCOMMAND_DB_TIMEOUT_SECONDS = 15.0\n",
    "command DB timeout constant",
)
text = replace_once(
    text,
    '    log = logging.getLogger("memecoin_bot.discord")\n    menu_data = CommandCenterData(service, store, settings)\n',
    '''    log = logging.getLogger("memecoin_bot.discord")
    # Discord reporting gets its own SQLite connection. The intelligence pipeline
    # can keep writing through the service Store without a long report query or
    # command read blocking the client event loop or sharing one connection across
    # threads. WAL keeps these readers current while preserving one database truth.
    service_store = store
    owns_command_store = isinstance(service_store, Store)
    if owns_command_store:
        store = Store(service_store.path, service_store.migrations_dir)
        store.migrate()
    menu_data = CommandCenterData(service, store, settings)
''',
    "command Store isolation",
)
text = replace_once(
    text,
    "    def track_command(callback: CommandCallback) -> CommandCallback:\n",
    '''    async def store_call(method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run SQLite/report work off the Discord event loop with a hard ceiling."""
        return await asyncio.wait_for(
            asyncio.to_thread(method, *args, **kwargs),
            timeout=COMMAND_DB_TIMEOUT_SECONDS,
        )

    def track_command(callback: CommandCallback) -> CommandCallback:
''',
    "store_call helper",
)

old_status = '''    async def status_command(interaction: discord.Interaction) -> None:
        if await require_guild(interaction):
            stats = store.status_stats(service.started_at)
            stats["v14"] = store.v14_health()
            queue = getattr(service, "launch_queue", None)
            stats["event_queue"] = queue.stats() if queue else {}
            historical = getattr(service, "historical_context", None)
            stats["historical_context"] = historical.status() if historical else {"enabled": False}
            stats["model"] = operator_model_status(settings)
            runtime_health = getattr(service, "runtime_health", None)
            stats["runtime"] = runtime_health() if callable(runtime_health) else {}
            await send_card(interaction, status_card(stats))
'''
new_status = '''    async def status_command(interaction: discord.Interaction) -> None:
        if await require_guild(interaction):
            stats, v14 = await asyncio.gather(
                store_call(store.status_stats, service.started_at),
                store_call(store.v14_health),
            )
            stats["v14"] = v14
            queue = getattr(service, "launch_queue", None)
            stats["event_queue"] = queue.stats() if queue else {}
            historical = getattr(service, "historical_context", None)
            stats["historical_context"] = (
                await asyncio.to_thread(historical.status)
                if historical and callable(getattr(historical, "status", None))
                else {"enabled": False}
            )
            stats["model"] = operator_model_status(settings)
            runtime_health = getattr(service, "runtime_health", None)
            stats["runtime"] = runtime_health() if callable(runtime_health) else {}
            await send_card(interaction, status_card(stats))
'''
text = replace_once(text, old_status, new_status, "nonblocking status")

old_menu = '''    async def menu_command(interaction: discord.Interaction) -> None:
        if await require_guild(interaction):
            payload = await menu_data.render("home", interaction)
            await send_card(interaction, payload, True, MenuView(menu_data, log, timeout=900))
'''
new_menu = '''    async def menu_command(interaction: discord.Interaction) -> None:
        if await require_guild(interaction):
            payload = await menu_data.render("home", interaction)
            # A public persistent command center receives a fresh component
            # interaction token on every click and therefore remains usable well
            # beyond the 15-minute lifetime of an ephemeral interaction token.
            await send_card(interaction, payload, False, MenuView(menu_data, log, timeout=None))
'''
text = replace_once(text, old_menu, new_menu, "persistent public menu")

old_performance = '''        report = store.performance(
            settings.scoring_version, since, settings.major_missed_runner_multiple
        )
        report["right_tail"] = store.right_tail_performance(settings.min_sample_for_edge_metrics)
        report["v15"] = store.v15_performance(settings.min_sample_for_edge_metrics)
'''
new_performance = '''        report, right_tail, v15 = await asyncio.gather(
            store_call(
                store.performance,
                settings.scoring_version,
                since,
                settings.major_missed_runner_multiple,
            ),
            store_call(store.right_tail_performance, settings.min_sample_for_edge_metrics),
            store_call(store.v15_performance, settings.min_sample_for_edge_metrics),
        )
        report["right_tail"] = right_tail
        report["v15"] = v15
'''
text = replace_once(text, old_performance, new_performance, "nonblocking performance")

text = replace_once(
    text,
    '''        created = store.add_watch(
            interaction.guild_id, interaction.user.id, chain.lower(), address.strip()
        )
''',
    '''        created = await store_call(
            store.add_watch,
            interaction.guild_id,
            interaction.user.id,
            chain.lower(),
            address.strip(),
        )
''',
    "nonblocking watch",
)
text = replace_once(
    text,
    '''        removed = store.remove_watch(
            interaction.guild_id, interaction.user.id, chain.lower(), address.strip()
        )
''',
    '''        removed = await store_call(
            store.remove_watch,
            interaction.guild_id,
            interaction.user.id,
            chain.lower(),
            address.strip(),
        )
''',
    "nonblocking unwatch",
)
text = replace_once(
    text,
    '''                watchlist_card(store.user_watchlist(interaction.guild_id, interaction.user.id)),
''',
    '''                watchlist_card(
                    await store_call(store.user_watchlist, interaction.guild_id, interaction.user.id)
                ),
''',
    "nonblocking watchlist",
)
text = replace_once(
    text,
    '''                store.candidates_report(10),
''',
    '''                await store_call(store.candidates_report, 10),
''',
    "nonblocking candidates",
)
text = replace_once(
    text,
    '''        report = store.rejection_report((datetime.now(UTC) - timedelta(hours=24)).isoformat())
''',
    '''        report = await store_call(
            store.rejection_report,
            (datetime.now(UTC) - timedelta(hours=24)).isoformat(),
        )
''',
    "nonblocking rejections",
)
text = replace_once(
    text,
    '''        rows = store.missed_report(
            (datetime.now(UTC) - timedelta(hours=hours)).isoformat(),
            settings.missed_runner_multiple,
            8,
        )
''',
    '''        rows = await store_call(
            store.missed_report,
            (datetime.now(UTC) - timedelta(hours=hours)).isoformat(),
            settings.missed_runner_multiple,
            8,
        )
''',
    "nonblocking missed",
)
text = replace_once(
    text,
    '''        data = store.token_intelligence(address)
''',
    '''        data = await store_call(store.token_intelligence, address)
''',
    "nonblocking token intelligence",
)

# Fetch Radar board once, off-loop, for each report command.
text = replace_once(
    text,
    '''        await send_card(
            interaction,
            rows_card(
                "RADAR • ACTIVE INTELLIGENCE",
                store.radar_board(10),
''',
    '''        rows = await store_call(store.radar_board, 10)
        await send_card(
            interaction,
            rows_card(
                "CALLS • ACTIVE INTELLIGENCE",
                rows,
''',
    "nonblocking radar/calls",
)
text = replace_once(
    text,
    '''        rows = [r for r in store.radar_board(100) if (r.get("max_multiple") or 0) >= 2]
''',
    '''        board = await store_call(store.radar_board, 100)
        rows = [r for r in board if (r.get("max_multiple") or 0) >= 2]
''',
    "nonblocking runners",
)
text = replace_once(
    text,
    '''        rows = [r for r in store.radar_board(100) if r.get("signal_status") == "FAILED"]
''',
    '''        board = await store_call(store.radar_board, 100)
        rows = [r for r in board if r.get("signal_status") == "FAILED"]
''',
    "nonblocking failed",
)
text = replace_once(
    text,
    '''            await send_card(interaction, wallet_card(store.wallet_report(address.strip())), True)
''',
    '''            await send_card(
                interaction,
                wallet_card(await store_call(store.wallet_report, address.strip())),
                True,
            )
''',
    "nonblocking wallet",
)
text = replace_once(
    text,
    '''                    store.cluster_report(10),
''',
    '''                    await store_call(store.cluster_report, 10),
''',
    "nonblocking clusters",
)
text = replace_once(
    text,
    '''                creator_card(store.creator_report(address.strip()), address.strip()),
''',
    '''                creator_card(
                    await store_call(store.creator_report, address.strip()), address.strip()
                ),
''',
    "nonblocking creator",
)
text = replace_once(
    text,
    '''                narrative_card(store.narrative_report(query or None), query or None),
''',
    '''                narrative_card(
                    await store_call(store.narrative_report, query or None), query or None
                ),
''',
    "nonblocking narrative",
)

old_setup = '''            store.set_guild_settings(
                interaction.guild_id,
                destination.id,
                True,
                alert_tier,
                interaction.user.id,
                daily_report,
                [value.strip().lower() for value in chains.split(",") if value.strip()],
            )
'''
new_setup = '''            await store_call(
                store.set_guild_settings,
                interaction.guild_id,
                destination.id,
                True,
                alert_tier,
                interaction.user.id,
                daily_report,
                [value.strip().lower() for value in chains.split(",") if value.strip()],
            )
'''
text = replace_once(text, old_setup, new_setup, "nonblocking setup write")
text = replace_once(
    text,
    '''            interaction, settings_card(store.guild_settings(interaction.guild_id)), True
''',
    '''            interaction,
            settings_card(await store_call(store.guild_settings, interaction.guild_id)),
            True,
''',
    "nonblocking setup settings read",
)
text = replace_once(
    text,
    '''                interaction, settings_card(store.guild_settings(interaction.guild_id)), True
''',
    '''                interaction,
                settings_card(await store_call(store.guild_settings, interaction.guild_id)),
                True,
''',
    "nonblocking server settings",
)

old_test_alert = '''    async def test_alert_command(interaction: discord.Interaction) -> None:
        if not await require_guild(interaction):
            return
        permissions = getattr(interaction.user, "guild_permissions", None)
        if not permissions or not permissions.manage_guild:
            await send_text(interaction, "Manage Server permission is required.")
            return
        message = await send_card(interaction, test_alert_card())
        remote_id = str(message.id) if message is not None else None
        try:
            await asyncio.to_thread(
                store.record_test_alert,
                interaction.guild_id,
                interaction.channel_id,
                interaction.user.id,
                remote_id,
            )
        except Exception as error:  # noqa: BLE001 - optional audit must not fail delivery
            _safe_failure_log(
                log,
                "test_alert_audit_failed",
                error,
                command_name="test-alert",
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                result="audit_failure_card_succeeded",
            )
'''
new_test_alert = '''    async def test_alert_command(interaction: discord.Interaction) -> None:
        if not await require_guild(interaction):
            return
        permissions = getattr(interaction.user, "guild_permissions", None)
        if not permissions or not permissions.manage_guild:
            await send_text(interaction, "Manage Server permission is required.")
            return
        guild_settings = await store_call(store.guild_settings, interaction.guild_id)
        destination_id = int(guild_settings.get("alert_channel_id") or interaction.channel_id)
        destination = client.get_channel(destination_id)
        if destination is None:
            try:
                destination = await asyncio.wait_for(
                    client.fetch_channel(destination_id), timeout=8.0
                )
            except Exception as error:  # noqa: BLE001 - user gets a safe command response
                _safe_failure_log(
                    log,
                    "test_alert_destination_failed",
                    error,
                    command_name="test-alert",
                    guild_id=interaction.guild_id,
                    channel_id=destination_id,
                    result="failure",
                )
                await send_text(
                    interaction,
                    "Test alert could not resolve the configured alert channel. Run /setup again.",
                )
                return
        if not hasattr(destination, "send"):
            await send_text(interaction, "Configured alert destination is not a message channel.")
            return
        embed = validate_message(card_payload=test_alert_card())
        try:
            message = await asyncio.wait_for(
                destination.send(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                ),
                timeout=10.0,
            )
        except Exception as error:  # noqa: BLE001 - transport boundary must be contained
            _safe_failure_log(
                log,
                "test_alert_delivery_failed",
                error,
                command_name="test-alert",
                guild_id=interaction.guild_id,
                channel_id=destination_id,
                result="failure",
            )
            await send_text(
                interaction,
                f"Test alert failed to deliver to <#{destination_id}>. Check bot permissions.",
            )
            return
        remote_id = str(getattr(message, "id", "")) or None
        try:
            await store_call(
                store.record_test_alert,
                interaction.guild_id,
                destination_id,
                interaction.user.id,
                remote_id,
            )
        except Exception as error:  # noqa: BLE001 - optional audit must not fail delivery
            _safe_failure_log(
                log,
                "test_alert_audit_failed",
                error,
                command_name="test-alert",
                guild_id=interaction.guild_id,
                channel_id=destination_id,
                result="audit_failure_card_succeeded",
            )
        await send_text(interaction, f"Test alert delivered to <#{destination_id}>.")
'''
text = replace_once(text, old_test_alert, new_test_alert, "real test-alert transport")

text = replace_once(
    text,
    '            persistent_view_count=2,\n',
    '            persistent_view_count=3,\n',
    "persistent view count",
)
text = replace_once(
    text,
    '''    finally:
        service.stop()
        await service_task
''',
    '''    finally:
        service.stop()
        await service_task
        if owns_command_store:
            store.close()
''',
    "close command Store",
)
write(path, text)


# ---------------------------------------------------------------------------
# Components: persistent menu by default + strict acknowledgement and operation
# ceilings so no button/select can leave Discord showing a dead interaction.
# ---------------------------------------------------------------------------
path = "src/memecoin_bot/discord/command_center.py"
text = read(path)
text = replace_once(
    text,
    'PAGE_TITLES = {\n',
    'COMPONENT_ACK_TIMEOUT_SECONDS = 2.5\nCOMPONENT_TIMEOUT_SECONDS = 30.0\n\nPAGE_TITLES = {\n',
    "component timeout constants",
)
text = replace_once(
    text,
    '        timeout: float | None = 900,\n',
    '        timeout: float | None = None,\n',
    "persistent MenuView default",
)
text = replace_once(
    text,
    '''            if not interaction.response.is_done():
                await interaction.response.defer()
            payload = await self.data.render(page, interaction)
''',
    '''            if not interaction.response.is_done():
                await asyncio.wait_for(
                    interaction.response.defer(), timeout=COMPONENT_ACK_TIMEOUT_SECONDS
                )
            payload = await asyncio.wait_for(
                self.data.render(page, interaction), timeout=COMPONENT_TIMEOUT_SECONDS
            )
''',
    "menu component timeout boundary",
)
text = replace_once(
    text,
    '''        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=ephemeral_defer)
        await callback()
''',
    '''        if not interaction.response.is_done():
            await asyncio.wait_for(
                interaction.response.defer(ephemeral=ephemeral_defer),
                timeout=COMPONENT_ACK_TIMEOUT_SECONDS,
            )
        await asyncio.wait_for(callback(), timeout=COMPONENT_TIMEOUT_SECONDS)
''',
    "generic component timeout boundary",
)
write(path, text)


# ---------------------------------------------------------------------------
# Compact cards: status should answer "is it alive, are calls flowing, why not,
# and can Discord deliver?" without dumping every internal subsystem.
# ---------------------------------------------------------------------------
path = "src/memecoin_bot/discord/cards.py"
text = read(path)
compact_status = r'''def status_card(stats: dict[str, Any]) -> dict[str, Any]:
    provider_rows = list(stats.get("provider_status") or [])
    pipeline = stats.get("pipeline") or {}
    runtime = stats.get("runtime") or {}
    blockers = pipeline.get("top_blockers") or []
    last_decision = pipeline.get("last_decision") or {}
    last_qualified = pipeline.get("last_qualified") or {}

    unhealthy = [
        row
        for row in provider_rows
        if str(row.get("state") or "").upper()
        in {"DEGRADED", "RATE_LIMITED", "CIRCUIT_OPEN", "FAILED", "ERROR", "DOWN"}
    ]
    provider_line = (
        f"Healthy **{_value(stats.get('providers_healthy'))}/{_value(stats.get('providers_total'))}**"
    )
    if unhealthy:
        provider_line += "\n" + "\n".join(
            f"• {str(row.get('provider') or 'provider').replace('_', ' ')} — "
            f"{str(row.get('state') or 'UNKNOWN').replace('_', ' ')}"
            for row in unhealthy[:4]
        )

    blocker_lines = "\n".join(
        f"• {str(row.get('reason') or 'UNKNOWN').replace('_', ' ')} — {row.get('count', 0)}"
        for row in blockers[:4]
    ) or "No dominant active blocker"

    runtime_state = runtime.get("status") or stats.get("status") or "UNKNOWN"
    delivery_failures = int(stats.get("discord_deliveries_failed") or 0)
    system_state = "HEALTHY" if runtime_state == "HEALTHY" and delivery_failures == 0 else runtime_state

    return card(
        "GAMBIT JR • STATUS",
        f"**{system_state}** • live read-only intelligence",
        "green" if system_state == "HEALTHY" else "amber",
        [
            _field(
                "CALL PIPELINE",
                f"Developing: **{_value(stats.get('pending_evidence'))}** • "
                f"Active calls: **{_value(stats.get('active_signals'))}**\n"
                f"Last decision: **{_value(last_decision.get('tier'), 'NONE')}** • "
                f"{_value(last_decision.get('decision_reason'), 'NO DECISION')}\n"
                f"Last qualified: **{_value(last_qualified.get('tier'), 'NONE')}**",
                False,
            ),
            _field("TOP BLOCKERS", blocker_lines, False),
            _field("PROVIDERS", provider_line, False),
            _field(
                "DISCORD",
                f"Destinations: **{_value(pipeline.get('enabled_alert_destinations'), '0')}** • "
                f"Outbox: **{_value(stats.get('outbox_pending'))}** • "
                f"Failed: **{_value(stats.get('discord_deliveries_failed'))}**\n"
                f"Last error: {_value(stats.get('last_alert_error'), 'NONE')}",
                False,
            ),
            _field(
                "LIFETIME",
                f"Discovered **{_value(stats.get('tokens_discovered'))}** • "
                f"Evaluated **{_value(stats.get('tokens_evaluated'))}** • "
                f"Calls **{_value(stats.get('signals'))}**",
                False,
            ),
        ],
    )
'''
text = sub_once(
    text,
    r"def status_card\(stats: dict\[str, Any\]\) -> dict\[str, Any\]:.*?\n\n\ndef menu_card",
    compact_status + "\n\n\ndef menu_card",
    "compact status card",
)
compact_menu = r'''def menu_card() -> dict[str, Any]:
    return card(
        "GAMBIT JR • COMMAND CENTER",
        "Calls, research and system health in one place.",
        fields=[
            _field("CALLS", "`/candidates` • `/runners` • `/failed`", False),
            _field("RESEARCH", "`/scan` • `/token` • `/smartmoney` • `/compare`", False),
            _field("SYSTEM", "`/status` • `/performance` • `/server-settings`", False),
        ],
    )
'''
text = sub_once(
    text,
    r"def menu_card\(\) -> dict\[str, Any\]:.*?\n\n\ndef scan_card",
    compact_menu + "\n\n\ndef scan_card",
    "compact menu card",
)
write(path, text)


# ---------------------------------------------------------------------------
# Product policy: keep branding/humanisation but stop re-inflating the compact
# status card; trim qualified-call cards to decision-critical information.
# ---------------------------------------------------------------------------
path = "src/memecoin_bot/discord/product_policy.py"
text = read(path)
text = replace_once(
    text,
    '        if name in {"Runner potential", "Failure risk"}:\n',
    '        if name in {"Runner potential", "Failure risk", "Historical context", "Contract address"}:\n',
    "trim signal fields",
)
text = sub_once(
    text,
    r"    @functools\.wraps\(original_status_card\)\n    def reliability_status_card\(stats: dict\[str, Any\]\) -> dict\[str, Any\]:.*?\n        return apply_product_presentation\(payload\)\n",
    '''    @functools.wraps(original_status_card)
    def reliability_status_card(stats: dict[str, Any]) -> dict[str, Any]:
        # The base status card already consumes runtime/pipeline diagnostics.
        # Do not append internal audit sections back into the user-facing UI.
        return apply_product_presentation(original_status_card(stats))
''',
    "compact product status wrapper",
)
write(path, text)


# ---------------------------------------------------------------------------
# Operational diagnostics: surface the reasons active candidates are not
# qualifying. This lets status distinguish "nothing good enough" from broken
# safety/RPC evidence instead of showing a silent zero-call count.
# ---------------------------------------------------------------------------
path = "src/memecoin_bot/database/store.py"
text = read(path)
text = replace_once(
    text,
    '''        result["pipeline"] = {
            "route_counts": route_counts,
''',
    '''        top_blockers = [
            dict(row)
            for row in self.conn.execute(
                "SELECT COALESCE(NULLIF(reason,''),'UNKNOWN') AS reason,COUNT(*) AS count "
                "FROM candidates WHERE state NOT IN "
                "('REJECTED_UNSAFE','EXPIRED','SIGNALLED','QUALIFIED_SIGNAL') "
                "AND first_discovered_at>=? GROUP BY reason ORDER BY count DESC LIMIT 5",
                (stale_cutoff,),
            )
        ]
        result["pipeline"] = {
            "route_counts": route_counts,
            "top_blockers": top_blockers,
''',
    "pipeline blocker diagnostics",
)
write(path, text)


# ---------------------------------------------------------------------------
# Solana safety failover: the VPS had public Solana RPC in CIRCUIT_OPEN while
# other paid/free RPC credentials existed. Safety evidence is a route blocker,
# so ignoring those configured providers can legitimately force every candidate
# to HOLD. Prefer configured RPCs and fail over without weakening safety gates.
# ---------------------------------------------------------------------------
path = "src/memecoin_bot/config.py"
text = read(path)
text = sub_once(
    text,
    r"    def effective_solana_rpc_url\(self\) -> str:.*?\n    def effective_solana_websocket_url",
    '''    def effective_solana_rpc_url(self) -> str:
        """Choose the strongest configured Solana HTTP endpoint before public RPC."""
        public_defaults = {
            "https://api.mainnet-beta.solana.com",
            "https://api.mainnet-beta.solana.com/",
        }
        if self.solana_rpc_url not in public_defaults:
            return self.solana_rpc_url
        if self.helius_api_key:
            return f"https://mainnet.helius-rpc.com/?api-key={quote_plus(self.helius_api_key)}"
        if self.solana_tracker_rpc_url:
            return self.solana_tracker_rpc_url
        alchemy = self.effective_alchemy_rpc_url()
        if alchemy:
            return alchemy
        if self.shyft_solana_rpc_url:
            return self.shyft_solana_rpc_url
        return self.solana_rpc_url

    def effective_solana_websocket_url''',
    "RPC preference",
)
text = sub_once(
    text,
    r"    def effective_solana_websocket_url\(self\) -> str:.*?\n    def effective_alchemy_rpc_url",
    '''    def effective_solana_websocket_url(self) -> str:
        effective = self.effective_solana_rpc_url()
        if self.helius_api_key and "helius-rpc.com" in effective:
            return f"wss://mainnet.helius-rpc.com/?api-key={quote_plus(self.helius_api_key)}"
        if self.solana_tracker_rpc_url and effective == self.solana_tracker_rpc_url:
            if self.solana_tracker_wss_url:
                return self.solana_tracker_wss_url
        alchemy = self.effective_alchemy_rpc_url()
        if alchemy and effective == alchemy:
            alchemy_wss = self.effective_alchemy_websocket_url()
            if alchemy_wss:
                return alchemy_wss
        return effective.replace("https://", "wss://", 1).replace("http://", "ws://", 1)

    def effective_alchemy_rpc_url''',
    "WSS preference",
)
write(path, text)

path = "src/memecoin_bot/providers/solana_rpc.py"
text = read(path)
text = replace_once(
    text,
    '''    def __init__(self, rpc_url: str, client: ResilientJsonClient):
        self.rpc_url = rpc_url
        self.client = client
''',
    '''    def __init__(
        self,
        rpc_url: str,
        client: ResilientJsonClient,
        name: str = "solana_rpc",
    ):
        self.rpc_url = rpc_url
        self.client = client
        self.name = name
''',
    "named Solana RPC provider",
)
text += '''\n\nclass SolanaSafetyFailoverProvider:\n    """Try independent Solana RPC safety providers without relaxing evidence rules."""\n\n    name = "solana_safety_failover"\n\n    def __init__(self, providers: list[SolanaRpcProvider]):\n        if not providers:\n            raise ValueError("at least one Solana safety provider is required")\n        self.providers = providers\n\n    async def safety(self, token_address: str) -> SafetyAssessment:\n        failures: list[str] = []\n        for provider in self.providers:\n            try:\n                assessment = await provider.safety(token_address)\n            except ProviderError as error:\n                failures.append(f"{provider.name}:{type(error).__name__}")\n                continue\n            if failures:\n                assessment.warnings.append(f"RPC_FAILOVER_USED:{provider.name}")\n            return assessment\n        raise ProviderError(\n            "SOLANA_SAFETY_ALL_RPC_FAILED:" + ",".join(failures or ["UNKNOWN"])\n        )\n'''
write(path, text)

path = "src/memecoin_bot/main.py"
text = read(path)
text = replace_once(
    text,
    "from memecoin_bot.providers.solana_rpc import SolanaRpcProvider\n",
    "from memecoin_bot.providers.solana_rpc import SolanaRpcProvider, SolanaSafetyFailoverProvider\n",
    "Solana failover import",
)
old_rpc_setup = '''    primary_solana_url = settings.effective_solana_rpc_url()
    helius_is_primary = primary_solana_url != settings.solana_rpc_url
    rpc_client = ResilientJsonClient(
        "helius_solana_primary" if helius_is_primary else "solana_rpc",
        settings.provider_timeout_seconds,
        settings.provider_max_retries,
        settings.provider_circuit_failures,
        settings.provider_circuit_cooldown_seconds,
        callback,
    )
    fallback_rpc_client = ResilientJsonClient(
        "solana_public_fallback",
        settings.provider_timeout_seconds,
        settings.provider_max_retries,
        settings.provider_circuit_failures,
        settings.provider_circuit_cooldown_seconds,
        callback,
    )
'''
new_rpc_setup = '''    primary_solana_url = settings.effective_solana_rpc_url()
    if "helius-rpc.com" in primary_solana_url:
        primary_solana_name = "helius_solana_primary"
    elif settings.solana_tracker_rpc_url and primary_solana_url == settings.solana_tracker_rpc_url:
        primary_solana_name = "solana_tracker_solana_primary"
    elif settings.effective_alchemy_rpc_url() == primary_solana_url:
        primary_solana_name = "alchemy_solana_primary"
    elif settings.shyft_solana_rpc_url and primary_solana_url == settings.shyft_solana_rpc_url:
        primary_solana_name = "shyft_solana_primary"
    else:
        primary_solana_name = "solana_rpc"
    rpc_client = ResilientJsonClient(
        primary_solana_name,
        settings.provider_timeout_seconds,
        settings.provider_max_retries,
        settings.provider_circuit_failures,
        settings.provider_circuit_cooldown_seconds,
        callback,
    )
    fallback_rpc_client = ResilientJsonClient(
        "solana_public_fallback",
        settings.provider_timeout_seconds,
        settings.provider_max_retries,
        settings.provider_circuit_failures,
        settings.provider_circuit_cooldown_seconds,
        callback,
    )
'''
text = replace_once(text, old_rpc_setup, new_rpc_setup, "runtime RPC naming")
old_solana = '''    solana = SolanaRpcProvider(primary_solana_url, rpc_client)
    bsc = BscRpcProvider(settings.bsc_rpc_url, bsc_client)
    safety = ChainSafetyRouter({"solana": solana, "bsc": bsc})
'''
new_solana = '''    solana = SolanaRpcProvider(primary_solana_url, rpc_client, primary_solana_name)
    safety_providers = [solana]
    fallback_specs = [
        ("solana_tracker_safety", settings.solana_tracker_rpc_url),
        ("alchemy_solana_safety", settings.effective_alchemy_rpc_url()),
        ("shyft_solana_safety", settings.shyft_solana_rpc_url),
        ("solana_public_fallback", settings.solana_fallback_rpc_url),
    ]
    seen_rpc_urls = {primary_solana_url.rstrip("/")}
    for provider_name, provider_url in fallback_specs:
        if not provider_url or provider_url.rstrip("/") in seen_rpc_urls:
            continue
        seen_rpc_urls.add(provider_url.rstrip("/"))
        provider_client = ResilientJsonClient(
            provider_name,
            settings.provider_timeout_seconds,
            settings.provider_max_retries,
            settings.provider_circuit_failures,
            settings.provider_circuit_cooldown_seconds,
            callback,
        )
        safety_providers.append(
            SolanaRpcProvider(provider_url, provider_client, provider_name)
        )
    bsc = BscRpcProvider(settings.bsc_rpc_url, bsc_client)
    safety = ChainSafetyRouter(
        {"solana": SolanaSafetyFailoverProvider(safety_providers), "bsc": bsc}
    )
'''
text = replace_once(text, old_solana, new_solana, "Solana safety failover wiring")
text = replace_once(
    text,
    '                fallback_rpc_url=settings.solana_fallback_rpc_url if helius_is_primary else None,\n                fallback_client=fallback_rpc_client if helius_is_primary else None,\n',
    '''                fallback_rpc_url=(
                    settings.solana_fallback_rpc_url
                    if settings.solana_fallback_rpc_url.rstrip("/")
                    != primary_solana_url.rstrip("/")
                    else None
                ),
                fallback_client=(
                    fallback_rpc_client
                    if settings.solana_fallback_rpc_url.rstrip("/")
                    != primary_solana_url.rstrip("/")
                    else None
                ),
''',
    "native Pump fallback",
)
write(path, text)


# ---------------------------------------------------------------------------
# Existing tests whose contract intentionally changed: menus are now truly
# persistent/public and test-alert exercises the destination channel.
# ---------------------------------------------------------------------------
path = "tests/test_discord_command_center.py"
text = read(path)
text = replace_once(
    text,
    '''        self.channel = SimpleNamespace(id=202)
''',
    '''        self.channel = FakeChannel(202)
''',
    "FakeInteraction channel",
)
text = replace_once(
    text,
    '''class FakeInteraction:
''',
    '''class FakeChannel:
    def __init__(self, channel_id: int):
        self.id = channel_id
        self.messages: list[dict] = []

    async def send(self, **kwargs):
        self.messages.append(kwargs)
        return SimpleNamespace(id=999)


class FakeInteraction:
''',
    "FakeChannel class",
)
text = replace_once(
    text,
    '''    assert view.timeout == 900
    assert not view.is_persistent()
    assert MenuView(view.data, timeout=None).is_persistent()
''',
    '''    assert view.timeout is None
    assert view.is_persistent()
    assert MenuView(view.data, timeout=None).is_persistent()
''',
    "persistent menu test",
)
text = replace_once(
    text,
    '''async def test_menu_command_sends_actual_ephemeral_view_and_all_commands_remain_registered():
''',
    '''async def test_menu_command_sends_actual_persistent_view_and_all_commands_remain_registered():
''',
    "menu test name",
)
text = replace_once(
    text,
    '''    assert sent["view"].timeout == 900
''',
    '''    assert sent["view"].timeout is None
    assert interaction.response.messages[0]["ephemeral"] is False
''',
    "menu public persistent assertion",
)
write(path, text)

path = "tests/test_pipeline_reliability_v2.py"
text = read(path)
text = sub_once(
    text,
    r"@pytest\.mark\.asyncio\nasync def test_test_alert_card_survives_optional_audit_failure\(\) -> None:.*?\n\n\n@pytest\.mark\.asyncio\nasync def test_slow_command",
    '''@pytest.mark.asyncio
async def test_test_alert_card_survives_optional_audit_failure() -> None:
    with patch.object(FakeStore, "record_test_alert", side_effect=RuntimeError("audit locked")):
        tree, client, _store = await capture_runtime()
        channel = FakeInteraction(admin=True).channel
        client.get_channel = lambda _channel_id: channel
        interaction = FakeInteraction(admin=True)
        interaction.channel = channel
        await tree.get_command("test-alert").callback(interaction)
        payload = primary_payload(interaction)
        assert "delivered" in payload["content"].lower()
        assert len(channel.messages) == 1
        assert channel.messages[0]["embed"].title == "GAMBIT JR • TEST ALERT"


@pytest.mark.asyncio
async def test_slow_command''',
    "test-alert regression rewrite",
)
write(path, text)


# ---------------------------------------------------------------------------
# New brutal stress suite. These are deliberately repetitive: one successful
# interaction is not evidence that a busy 24/7 bot remains responsive.
# ---------------------------------------------------------------------------
stress_test = '''from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from memecoin_bot.config import Settings
from memecoin_bot.discord import bot_runtime
from memecoin_bot.discord.command_center import MENU_PAGES, MenuView
from memecoin_bot.models import SafetyAssessment, iso
from memecoin_bot.providers.base import ProviderError
from memecoin_bot.providers.solana_rpc import SolanaSafetyFailoverProvider
from tests.test_discord_command_center import (
    FakeInteraction,
    FakeStore,
    capture_runtime,
    primary_payload,
)


@pytest.mark.asyncio
async def test_100_concurrent_status_commands_ack_before_slow_db_work() -> None:
    original = FakeStore.status_stats

    def slow_status(self, started_at):
        time.sleep(0.04)
        return original(self, started_at)

    with patch.object(FakeStore, "status_stats", slow_status):
        tree, _client, _store = await capture_runtime()
        command = tree.get_command("status")
        interactions = [FakeInteraction() for _ in range(100)]
        started = time.monotonic()
        await asyncio.gather(*(command.callback(item) for item in interactions))
        elapsed = time.monotonic() - started
        assert elapsed < 3.0
        assert all(item.response.deferred_at is not None for item in interactions)
        assert all("embed" in primary_payload(item) for item in interactions)


@pytest.mark.asyncio
async def test_menu_survives_250_repeated_component_navigations() -> None:
    tree, _client, _store = await capture_runtime()
    opened = FakeInteraction()
    await tree.get_command("menu").callback(opened)
    view = primary_payload(opened)["view"]
    assert isinstance(view, MenuView)
    assert view.timeout is None
    for index in range(250):
        interaction = FakeInteraction()
        page = MENU_PAGES[index % len(MENU_PAGES)]
        await view.navigate(interaction, page, "gambit:menu:navigate")
        assert interaction.response.deferred_at is not None
        assert len(interaction.edits) == 1
        assert not interaction.followup.messages


@pytest.mark.asyncio
async def test_50_test_alerts_deliver_exactly_once_each() -> None:
    tree, client, _store = await capture_runtime()
    channel = FakeInteraction(admin=True).channel
    client.get_channel = lambda _channel_id: channel
    command = tree.get_command("test-alert")
    for _ in range(50):
        interaction = FakeInteraction(admin=True)
        interaction.channel = channel
        await command.callback(interaction)
        assert "delivered" in primary_payload(interaction)["content"].lower()
    assert len(channel.messages) == 50
    assert all(message["embed"].title == "GAMBIT JR • TEST ALERT" for message in channel.messages)


class BrokenSafety:
    def __init__(self, name: str):
        self.name = name

    async def safety(self, _token_address: str):
        raise ProviderError(f"{self.name} unavailable")


class WorkingSafety:
    name = "working"

    async def safety(self, _token_address: str):
        return SafetyAssessment(
            checked_at=iso(),
            source=self.name,
            chain="solana",
            mint_authority=None,
            freeze_authority=None,
            top10_percent=20,
        )


@pytest.mark.asyncio
async def test_solana_safety_failover_recovers_after_two_rpc_failures() -> None:
    provider = SolanaSafetyFailoverProvider(
        [BrokenSafety("one"), BrokenSafety("two"), WorkingSafety()]  # type: ignore[list-item]
    )
    result = await provider.safety("So111")
    assert result.source == "working"
    assert "RPC_FAILOVER_USED:working" in result.warnings


def test_effective_solana_rpc_prefers_configured_nonpublic_sources(monkeypatch) -> None:
    config = Settings()
    config.solana_rpc_url = "https://api.mainnet-beta.solana.com"
    config.solana_tracker_rpc_url = "https://tracker.example/rpc"
    assert config.effective_solana_rpc_url() == "https://tracker.example/rpc"

    config.helius_api_key = "helius-key"
    assert "helius-rpc.com" in config.effective_solana_rpc_url()

    config.solana_rpc_url = "https://operator.example/rpc"
    assert config.effective_solana_rpc_url() == "https://operator.example/rpc"


def test_status_card_is_compact_and_surfaces_blockers() -> None:
    from memecoin_bot.discord.cards import status_card

    payload = status_card(
        {
            "status": "DEGRADED",
            "runtime": {"status": "HEALTHY"},
            "pending_evidence": 367,
            "active_signals": 0,
            "tokens_discovered": 35155,
            "tokens_evaluated": 22975,
            "signals": 0,
            "providers_healthy": 6,
            "providers_total": 7,
            "provider_status": [
                {"provider": "solana_rpc", "state": "CIRCUIT_OPEN"},
                {"provider": "helius_curated", "state": "NOT_CONFIGURED"},
            ],
            "discord_deliveries_failed": 0,
            "outbox_pending": 0,
            "last_alert_error": None,
            "pipeline": {
                "enabled_alert_destinations": 1,
                "last_decision": {
                    "tier": "SILENT_WATCH",
                    "decision_reason": "SAFETY_DATA_UNAVAILABLE",
                },
                "last_qualified": None,
                "top_blockers": [
                    {"reason": "SAFETY_DATA_UNAVAILABLE", "count": 199},
                    {"reason": "LIQUIDITY_TOO_LOW", "count": 80},
                ],
            },
        }
    )
    fields = payload["embed"]["fields"]
    assert len(fields) == 5
    text = " ".join(str(field["value"]) for field in fields)
    assert "SAFETY DATA UNAVAILABLE" in text.upper()
    assert "CIRCUIT OPEN" in text.upper()
    assert "helius curated" not in text.lower()
'''
Path("tests/test_discord_brutal_e2e_v3.py").write_text(stress_test, encoding="utf-8")

print("discord brutal e2e v3 patch applied")

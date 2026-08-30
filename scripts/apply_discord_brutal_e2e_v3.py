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
    new, count = re.subn(pattern, replacement, text, count=1, flags=re.DOTALL)
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
    '''    async def store_call(callback: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.wait_for(
            asyncio.to_thread(callback, *args), timeout=COMMAND_DB_TIMEOUT_SECONDS
        )

    def track_command(callback: CommandCallback) -> CommandCallback:
''',
    "store_call helper",
)
text = replace_once(
    text,
    '                    session = InteractionResponder(interaction, name, visibility, log)\n',
    '                    session = InteractionResponder(interaction, name, visibility, log)\n',
    "session construction anchor",
)
text = replace_once(
    text,
    '            stats = store.status_stats(service.started_at)\n            stats["v14"] = store.v14_health()\n',
    '''            stats = await store_call(store.status_stats, service.started_at)
            stats["v14"] = await store_call(store.v14_health)
''',
    "nonblocking status",
)
text = replace_once(
    text,
    '            payload = await menu_data.render("home", interaction)\n            await send_card(interaction, payload, True, MenuView(menu_data, log, timeout=900))\n',
    '''            payload = await menu_data.render("home", interaction)
            await send_card(interaction, payload, False, MenuView(menu_data, log, timeout=None))
''',
    "persistent public menu",
)
text = replace_once(
    text,
    '''        report = store.performance(
            settings.scoring_version, since, settings.major_missed_runner_multiple
        )
        report["right_tail"] = store.right_tail_performance(settings.min_sample_for_edge_metrics)
        report["v15"] = store.v15_performance(settings.min_sample_for_edge_metrics)
''',
    '''        report = await store_call(
            store.performance,
            settings.scoring_version,
            since,
            settings.major_missed_runner_multiple,
        )
        report["right_tail"] = await store_call(
            store.right_tail_performance, settings.min_sample_for_edge_metrics
        )
        report["v15"] = await store_call(
            store.v15_performance, settings.min_sample_for_edge_metrics
        )
''',
    "nonblocking performance",
)
text = replace_once(
    text,
    '                store.candidates_report(10),\n',
    '                await store_call(store.candidates_report, 10),\n',
    "nonblocking candidates",
)
text = replace_once(
    text,
    '        report = store.rejection_report((datetime.now(UTC) - timedelta(hours=24)).isoformat())\n',
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
    '        data = store.token_intelligence(address)\n',
    '        data = await store_call(store.token_intelligence, address)\n',
    "nonblocking token intelligence",
)
text = replace_once(
    text,
    '                store.radar_board(10),\n',
    '                await store_call(store.radar_board, 10),\n',
    "nonblocking radar",
)
text = replace_once(
    text,
    '        rows = [r for r in store.radar_board(100) if (r.get("max_multiple") or 0) >= 2]\n',
    '''        rows = [
            r
            for r in await store_call(store.radar_board, 100)
            if (r.get("max_multiple") or 0) >= 2
        ]
''',
    "nonblocking runners",
)
text = replace_once(
    text,
    '        rows = [r for r in store.radar_board(100) if r.get("signal_status") == "FAILED"]\n',
    '''        rows = [
            r
            for r in await store_call(store.radar_board, 100)
            if r.get("signal_status") == "FAILED"
        ]
''',
    "nonblocking failed",
)
text = replace_once(
    text,
    '            await send_card(interaction, wallet_card(store.wallet_report(address.strip())), True)\n',
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
    '                    store.cluster_report(10),\n',
    '                    await store_call(store.cluster_report, 10),\n',
    "nonblocking clusters",
)
text = replace_once(
    text,
    '                creator_card(store.creator_report(address.strip()), address.strip()),\n',
    '''                creator_card(
                    await store_call(store.creator_report, address.strip()), address.strip()
                ),
''',
    "nonblocking creator",
)
text = replace_once(
    text,
    '                narrative_card(store.narrative_report(query or None), query or None),\n',
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
'''
new_test_alert = '''    async def test_alert_command(interaction: discord.Interaction) -> None:
        if not await require_guild(interaction):
            return
        permissions = getattr(interaction.user, "guild_permissions", None)
        if not permissions or not permissions.manage_guild:
            await send_text(interaction, "Manage Server permission is required.")
            return
        current = await store_call(store.guild_settings, interaction.guild_id)
        destination_id = int(current.get("alert_channel_id") or interaction.channel_id)
        destination = client.get_channel(destination_id)
        if destination is None:
            try:
                destination = await client.fetch_channel(destination_id)
            except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                destination = None
        if destination is None or not hasattr(destination, "send"):
            await send_text(
                interaction,
                "The configured alert channel is unavailable. Run /setup again.",
            )
            return
        payload = test_alert_card()
        embed = validate_message(card_payload=payload)
        delivered = await destination.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        remote_id = str(delivered.id)
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
'''
text = replace_once(text, old_test_alert, new_test_alert, "real test-alert transport")
text = replace_once(
    text,
    '                channel_id=interaction.channel_id,\n                result="audit_failure_card_succeeded",\n',
    '                channel_id=destination_id,\n                result="audit_failure_card_succeeded",\n',
    "test alert audit channel",
)
text = replace_once(
    text,
    '''            )

    @tree.error
''',
    '''            )
        await send_text(interaction, f"Test alert delivered to <#{destination_id}>.")

    @tree.error
''',
    "test alert success response",
)
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
                f"Destinations: **{pipeline.get('enabled_alert_destinations', 0)}** • "
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
    r"def status_card\(stats: dict\[str, Any\]\) -> dict\[str, Any\]:.*?\n\ndef menu_card",
    compact_status + "def menu_card",
    "compact status card",
)
text = replace_once(
    text,
    '''    return card(
        "GAMBIT JR • TEST ALERT",
        "TEST / NON-LIVE — Discord delivery and rich-card rendering succeeded.",
        "amber",
        [
            _field("Creates signal", "NO"),
            _field("Creates Radar", "NO"),
            _field("Trading", "DISABLED"),
        ],
        "TEST EVENT • NOT MARKET INTELLIGENCE • NO TRADE EXECUTED",
    )
''',
    '''    return card(
        "GAMBIT JR • TEST ALERT",
        "Non-live transport test. This proves the configured channel can receive Gambit cards.",
        "amber",
        [
            _field("Delivery", "SUCCESS"),
            _field("Market call", "NO"),
            _field("Execution", "DISABLED"),
        ],
        "GAMBIT JR • Made by Jay • TEST ONLY • NO EXECUTION",
    )
''',
    "compact test alert card",
)
write(path, text)


# The old product-policy status wrapper would append two large internal fields to
# the newly compact card. Remove that wrapper while preserving branding/taxonomy.
path = "src/memecoin_bot/discord/product_policy.py"
text = read(path)
text = sub_once(
    text,
    r"    original_status_card = cards\.status_card.*?    command_center\.status_card = reliability_status_card\n\n",
    "",
    "remove legacy status expansion",
)
write(path, text)


# ---------------------------------------------------------------------------
# Pipeline diagnostics: show why 35k discoveries can still yield zero calls.
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
# persistent/public and test-alert must prove real channel delivery.
# ---------------------------------------------------------------------------
path = "tests/test_discord_command_center.py"
text = read(path)
text = replace_once(
    text,
    '''        self.channel = SimpleNamespace(id=202)
''',
    '''        self.channel = SimpleNamespace(id=202, messages=[])

        async def send(*, embed=None, allowed_mentions=None):
            self.channel.messages.append(
                {"embed": embed, "allowed_mentions": allowed_mentions}
            )
            return SimpleNamespace(id=99)

        self.channel.send = send
''',
    "fake channel send",
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
        assert interaction.followup.messages == []


@pytest.mark.asyncio
async def test_test_alert_delivers_exactly_once_across_50_invocations() -> None:
    tree, client, store = await capture_runtime()
    channel = FakeInteraction(admin=True).channel
    client.get_channel = lambda _channel_id: channel
    command = tree.get_command("test-alert")
    for _ in range(50):
        interaction = FakeInteraction(admin=True)
        interaction.channel = channel
        await command.callback(interaction)
        result = primary_payload(interaction)
        assert "delivered" in result["content"].lower()
    assert len(channel.messages) == 50
    assert store.calls.count("record_test_alert") == 50


class BrokenSafetyProvider:
    def __init__(self, name: str):
        self.name = name

    async def safety(self, _token_address: str):
        raise ProviderError(f"{self.name} unavailable")


class HealthySafetyProvider:
    name = "healthy_safety"

    async def safety(self, _token_address: str):
        return SafetyAssessment(checked_at=iso(), source=self.name, chain="solana")


@pytest.mark.asyncio
async def test_solana_safety_fails_over_after_two_provider_failures() -> None:
    provider = SolanaSafetyFailoverProvider(
        [
            BrokenSafetyProvider("primary"),
            BrokenSafetyProvider("secondary"),
            HealthySafetyProvider(),
        ]
    )
    result = await provider.safety("So111")
    assert result.source == "healthy_safety"
    assert "RPC_FAILOVER_USED:healthy_safety" in result.warnings


def test_rpc_preference_uses_configured_credentials_before_public_rpc() -> None:
    configured = Settings(
        helius_api_key="helius-key",
        solana_tracker_rpc_url="https://rpc.solanatracker.io/example",
        alchemy_api_key="alchemy-key",
        shyft_solana_rpc_url="https://rpc.shyft.example",
    )
    assert "helius-rpc.com" in configured.effective_solana_rpc_url()

    tracker = Settings(solana_tracker_rpc_url="https://rpc.solanatracker.io/example")
    assert tracker.effective_solana_rpc_url() == "https://rpc.solanatracker.io/example"


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
    rendered = str(payload).lower()
    assert "safety data unavailable" in rendered
    assert "solana rpc" in rendered
    assert "helius curated" not in rendered
'''
write("tests/test_discord_brutal_e2e_v3.py", stress_test)

print("discord brutal e2e v3 patch applied")

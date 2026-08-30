from __future__ import annotations

import argparse
import asyncio
import json
import signal
from pathlib import Path

from memecoin_bot.analytics import format_performance, format_status
from memecoin_bot.config import Settings
from memecoin_bot.database import Store
from memecoin_bot.discord import DiscordNotifier, NullNotifier
from memecoin_bot.discovery import DiscoveryPoller
from memecoin_bot.health import start_health_server
from memecoin_bot.historical import ApprovedFeatureStore, HistoricalContextReader
from memecoin_bot.observability.logging import configure_logging
from memecoin_bot.providers.base import ResilientJsonClient
from memecoin_bot.providers.bsc_rpc import BscRpcProvider, ChainSafetyRouter
from memecoin_bot.providers.dexscreener import DexScreenerProvider
from memecoin_bot.providers.geckoterminal import GeckoTerminalDiscoveryProvider
from memecoin_bot.providers.gmgn import GmgnProvider
from memecoin_bot.providers.launch_events import EvmFactoryLaunchSource, SolanaProgramLaunchSource
from memecoin_bot.providers.solana_rpc import SolanaRpcProvider, SolanaSafetyFailoverProvider
from memecoin_bot.radar_board import start_radar_board
from memecoin_bot.realtime.providers import (
    EvmFactoryRealtimeSource,
    HeliusCuratedSource,
    NativePumpFunSource,
    PumpCurveAccountSource,
    PumpPortalSource,
)
from memecoin_bot.replay import ReplayRunner
from memecoin_bot.service import IntelligenceService
from memecoin_bot.social import BlueskyJetstreamSocialSource, TelegramAuthorizedSocialSource


def build(settings: Settings) -> tuple[Store, IntelligenceService]:
    settings.validate()
    store = Store(settings.database_path)
    store.migrate()
    store.reconcile_stale_candidates(settings.candidate_max_age_minutes)
    store.reconcile_v14_state()
    store.register_scoring_version(
        settings.scoring_version,
        settings.weights,
        {
            "watch": settings.watch_threshold,
            "strong": settings.strong_threshold,
            "high_conviction": settings.high_conviction_threshold,
            "min_confidence": settings.min_confidence_for_signal,
        },
    )
    store.register_config_fingerprint(
        settings.config_fingerprint(),
        settings.software_version,
        settings.scoring_version,
        settings.radar_version,
        {
            "weights": settings.weights,
            "thresholds": {
                "watch": settings.watch_threshold,
                "strong": settings.strong_threshold,
                "high": settings.high_conviction_threshold,
                "confidence": settings.min_confidence_for_signal,
            },
        },
    )
    callback = store.set_provider_health
    dex_client = ResilientJsonClient(
        "dexscreener",
        settings.provider_timeout_seconds,
        settings.provider_max_retries,
        settings.provider_circuit_failures,
        settings.provider_circuit_cooldown_seconds,
        callback,
    )
    primary_solana_url = settings.effective_solana_rpc_url()
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
    bsc_client = ResilientJsonClient(
        "bsc_rpc",
        settings.provider_timeout_seconds,
        settings.provider_max_retries,
        settings.provider_circuit_failures,
        settings.provider_circuit_cooldown_seconds,
        callback,
    )
    gecko_solana_client = ResilientJsonClient(
        "geckoterminal_solana_new_pools",
        settings.provider_timeout_seconds,
        settings.provider_max_retries,
        settings.provider_circuit_failures,
        settings.provider_circuit_cooldown_seconds,
        callback,
    )
    gecko_bsc_client = ResilientJsonClient(
        "geckoterminal_bsc_new_pools",
        settings.provider_timeout_seconds,
        settings.provider_max_retries,
        settings.provider_circuit_failures,
        settings.provider_circuit_cooldown_seconds,
        callback,
    )
    market = DexScreenerProvider(settings.dexscreener_base_url, dex_client)
    solana = SolanaRpcProvider(primary_solana_url, rpc_client, primary_solana_name)
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
    discovery = DiscoveryPoller(
        [
            GeckoTerminalDiscoveryProvider(
                settings.geckoterminal_base_url, gecko_solana_client, "solana"
            ),
            GeckoTerminalDiscoveryProvider(
                settings.geckoterminal_base_url, gecko_bsc_client, "bsc"
            ),
            market,
        ]
    )
    gmgn = None
    if settings.gmgn_enabled:
        gmgn_client = ResilientJsonClient(
            "gmgn",
            settings.gmgn_timeout_seconds,
            settings.gmgn_max_retries,
            settings.gmgn_circuit_failures,
            settings.gmgn_circuit_cooldown_seconds,
            callback,
        )
        gmgn = GmgnProvider(
            settings.gmgn_base_url,
            settings.gmgn_api_key or "",
            gmgn_client,
            settings.gmgn_cache_ttl_seconds,
            settings.gmgn_concurrency,
        )
    else:
        store.set_provider_health("gmgn", False, 0, "GMGN_DISABLED", "DISABLED")
    launch_sources = []
    if (
        not settings.realtime_fabric_enabled
        and settings.direct_launch_discovery_enabled
        and settings.pumpfun_discovery_enabled
    ):
        pump_client = ResilientJsonClient(
            "solana_direct_launch",
            settings.provider_timeout_seconds,
            settings.provider_max_retries,
            settings.provider_circuit_failures,
            settings.provider_circuit_cooldown_seconds,
            callback,
        )
        launch_sources.append(
            SolanaProgramLaunchSource(
                primary_solana_url,
                settings.pumpfun_program_ids,
                pump_client,
                reconnect_seconds=settings.launch_source_reconnect_seconds,
            )
        )
    elif not settings.realtime_fabric_enabled:
        store.set_provider_health(
            "solana_direct_launch", False, 0, "DIRECT_LAUNCH_DISABLED", "DISABLED"
        )
    bnb_realtime_poller = None
    if settings.direct_launch_discovery_enabled and settings.bnb_launch_discovery_enabled:
        bnb_launch_client = ResilientJsonClient(
            "bsc_direct_launch",
            settings.provider_timeout_seconds,
            settings.provider_max_retries,
            settings.provider_circuit_failures,
            settings.provider_circuit_cooldown_seconds,
            callback,
        )
        bnb_realtime_poller = EvmFactoryLaunchSource(
            settings.bsc_rpc_url,
            settings.bnb_launch_factory_addresses,
            settings.bnb_launch_event_topics,
            bnb_launch_client,
            token_topic_index=settings.bnb_launch_token_topic_index,
            token_data_word_index=settings.bnb_launch_token_data_word_index,
            creator_data_word_index=settings.bnb_launch_creator_data_word_index,
            load_cursor=store.launch_cursor,
            save_cursor=store.save_launch_cursor,
            poll_seconds=settings.launch_source_reconnect_seconds,
        )
        if not settings.realtime_fabric_enabled:
            launch_sources.append(bnb_realtime_poller)
    else:
        store.set_provider_health(
            "bsc_direct_launch", False, 0, "DIRECT_LAUNCH_DISABLED", "DISABLED"
        )
    alert_transport_enabled = (
        settings.public_alerts_enabled or settings.operator_shadow_alerts_enabled
    )
    if not alert_transport_enabled:
        notifier = NullNotifier()
    elif settings.discord_webhook_url or settings.discord_token:
        notifier = DiscordNotifier(
            settings.discord_token,
            settings.discord_channel_id or settings.discord_channel_ids[0],
            settings.discord_webhook_url,
            settings.provider_timeout_seconds,
        )
    else:
        raise ValueError("Discord credentials required when alerts are enabled")
    historical_context = None
    if settings.historical_live_context_enabled:
        historical_context = HistoricalContextReader(
            ApprovedFeatureStore(settings.approved_feature_store_path),
            settings.historical_live_latency_budget_ms,
        )
    realtime_sources = []
    if settings.realtime_fabric_enabled and bnb_realtime_poller is not None:
        realtime_sources.append(
            EvmFactoryRealtimeSource(
                bnb_realtime_poller,
                silence_seconds=settings.realtime_silence_seconds,
            )
        )
    if settings.realtime_fabric_enabled and settings.pumpfun_native_enabled:
        realtime_sources.append(
            NativePumpFunSource(
                primary_solana_url,
                rpc_client,
                program_id=settings.pumpfun_program_ids[0],
                reconnect_seconds=settings.launch_source_reconnect_seconds,
                silence_seconds=settings.realtime_silence_seconds,
                backfill_limit=settings.realtime_backfill_limit,
                backfill_max_pages=settings.realtime_backfill_max_pages,
                fallback_rpc_url=(
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
                enrichment_queue_max=settings.realtime_enrichment_queue_max,
                enrichment_concurrency=settings.realtime_enrichment_concurrency,
                load_cursor=store.launch_cursor,
                save_cursor=store.save_launch_cursor,
            )
        )
        if settings.pumpfun_curve_monitor_enabled:
            realtime_sources.append(
                PumpCurveAccountSource(
                    primary_solana_url,
                    rpc_client,
                    store.realtime_curve_targets,
                    silence_seconds=settings.realtime_silence_seconds,
                )
            )
    else:
        store.set_provider_health(
            "solana_pumpfun_native", False, 0, "REALTIME_NATIVE_DISABLED", "DISABLED"
        )
    if settings.realtime_fabric_enabled and settings.pumpportal_api_key:
        realtime_sources.append(
            PumpPortalSource(
                settings.pumpportal_api_key,
                settings.pumpportal_websocket_url,
                settings.launch_source_reconnect_seconds,
                settings.realtime_silence_seconds,
            )
        )
    else:
        store.set_provider_health(
            "pumpportal_redundancy",
            False,
            0,
            "OPTIONAL_REDUNDANCY_UNAVAILABLE",
            "OPTIONAL_REDUNDANCY_UNAVAILABLE",
        )
    if (
        settings.realtime_fabric_enabled
        and settings.helius_api_key
        and settings.helius_curated_accounts
    ):
        realtime_sources.append(
            HeliusCuratedSource(settings.helius_api_key, settings.helius_curated_accounts)
        )
    else:
        store.set_provider_health(
            "helius_curated",
            bool(settings.helius_api_key),
            0,
            (
                "CURATED_ACCOUNT_WATCHLIST_EMPTY"
                if settings.helius_api_key
                else "HELIUS_API_KEY_NOT_CONFIGURED"
            ),
            ("OPTIONAL_WATCHLIST_EMPTY" if settings.helius_api_key else "NOT_CONFIGURED"),
        )

    def known_token(chain: str, address: str) -> bool:
        return store.token_id(address, chain) is not None

    if settings.realtime_fabric_enabled and settings.bluesky_social_enabled:
        realtime_sources.append(
            BlueskyJetstreamSocialSource(
                known_token, silence_seconds=settings.realtime_silence_seconds
            )
        )
    else:
        store.set_provider_health(
            "bluesky_jetstream_social",
            False,
            0,
            "BLUESKY_SOCIAL_DISABLED",
            "DISABLED",
        )
    if settings.realtime_fabric_enabled and settings.telegram_social_enabled:
        assert settings.telegram_api_id is not None
        assert settings.telegram_api_hash is not None
        assert settings.telegram_session is not None
        realtime_sources.append(
            TelegramAuthorizedSocialSource(
                settings.telegram_api_id,
                settings.telegram_api_hash,
                settings.telegram_session,
                settings.telegram_channels,
                known_token,
            )
        )
    else:
        store.set_provider_health(
            "telegram_authorized_social",
            False,
            0,
            "TELEGRAM_SOCIAL_DISABLED",
            "DISABLED",
        )
    service = IntelligenceService(
        settings,
        store,
        discovery,
        market,
        safety,
        notifier,
        gmgn,
        launch_sources,
        historical_context,
        realtime_sources,
    )
    return store, service


async def async_main(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    if args.command == "replay":
        if args.database:
            settings.database_path = Path(args.database)
        settings.validate()
        store = Store(settings.database_path)
        store.migrate()
        store.register_scoring_version(settings.scoring_version, settings.weights, {})
        try:
            report = await ReplayRunner(settings, store).run(args.fixture)
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
            print(json.dumps(report, indent=2, default=str))
        finally:
            store.close()
        return 0

    store, service = build(settings)
    try:
        if args.command == "scan":
            result = await service.manual_scan(args.address, args.chain)
            print(json.dumps(result, indent=2, default=str))
            return 0
        if args.command == "once":
            results = await service.scan_once()
            sent = await service.flush_outbox()
            evidence = {
                "evidence_type": "LIVE_SHADOW_ATTEMPT",
                "results": results,
                "outbox_sent_or_suppressed": sent,
                "status": store.status_stats(service.started_at),
                "providers": [dict(x) for x in store.conn.execute("SELECT * FROM provider_health")],
                "evaluations": [
                    dict(x)
                    for x in store.conn.execute(
                        "SELECT e.*,t.token_address FROM evaluations e JOIN tokens t ON t.id=e.token_id "
                        "ORDER BY e.id DESC LIMIT 50"
                    )
                ],
                "limitations": [
                    "Discord delivery only occurs when configured and a real candidate qualifies",
                    "Developer history, social velocity, bundlers and holder count remain unknown without providers",
                ],
            }
            if args.output:
                output = Path(args.output)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
            print(json.dumps(evidence, indent=2, default=str))
            return 0
        if args.command == "status":
            print(format_status(store.status_stats(service.started_at)))
            return 0
        if args.command == "performance":
            print(
                format_performance(
                    store.performance(
                        settings.scoring_version,
                        major_multiple=settings.major_missed_runner_multiple,
                    )
                )
            )
            return 0
        if args.command == "realtime-research":
            report = service.learning_lab.run_store()
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
            print(json.dumps(report, indent=2, default=str))
            return 0
        server = start_health_server(
            settings.health_port, lambda: store.status_stats(service.started_at)
        )
        board = (
            start_radar_board(settings.radar_board_port, store, service.started_at)
            if settings.radar_board_enabled
            else None
        )
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, service.stop)
            except (NotImplementedError, RuntimeError):
                pass
        if settings.discord_token:
            from memecoin_bot.discord.bot_runtime import run_discord_bot

            await run_discord_bot(service, store, settings)
        else:
            await service.run()
        server.shutdown()
        if board:
            board.shutdown()
        return 0
    finally:
        service.close()
        store.close()


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Read-only Solana and BNB memecoin intelligence service"
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Run the 24/7 scanner and tracker")
    once = sub.add_parser("once", help="Perform one real shadow discovery/evaluation cycle")
    once.add_argument("--output", default="evidence/live-shadow.json")
    replay = sub.add_parser("replay", help="Run deterministic lifecycle simulation")
    replay.add_argument("--fixture", default="fixtures/replay_lifecycle.json")
    replay.add_argument("--database", default="data/replay.db")
    replay.add_argument("--output", default="evidence/replay.json")
    manual_scan = sub.add_parser("scan", help="Run one isolated read-only manual token scan")
    manual_scan.add_argument("address")
    manual_scan.add_argument("--chain", choices=("solana", "bsc"), default="solana")
    sub.add_parser("status")
    sub.add_parser("performance")
    research = sub.add_parser(
        "realtime-research",
        help="Run the human-gated realtime challenger lab from matured operational evidence",
    )
    research.add_argument("--output", default="outputs/realtime-challenger.json")
    return p


def main() -> None:
    try:
        code = asyncio.run(async_main(parser().parse_args()))
    except KeyboardInterrupt:
        code = 0
    raise SystemExit(code)


if __name__ == "__main__":
    main()

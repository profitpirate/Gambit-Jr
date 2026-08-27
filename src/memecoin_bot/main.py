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
from memecoin_bot.observability.logging import configure_logging
from memecoin_bot.providers.base import ResilientJsonClient
from memecoin_bot.providers.bsc_rpc import BscRpcProvider, ChainSafetyRouter
from memecoin_bot.providers.dexscreener import DexScreenerProvider
from memecoin_bot.providers.geckoterminal import GeckoTerminalDiscoveryProvider
from memecoin_bot.providers.gmgn import GmgnProvider
from memecoin_bot.providers.launch_events import EvmFactoryLaunchSource, SolanaProgramLaunchSource
from memecoin_bot.providers.solana_rpc import SolanaRpcProvider
from memecoin_bot.radar_board import start_radar_board
from memecoin_bot.replay import ReplayRunner
from memecoin_bot.service import IntelligenceService


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
    rpc_client = ResilientJsonClient(
        "solana_rpc",
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
    solana = SolanaRpcProvider(settings.solana_rpc_url, rpc_client)
    bsc = BscRpcProvider(settings.bsc_rpc_url, bsc_client)
    safety = ChainSafetyRouter({"solana": solana, "bsc": bsc})
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
    if settings.direct_launch_discovery_enabled and settings.pumpfun_discovery_enabled:
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
                settings.solana_rpc_url,
                settings.pumpfun_program_ids,
                pump_client,
                reconnect_seconds=settings.launch_source_reconnect_seconds,
            )
        )
    else:
        store.set_provider_health(
            "solana_direct_launch", False, 0, "DIRECT_LAUNCH_DISABLED", "DISABLED"
        )
    if settings.direct_launch_discovery_enabled and settings.bnb_launch_discovery_enabled:
        bnb_launch_client = ResilientJsonClient(
            "bsc_direct_launch",
            settings.provider_timeout_seconds,
            settings.provider_max_retries,
            settings.provider_circuit_failures,
            settings.provider_circuit_cooldown_seconds,
            callback,
        )
        launch_sources.append(
            EvmFactoryLaunchSource(
                settings.bsc_rpc_url,
                settings.bnb_launch_factory_addresses,
                settings.bnb_launch_event_topics,
                bnb_launch_client,
                token_topic_index=settings.bnb_launch_token_topic_index,
                token_data_word_index=settings.bnb_launch_token_data_word_index,
                creator_data_word_index=settings.bnb_launch_creator_data_word_index,
                poll_seconds=settings.launch_source_reconnect_seconds,
            )
        )
    else:
        store.set_provider_health(
            "bsc_direct_launch", False, 0, "DIRECT_LAUNCH_DISABLED", "DISABLED"
        )
    if settings.shadow_mode and not settings.shadow_send_alerts:
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
    service = IntelligenceService(
        settings,
        store,
        discovery,
        market,
        safety,
        notifier,
        gmgn,
        launch_sources,
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
    return p


def main() -> None:
    try:
        code = asyncio.run(async_main(parser().parse_args()))
    except KeyboardInterrupt:
        code = 0
    raise SystemExit(code)


if __name__ == "__main__":
    main()

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from typing import Any

from memecoin_bot.config import Settings
from memecoin_bot.database import Store
from memecoin_bot.developers import DeveloperEngine
from memecoin_bot.discovery import DiscoveryPoller
from memecoin_bot.models import DeveloperClass, DiscoveryEvent, SafetyAssessment, SignalClass, iso
from memecoin_bot.momentum import MomentumEngine
from memecoin_bot.narratives import NarrativeEngine
from memecoin_bot.observability.logging import event as log_event
from memecoin_bot.onchain import OnchainEngine
from memecoin_bot.providers.base import ProviderError
from memecoin_bot.providers.dexscreener import DexScreenerProvider
from memecoin_bot.providers.solana_rpc import SolanaRpcProvider
from memecoin_bot.safety import SafetyGates
from memecoin_bot.scoring import ScoringEngine
from memecoin_bot.signals import format_event, signal_payload
from memecoin_bot.social import SocialEngine
from memecoin_bot.tracking import SignalTracker


class IntelligenceService:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        discovery: DiscoveryPoller,
        market: DexScreenerProvider,
        solana: SolanaRpcProvider,
        notifier: Any,
    ):
        self.settings = settings
        self.store = store
        self.discovery = discovery
        self.market = market
        self.solana = solana
        self.notifier = notifier
        self.safety_gates = SafetyGates(settings)
        self.scoring = ScoringEngine(settings)
        self.developers = DeveloperEngine()
        self.narratives = NarrativeEngine()
        self.social = SocialEngine()
        self.onchain = OnchainEngine()
        self.momentum = MomentumEngine()
        self.tracker = SignalTracker(store, market, settings)
        self.started_at = iso()
        self.log = logging.getLogger("memecoin_bot.service")
        self.stop_event = asyncio.Event()

    async def evaluate(self, discovery: DiscoveryEvent) -> str:
        token_id, created = self.store.upsert_discovery(discovery)
        if not created and self.store.has_evaluation(token_id):
            return "DUPLICATE"
        log_event(self.log, logging.INFO, "token_discovered", token=discovery.token_address,
                  source=discovery.source)
        try:
            market = await self.market.market_snapshot(discovery.token_address)
        except ProviderError as exc:
            log_event(self.log, logging.WARNING, "market_provider_failure",
                      token=discovery.token_address, error=str(exc))
            return "MARKET_UNAVAILABLE"
        if market is None:
            return "NO_PAIR"
        discovery.symbol = market.symbol
        discovery.name = market.name
        discovery.pair_address = market.pair_address
        self.store.upsert_discovery(discovery)
        previous_rows = self.store.recent_snapshots(token_id, 1)
        previous = json.loads(previous_rows[0]["payload_json"]) if previous_rows else None
        try:
            safety = await self.solana.safety(discovery.token_address)
            safety_unavailable = False
        except ProviderError as exc:
            safety = SafetyAssessment(
                checked_at=iso(), source=self.solana.name,
                warnings=[f"SAFETY_PROVIDER_UNAVAILABLE:{exc}"],
            )
            safety_unavailable = True
        self.store.save_snapshot(token_id, market, safety.holder_count)
        hard_rejections = self.safety_gates.evaluate(market, safety)
        if safety_unavailable:
            hard_rejections.append("SAFETY_DATA_UNAVAILABLE")

        developer = self.developers.assess(discovery.deployer)
        narrative = self.narratives.assess(discovery, market)
        social = self.social.assess(market, previous)
        onchain = self.onchain.assess(safety)
        momentum = self.momentum.assess(market, previous)
        safety_score = None if safety_unavailable else (
            5.0 if not safety.mint_authority and not safety.freeze_authority else 0.0
        )
        components = {
            "narrative": narrative.get("score"),
            "social": social.get("score"),
            "onchain": onchain.get("score"),
            "developer": developer.get("score"),
            "momentum": momentum.get("score"),
            "safety": safety_score,
        }
        score = self.scoring.score(components, sorted(set(hard_rejections)))
        intelligence = {
            "developer": developer, "narrative": narrative, "social": social,
            "onchain": onchain, "momentum": momentum,
            "risks": list(safety.warnings) + (["Very young token"] if market.pair_created_at else []),
            "thesis": [
                x for x in [
                    f"{narrative.get('label')} metadata fit" if narrative.get("label") else None,
                    "buying and volume acceleration observed" if momentum.get("score") else None,
                    "distribution measured on Solana RPC" if safety.top10_percent is not None else None,
                ] if x
            ],
        }
        evidence = {
            "market": market.to_dict(), "safety": asdict(safety),
            "intelligence": intelligence,
            "unknown_fields": [
                name for name, value in {
                    "holder_count": safety.holder_count,
                    "bundled_percent": safety.bundled_percent,
                    "deployer_percent": safety.deployer_percent,
                    "developer_score": developer.get("score"),
                    "social_velocity": social.get("score"),
                }.items() if value is None
            ],
        }
        self.store.save_evaluation(token_id, score, evidence)
        log_event(self.log, logging.INFO, "evaluation_decision", token=discovery.token_address,
                  classification=score.classification, score=score.total,
                  confidence=score.confidence, rejections=score.hard_rejections)
        if score.classification not in {
            SignalClass.WATCH, SignalClass.STRONG, SignalClass.HIGH_CONVICTION
        }:
            return str(score.classification)
        payload = signal_payload(
            discovery, market, safety, intelligence, score,
            self.settings.shadow_mode,
        )
        signal_id = self.store.create_signal(
            token_id, market, score,
            {"developer": developer, "narrative": narrative,
             "social": social, "onchain": onchain},
            intelligence["risks"], payload, safety.holder_count,
        )
        if signal_id:
            log_event(self.log, logging.INFO, "signal_persisted", token=discovery.token_address,
                      signal_id=signal_id, score=score.total)
        return str(score.classification)

    async def scan_once(self) -> dict[str, int]:
        results: dict[str, int] = {}
        try:
            discoveries = await self.discovery.poll()
        except ProviderError as exc:
            log_event(self.log, logging.ERROR, "discovery_failure", error=str(exc))
            return {"DISCOVERY_FAILURE": 1}
        for discovered in discoveries[:self.settings.max_discoveries_per_cycle]:
            try:
                result = await self.evaluate(discovered)
            except Exception:
                self.log.exception("token evaluation failed", extra={"fields": {"token": discovered.token_address}})
                result = "ERROR"
            results[result] = results.get(result, 0) + 1
        return results

    async def flush_outbox(self) -> int:
        sent = 0
        for row in self.store.pending_outbox():
            try:
                payload = json.loads(row["payload_json"])
                content = format_event(row["event_type"], payload)
                remote_id = await self.notifier.send(content)
                self.store.mark_outbox_sent(int(row["id"]), remote_id)
                sent += 1
            except Exception as exc:
                self.store.mark_outbox_error(int(row["id"]), str(exc))
                log_event(self.log, logging.ERROR, "discord_failure", outbox_id=row["id"], error=str(exc))
                break
        return sent

    async def run(self) -> None:
        log_event(self.log, logging.INFO, "restart_recovery", active_signals=len(self.store.active_signals()),
                  pending_outbox=len(self.store.pending_outbox()))
        async def scanner() -> None:
            while not self.stop_event.is_set():
                await self.scan_once()
                await self.flush_outbox()
                try:
                    await asyncio.wait_for(self.stop_event.wait(), self.settings.discovery_interval_seconds)
                except asyncio.TimeoutError:
                    pass

        async def tracker() -> None:
            while not self.stop_event.is_set():
                try:
                    await self.tracker.monitor_once()
                    await self.flush_outbox()
                except Exception:
                    self.log.exception("tracking cycle failed")
                try:
                    await asyncio.wait_for(self.stop_event.wait(), self.settings.monitor_interval_seconds)
                except asyncio.TimeoutError:
                    pass

        await asyncio.gather(scanner(), tracker())

    def stop(self) -> None:
        self.stop_event.set()

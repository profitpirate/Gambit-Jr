from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from memecoin_bot.config import Settings
from memecoin_bot.database import Store
from memecoin_bot.developers import DeveloperEngine
from memecoin_bot.discovery import DiscoveryPoller
from memecoin_bot.models import CandidateState, DiscoveryEvent, MarketSnapshot, SafetyAssessment, SignalClass, iso
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
        candidate_id, candidate_created = self.store.ensure_candidate(
            token_id, discovery.discovered_at, self.settings.scoring_version
        )
        if not candidate_created:
            return "KNOWN_CANDIDATE"
        log_event(self.log, logging.INFO, "token_discovered", token=discovery.token_address,
                  source=discovery.source)
        log_event(self.log, logging.INFO, "candidate_created", token=discovery.token_address,
                  candidate_id=candidate_id, state=CandidateState.DISCOVERED)
        return await self._monitor_candidate(self.store.candidate_for_token(token_id), discovery)

    async def _monitor_candidate(self, candidate: Any, discovery: DiscoveryEvent | None = None) -> str:
        token_id = int(candidate["token_id"])
        candidate_id = int(candidate["id"])
        address = candidate["token_address"] if "token_address" in candidate.keys() else discovery.token_address
        last_seen = candidate["last_monitored_at"]
        if last_seen and (datetime.now(timezone.utc) - datetime.fromisoformat(last_seen)).total_seconds() / 60 > self.settings.candidate_inactivity_timeout_minutes:
            self.store.update_candidate(candidate_id, CandidateState.EXPIRED,
                                        "CANDIDATE_INACTIVITY_TIMEOUT", waiting_reasons=["CANDIDATE_INACTIVITY_TIMEOUT"], expired=True)
            return str(CandidateState.EXPIRED)
        try:
            market = await self.market.market_snapshot(address)
        except ProviderError as exc:
            self.store.update_candidate(candidate_id, CandidateState.FAILED_PROVIDER,
                                        "MARKET_PROVIDER_UNAVAILABLE", waiting_reasons=["MARKET_PROVIDER_UNAVAILABLE"])
            log_event(self.log, logging.WARNING, "provider_pending", token=address,
                      candidate_id=candidate_id, provider=self.market.name, error=str(exc))
            return str(CandidateState.FAILED_PROVIDER)
        if market is None:
            self.store.update_candidate(candidate_id, CandidateState.PENDING_EVIDENCE,
                                        "PAIR_NOT_AVAILABLE", waiting_reasons=["PAIR_NOT_AVAILABLE"])
            return str(CandidateState.PENDING_EVIDENCE)
        if discovery is None:
            discovery = DiscoveryEvent(token_address=address, symbol=candidate["symbol"],
                                       name=candidate["name"], pair_address=candidate["pair_address"],
                                       discovered_at=candidate["first_discovered_at"], source="candidate_monitor")
        discovery.symbol, discovery.name, discovery.pair_address = market.symbol, market.name, market.pair_address
        self.store.upsert_discovery(discovery)
        previous_rows = list(reversed(self.store.recent_snapshots(token_id, self.settings.snapshot_history_limit)))
        previous = [json.loads(row["payload_json"]) for row in previous_rows]
        try:
            safety = await self.solana.safety(address)
            safety_unavailable = False
        except ProviderError as exc:
            safety = SafetyAssessment(
                checked_at=iso(), source=self.solana.name,
                warnings=[f"SAFETY_PROVIDER_UNAVAILABLE:{exc}"],
            )
            safety_unavailable = True
        self.store.save_snapshot(token_id, market, safety.holder_count)
        hard_rejections = self.safety_gates.evaluate(market, safety)
        if hard_rejections:
            self.store.update_candidate(candidate_id, CandidateState.REJECTED_UNSAFE,
                                        hard_rejections[0], market, hard_rejections=hard_rejections)
            log_event(self.log, logging.WARNING, "candidate_rejected", token=address,
                      candidate_id=candidate_id, state=CandidateState.REJECTED_UNSAFE,
                      reason=hard_rejections)
            return str(CandidateState.REJECTED_UNSAFE)
        expiry = self.safety_gates.expiry(market, candidate["first_discovered_at"])
        if expiry:
            self.store.update_candidate(candidate_id, CandidateState.EXPIRED, expiry, market,
                                        waiting_reasons=[expiry], expired=True)
            log_event(self.log, logging.INFO, "candidate_expired", token=address,
                      candidate_id=candidate_id, reason=expiry)
            return str(CandidateState.EXPIRED)

        developer = self.developers.assess(discovery.deployer)
        narrative = self.narratives.assess(discovery, market)
        social = self.social.assess(market, previous[-1] if previous else None)
        onchain = self.onchain.assess(safety)
        momentum = self.momentum.assess_history(market, previous, self.settings.min_snapshots_for_momentum)
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
        waiting = self.safety_gates.readiness(market)
        if safety_unavailable:
            waiting.append("SAFETY_DATA_UNAVAILABLE")
        if momentum.get("score") is None:
            waiting.append(momentum.get("reason", "MOMENTUM_UNAVAILABLE"))
        if score.confidence < self.settings.min_confidence_for_signal:
            waiting.append("INSUFFICIENT_EVIDENCE_COVERAGE")
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
        signal_grade = score.classification in {SignalClass.WATCH, SignalClass.STRONG, SignalClass.HIGH_CONVICTION}
        existing_signal_id = candidate["signal_id"]
        if existing_signal_id:
            if signal_grade and not waiting:
                update = self.store.update_signal_intelligence(
                    int(existing_signal_id), str(score.classification), score, intelligence["thesis"],
                    {"symbol": market.symbol, "token_address": address,
                     "normalized_score": score.normalized_score, "confidence": score.confidence,
                     "shadow": self.settings.shadow_mode},
                )
                reason = update or "SIGNAL_MONITORING"
            else:
                deterioration = waiting or ["NORMALIZED_SCORE_BELOW_WATCH"]
                self.store.update_signal_intelligence(
                    int(existing_signal_id), "BELOW_WATCH", score, deterioration,
                    {"symbol": market.symbol, "token_address": address,
                     "normalized_score": score.normalized_score, "confidence": score.confidence,
                     "shadow": self.settings.shadow_mode},
                )
                reason = deterioration[0]
            self.store.update_candidate(candidate_id, CandidateState.SIGNALLED, reason, market, score,
                                        waiting_reasons=sorted(set(waiting)),
                                        unknown_fields=evidence["unknown_fields"], signal_id=int(existing_signal_id))
            return str(CandidateState.SIGNALLED)
        if waiting or not signal_grade:
            state = CandidateState.PENDING_EVIDENCE if waiting else CandidateState.CANDIDATE
            reason = waiting[0] if waiting else "SCORE_BELOW_WATCH"
            self.store.update_candidate(candidate_id, state, reason, market, score,
                                        waiting_reasons=sorted(set(waiting)),
                                        unknown_fields=evidence["unknown_fields"])
            log_event(self.log, logging.INFO, "candidate_snapshot", token=address,
                      candidate_id=candidate_id, state=state, reason=reason,
                      score=score.normalized_score, confidence=score.confidence,
                      market_cap=market.market_cap_usd, liquidity=market.liquidity_usd,
                      snapshot_count=len(previous) + 1)
            return str(state)
        log_event(self.log, logging.INFO, "evaluation_decision", token=address,
                  classification=score.classification, score=score.normalized_score,
                  confidence=score.confidence, rejections=score.hard_rejections)
        payload = signal_payload(
            discovery, market, safety, intelligence, score,
            self.settings.shadow_mode,
        )
        signal_id = self.store.create_signal(
            token_id, market, score,
            {"developer": developer, "narrative": narrative,
             "social": social, "onchain": onchain,
             "candidate_history": {"candidate_id": candidate_id, "snapshot_count": len(previous) + 1,
                                   "first_discovered_at": candidate["first_discovered_at"]}},
            intelligence["risks"], payload, safety.holder_count,
        )
        if signal_id:
            self.store.update_candidate(candidate_id, CandidateState.SIGNALLED,
                                        f"PROMOTED_{score.classification}", market, score,
                                        unknown_fields=evidence["unknown_fields"], signal_id=signal_id)
            log_event(self.log, logging.INFO, "candidate_promoted", token=address,
                      candidate_id=candidate_id, signal_id=signal_id,
                      state=score.classification, score=score.normalized_score,
                      confidence=score.confidence)
        return str(score.classification)

    async def monitor_candidates_once(self) -> dict[str, int]:
        results: dict[str, int] = {}
        for candidate in self.store.active_candidates(self.settings.max_active_candidates):
            try:
                result = await self._monitor_candidate(candidate)
            except Exception:
                self.log.exception("candidate monitoring failed", extra={"fields": {"candidate_id": candidate["id"]}})
                result = "ERROR"
            results[result] = results.get(result, 0) + 1
        return results

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

        async def candidate_monitor() -> None:
            while not self.stop_event.is_set():
                await self.monitor_candidates_once()
                await self.flush_outbox()
                try:
                    await asyncio.wait_for(self.stop_event.wait(), self.settings.candidate_monitor_interval_seconds)
                except asyncio.TimeoutError:
                    pass

        await asyncio.gather(scanner(), candidate_monitor(), tracker())

    def stop(self) -> None:
        self.stop_event.set()

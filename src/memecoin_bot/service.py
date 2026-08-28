from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from memecoin_bot.alpha_engine import (
    AlphaDecision,
    AlphaState,
    BoundedLaunchQueue,
    EntryState,
    LaunchEvent,
    PayoffGrade,
    SurvivalGrade,
    parallel_enrichment,
    payoff_engine,
    promotion_decision,
    survival_engine,
    t0_decision,
)
from memecoin_bot.alpha_engine import (
    entry_state as alpha_entry_state,
)
from memecoin_bot.config import Settings
from memecoin_bot.database import Store
from memecoin_bot.developers import DeveloperEngine
from memecoin_bot.discovery import DiscoveryPoller
from memecoin_bot.historical.intelligence_v3 import (
    EntryActionability,
    TimedValue,
    V3ShadowEngine,
)
from memecoin_bot.intelligence import (
    entry_quality,
    intelligence_pillar,
    narrative_context,
    priority,
    setup_quality,
    signal_convergence,
    social_presence,
    wallet_intelligence,
)
from memecoin_bot.models import (
    CandidateState,
    DiscoveryEvent,
    SafetyAssessment,
    SignalClass,
    iso,
)
from memecoin_bot.momentum import MomentumEngine
from memecoin_bot.narratives import NarrativeEngine
from memecoin_bot.observability.logging import event as log_event
from memecoin_bot.onchain import OnchainEngine
from memecoin_bot.providers.base import ProviderError
from memecoin_bot.providers.dexscreener import DexScreenerProvider
from memecoin_bot.radar import RadarEngine
from memecoin_bot.realtime import CanonicalEvent, CanonicalEventFabric, CanonicalEventType
from memecoin_bot.realtime.actors import ActorIntelligence
from memecoin_bot.realtime.features import RealtimeFeatureProjector
from memecoin_bot.realtime.learning import AdaptiveLearningLab
from memecoin_bot.safety import SafetyGates
from memecoin_bot.scoring import ScoringEngine
from memecoin_bot.signals import format_discord_event, radar_payload, signal_payload
from memecoin_bot.social import SocialEngine
from memecoin_bot.tracking import SignalTracker
from memecoin_bot.v15_engine import (
    SignalTier,
    Stage,
    buyer_trajectory,
    economic_concentration,
    evaluate_v15,
    tradeability,
)

_MARKET_UNSET = object()


def fair_chain_sample(discoveries: list[DiscoveryEvent], limit: int) -> list[DiscoveryEvent]:
    """Bound a discovery cycle without allowing the first provider/chain to starve others."""
    if limit <= 0:
        return []
    chains: dict[str, list[DiscoveryEvent]] = {}
    for discovery in discoveries:
        chains.setdefault(discovery.chain, []).append(discovery)
    selected: list[DiscoveryEvent] = []
    while len(selected) < limit:
        progressed = False
        for queue in chains.values():
            if queue and len(selected) < limit:
                selected.append(queue.pop(0))
                progressed = True
        if not progressed:
            break
    return selected


def _evidence_age_seconds(retrieved_at: str | None, observed_at: str) -> float | None:
    if not retrieved_at:
        return None
    retrieved = datetime.fromisoformat(retrieved_at)
    observed = datetime.fromisoformat(observed_at)
    if retrieved.tzinfo is None:
        retrieved = retrieved.replace(tzinfo=UTC)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    return max(0.0, (observed - retrieved).total_seconds())


def _v15_stage(discovery: DiscoveryEvent, market: Any) -> Stage:
    phase = str(discovery.metadata.get("launch_phase") or "").upper()
    if "REVIVAL" in phase:
        return Stage.REVIVAL
    if "BOND" in phase or "CURVE" in phase:
        return Stage.BONDING
    return Stage.MIGRATED if market.pair_address else Stage.NEW


def _economic_holder_rows(holders: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    rows = []
    for holder in holders or []:
        labels = holder.get("labels") or holder.get("tags") or holder.get("tag") or []
        if isinstance(labels, str):
            labels = [labels]
        label_text = " ".join(str(value).lower() for value in labels)
        rows.append(
            {
                "wallet": holder.get("wallet_address") or holder.get("address"),
                "percent": holder.get("percent")
                or holder.get("percentage")
                or holder.get("amount_percentage"),
                "cluster_id": holder.get("cluster_id")
                or holder.get("funder")
                or holder.get("funding_source"),
                "deployer_related": "dev" in label_text or "creator" in label_text,
                "excluded_non_economic": any(
                    marker in label_text for marker in ("lp", "liquidity", "burn", "locker")
                ),
            }
        )
    return rows


class IntelligenceService:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        discovery: DiscoveryPoller,
        market: DexScreenerProvider,
        safety_provider: Any,
        notifier: Any,
        gmgn: Any | None = None,
        launch_sources: list[Any] | None = None,
        historical_context: Any | None = None,
        realtime_sources: list[Any] | None = None,
    ):
        self.settings = settings
        self.store = store
        self.discovery = discovery
        self.market = market
        self.safety_provider = safety_provider
        self.notifier = notifier
        self.gmgn = gmgn
        self.launch_sources = launch_sources or []
        self.historical_context = historical_context
        self.realtime_sources = realtime_sources or []
        self.launch_queue = BoundedLaunchQueue(settings.event_queue_max)
        self.realtime_fabric = CanonicalEventFabric(store)
        self.realtime_features = RealtimeFeatureProjector(store)
        self.actor_intelligence = ActorIntelligence(store)
        self.learning_lab = AdaptiveLearningLab(store)
        self.realtime_wake = asyncio.Event()
        self.safety_gates = SafetyGates(settings)
        self.scoring = ScoringEngine(settings)
        self.developers = DeveloperEngine()
        self.narratives = NarrativeEngine()
        self.social = SocialEngine()
        self.onchain = OnchainEngine()
        self.momentum = MomentumEngine()
        self.radar = RadarEngine(settings)
        self.v3_shadow = V3ShadowEngine()
        self.tracker = SignalTracker(store, market, settings)
        self.started_at = iso()
        self.log = logging.getLogger("memecoin_bot.service")
        self.stop_event = asyncio.Event()

    def close(self) -> None:
        if self.historical_context is not None:
            self.historical_context.store.close()

    async def offer_launch_event(self, event: LaunchEvent) -> None:
        result = self.launch_queue.offer(event)
        if result != "QUEUED":
            log_event(
                self.log,
                logging.WARNING if result == "BACKPRESSURE" else logging.DEBUG,
                "launch_event_queue",
                event_key=event.event_key,
                result=result,
                queue=self.launch_queue.stats(),
            )

    async def offer_realtime_event(self, event: CanonicalEvent) -> None:
        result = self.realtime_fabric.publish(event)
        if result.is_new:
            self.realtime_wake.set()
        elif result.conflict:
            log_event(
                self.log,
                logging.WARNING,
                "canonical_event_conflict",
                event_id=result.event_id,
                source=event.source,
                event_type=str(event.event_type),
            )

    async def handle_realtime_event(self, event: CanonicalEvent) -> str:
        feature_ready = model_start = model_finish = decision_at = None
        token_id, _ = self.realtime_fabric.project(event)
        if token_id is not None:
            model_start = iso()
        if event.event_type == CanonicalEventType.TOKEN_CREATED:
            launch = LaunchEvent(
                event_key=event.event_id,
                source=event.source,
                chain=event.chain,
                token_address=event.canonical_token,
                source_event_timestamp=event.source_timestamp,
                source_received_at=event.received_timestamp,
                launchpad=event.platform,
                creator_address=event.payload.get("creator"),
                phase="CREATED",
                slot_or_block=event.slot_or_block,
                transaction_id=event.transaction_signature,
                metadata={
                    **event.payload,
                    "canonical_event_id": event.event_id,
                    "confirmation_sources": self.store.conn.execute(
                        "SELECT confirmation_sources_json FROM canonical_events WHERE event_id=?",
                        (event.event_id,),
                    ).fetchone()[0],
                },
            )
            await self.handle_launch_event(launch)
        if token_id is not None:
            feature_ready = iso()
            # Every event updates a bounded PIT trajectory snapshot. This is
            # research/shadow evidence and cannot route a public alert itself.
            feature = self.realtime_features.compute(token_id, event.available_timestamp)
            age = float(feature.get("token_age_seconds") or 0)
            actor_stage = (
                "BONDING_SNIPER"
                if age <= 30
                else "EARLY_CURVE"
                if age <= 120
                else "MID_CURVE"
                if age <= 600
                else "POST_MIGRATION_PULLBACK"
                if feature.get("migration_state") == "MIGRATED"
                else "UNKNOWN"
            )
            actor_evidence = {
                "funder": self.actor_intelligence.funder_graph(
                    token_id, event.available_timestamp
                ),
                "wallet_consensus": self.actor_intelligence.independent_consensus(
                    token_id=token_id,
                    decision_at=event.available_timestamp,
                    stage=actor_stage,
                ),
            }
            state = self.store.conn.execute(
                "SELECT creator_address,launched_at FROM token_realtime_state WHERE token_id=?",
                (token_id,),
            ).fetchone()
            if state and state["creator_address"]:
                actor_evidence["creator_prior_at_launch"] = (
                    self.actor_intelligence.creator_profile_at(
                        event.chain, str(state["creator_address"]), str(state["launched_at"])
                    )
                )
            feature["actor_intelligence"] = actor_evidence
            feature["trigger_event_id"] = event.event_id
            with self.store._lock, self.store.conn:
                self.store.conn.execute(
                    "UPDATE trajectory_feature_snapshots_v15 SET feature_json=? WHERE token_id=? "
                    "AND decision_timestamp=? AND feature_version=?",
                    (
                        json.dumps(feature, default=str, separators=(",", ":"), sort_keys=True),
                        token_id,
                        event.available_timestamp,
                        "realtime-trajectory-v1",
                    ),
                )
            model_finish = iso()
            decision_at = event.available_timestamp
        self.realtime_fabric.complete(
            event.event_id,
            feature_ready_timestamp=feature_ready,
            model_start_timestamp=model_start,
            model_finish_timestamp=model_finish,
            decision_timestamp=decision_at,
        )
        return "PROCESSED"

    async def handle_launch_event(self, event: LaunchEvent) -> str:
        launch_event_id, created = self.store.record_launch_event(event)
        if not created:
            return "DUPLICATE"
        discovery = DiscoveryEvent(
            token_address=event.token_address,
            chain=event.chain,
            source=event.source,
            discovered_at=event.source_received_at,
            estimated_creation_timestamp=event.source_event_timestamp,
            deployer=event.creator_address,
            metadata={
                **event.metadata,
                "launchpad": event.launchpad,
                "launch_event_key": event.event_key,
                "launch_phase": event.phase,
            },
        )
        token_id, _ = self.store.upsert_discovery(discovery)
        candidate_id, _ = self.store.ensure_candidate(
            token_id, event.source_received_at, self.settings.scoring_version
        )
        created_at = iso()
        self.store.link_launch_candidate(launch_event_id, candidate_id, created_at)
        received = datetime.fromisoformat(event.source_received_at)
        source_time = datetime.fromisoformat(event.source_event_timestamp)
        if received.tzinfo is None:
            received = received.replace(tzinfo=UTC)
        if source_time.tzinfo is None:
            source_time = source_time.replace(tzinfo=UTC)
        features = {
            **event.metadata,
            "launch_event_verified": True,
            "launchpad": event.launchpad,
            "creator_address": event.creator_address,
            "age_seconds": max(0, (received - source_time).total_seconds()),
        }
        decision = t0_decision(features)
        self.store.record_v14_decision(
            candidate_id,
            launch_event_id,
            decision,
            self.settings,
            observed_at=event.source_received_at,
            decided_at=iso(),
            providers={event.source: {"state": "HEALTHY", "observed_at": event.source_received_at}},
        )
        if decision.state == AlphaState.GENESIS_RADAR:
            call = {
                "call_timestamp": iso(),
                "entry_state": str(decision.entry_state),
                "confidence": decision.confidence,
                "features": decision.feature_vector,
                "providers": {event.source: {"state": "HEALTHY"}},
            }
            self.store.record_immutable_call(candidate_id, "GENESIS_RADAR", call, self.settings)
            self.store.enqueue_v14_event(
                f"v14:{candidate_id}:GENESIS_RADAR",
                "GENESIS_RADAR",
                {
                    "tier": "GENESIS_RADAR",
                    "chain": event.chain,
                    "token_address": event.token_address,
                    "launchpad": event.launchpad,
                    "confidence": decision.confidence,
                    "entry_state": str(decision.entry_state),
                    "reasons": decision.reasons,
                    "unknowns": decision.unknowns,
                    "shadow": self.settings.shadow_mode,
                },
            )
            log_event(
                self.log,
                logging.INFO,
                "genesis_triggered",
                token=event.token_address,
                candidate_id=candidate_id,
                confidence=decision.confidence,
            )
        return str(decision.state)

    async def manual_scan(
        self,
        address: str,
        chain: str = "solana",
        guild_id: Any = None,
        user_id: Any = None,
    ) -> dict[str, Any]:
        """Parallel, read-only scan that never creates a candidate, Radar call, or signal."""
        requested_at = iso()
        started = datetime.now(UTC)
        providers: dict[str, Any] = {
            "market": lambda: self.market.market_snapshot(address, chain),
            "safety": lambda: self.safety_provider.safety(chain, address),
        }
        if self.gmgn is not None:
            providers["gmgn"] = lambda: self.gmgn.enrich(chain, address)
        results = await parallel_enrichment(providers, self.settings.provider_timeout_seconds)
        market = results["market"].get("value")
        safety = results["safety"].get("value")
        features = {
            "age_seconds": None,
            "price_change_from_launch_percent": getattr(market, "price_change_5m", None),
            "market_cap_usd": getattr(market, "market_cap_usd", None),
            "liquidity_usd": getattr(market, "liquidity_usd", None),
            "terminal_safety_failure": bool(getattr(safety, "rejection_reasons", [])),
            "mint_authority_active": getattr(safety, "mint_authority", None) is not None
            if safety is not None
            else None,
            "freeze_authority_active": getattr(safety, "freeze_authority", None) is not None
            if safety is not None
            else None,
        }
        survival = survival_engine(features)
        payoff = payoff_engine(features, survival["grade"])
        unknowns = [key for key, value in features.items() if value is None]
        payload = {
            "state": "FOUND" if market is not None else "NO_MARKET_PAIR",
            "chain": chain,
            "token_address": address,
            "entry_state": str(alpha_entry_state(features)),
            "market": market.to_dict() if market is not None else None,
            "safety": asdict(safety) if safety is not None else None,
            "survival": survival,
            "payoff": payoff,
            "providers": {
                name: {key: value for key, value in result.items() if key != "value"}
                for name, result in results.items()
            },
            "unknowns": unknowns,
            "read_only": True,
        }
        elapsed = (datetime.now(UTC) - started).total_seconds() * 1000
        payload["latency_ms"] = elapsed
        self.store.record_manual_scan(
            chain, address, requested_at, payload, elapsed, guild_id=guild_id, user_id=user_id
        )
        return payload

    async def evaluate(self, discovery: DiscoveryEvent) -> str:
        token_id, _created = self.store.upsert_discovery(discovery)
        candidate_id, candidate_created = self.store.ensure_candidate(
            token_id, discovery.discovered_at, self.settings.scoring_version
        )
        if not candidate_created:
            return "KNOWN_CANDIDATE"
        log_event(
            self.log,
            logging.INFO,
            "token_discovered",
            token=discovery.token_address,
            source=discovery.source,
        )
        log_event(
            self.log,
            logging.INFO,
            "candidate_created",
            token=discovery.token_address,
            candidate_id=candidate_id,
            state=CandidateState.DISCOVERED,
        )
        return await self._monitor_candidate(self.store.candidate_for_token(token_id), discovery)

    async def _monitor_candidate(
        self,
        candidate: Any,
        discovery: DiscoveryEvent | None = None,
        market_override: Any = _MARKET_UNSET,
    ) -> str:
        token_id = int(candidate["token_id"])
        candidate_id = int(candidate["id"])
        candidate_keys = set(candidate.keys())
        if {"token_address", "chain"} <= candidate_keys:
            address, chain = candidate["token_address"], candidate["chain"]
        elif discovery is not None:
            address, chain = discovery.token_address, discovery.chain
        else:
            raise ValueError("candidate monitor row is missing joined token identity")
        if str(candidate["state"]) == str(CandidateState.SIGNALLED):
            return str(CandidateState.SIGNALLED)
        attempted_at = datetime.now(UTC)
        self.store.begin_candidate_attempt(candidate_id, attempted_at.isoformat())
        first_discovered = datetime.fromisoformat(str(candidate["first_discovered_at"]))
        if first_discovered.tzinfo is None:
            first_discovered = first_discovered.replace(tzinfo=UTC)
        if (
            attempted_at - first_discovered
        ).total_seconds() / 60 > self.settings.candidate_max_age_minutes:
            self.store.update_candidate(
                candidate_id,
                CandidateState.EXPIRED,
                "CANDIDATE_MAX_AGE_EXCEEDED",
                waiting_reasons=["CANDIDATE_MAX_AGE_EXCEEDED"],
                expired=True,
            )
            return str(CandidateState.EXPIRED)
        last_seen = candidate["last_monitored_at"]
        if (
            last_seen
            and (datetime.now(UTC) - datetime.fromisoformat(last_seen)).total_seconds() / 60
            > self.settings.candidate_inactivity_timeout_minutes
        ):
            self.store.update_candidate(
                candidate_id,
                CandidateState.EXPIRED,
                "CANDIDATE_INACTIVITY_TIMEOUT",
                waiting_reasons=["CANDIDATE_INACTIVITY_TIMEOUT"],
                expired=True,
            )
            return str(CandidateState.EXPIRED)
        try:
            market = (
                await self.market.market_snapshot(address, chain)
                if market_override is _MARKET_UNSET
                else market_override
            )
        except ProviderError as exc:
            self.store.update_candidate(
                candidate_id,
                CandidateState.FAILED_PROVIDER,
                "MARKET_PROVIDER_UNAVAILABLE",
                waiting_reasons=["MARKET_PROVIDER_UNAVAILABLE"],
            )
            self.store.schedule_candidate_retry(
                candidate_id,
                "MARKET_PROVIDER_UNAVAILABLE",
                "PROVIDER",
                self.settings.candidate_retry_initial_seconds,
                self.settings.candidate_retry_max_seconds,
                self.settings.candidate_retry_backoff,
                attempted_at.isoformat(),
            )
            log_event(
                self.log,
                logging.WARNING,
                "provider_pending",
                token=address,
                candidate_id=candidate_id,
                provider=self.market.name,
                error=str(exc),
            )
            return str(CandidateState.FAILED_PROVIDER)
        if market is None:
            self.store.update_candidate(
                candidate_id,
                CandidateState.PENDING_EVIDENCE,
                "PAIR_NOT_AVAILABLE",
                waiting_reasons=["PAIR_NOT_AVAILABLE"],
            )
            self.store.schedule_candidate_retry(
                candidate_id,
                "PAIR_NOT_AVAILABLE",
                "MISSING_PAIR",
                self.settings.candidate_retry_initial_seconds,
                self.settings.candidate_retry_max_seconds,
                self.settings.candidate_retry_backoff,
                attempted_at.isoformat(),
            )
            return str(CandidateState.PENDING_EVIDENCE)
        if discovery is None:
            discovery = DiscoveryEvent(
                token_address=address,
                chain=chain,
                symbol=candidate["symbol"],
                name=candidate["name"],
                pair_address=candidate["pair_address"],
                discovered_at=candidate["first_discovered_at"],
                source="candidate_monitor",
                metadata=json.loads(candidate["metadata_json"] or "{}"),
            )
        discovery.symbol, discovery.name, discovery.pair_address = (
            market.symbol,
            market.name,
            market.pair_address,
        )
        self.store.upsert_discovery(discovery)
        gmgn_snapshot = None
        gmgn_wallet = {
            "smart_money": "SMART_MONEY_UNKNOWN",
            "buyer_diversity": "UNKNOWN",
            "activity_quality": "UNKNOWN",
            "counts": {},
        }
        previous_rows = list(
            reversed(self.store.recent_snapshots(token_id, self.settings.snapshot_history_limit))
        )
        previous = [json.loads(row["payload_json"]) for row in previous_rows]
        enrichment_calls: dict[str, Any] = {
            "safety": lambda: self.safety_provider.safety(chain, address)
        }
        if self.gmgn is not None:
            enrichment_calls["gmgn"] = lambda: self.gmgn.enrich(chain, address)
        enrichment = await parallel_enrichment(
            enrichment_calls, self.settings.provider_timeout_seconds
        )
        safety = enrichment["safety"].get("value")
        safety_unavailable = safety is None
        if safety_unavailable:
            error = enrichment["safety"].get("error", "UNKNOWN")
            safety = SafetyAssessment(
                checked_at=iso(),
                source=getattr(self.safety_provider, "name", f"{chain}_safety"),
                chain=chain,
                warnings=[f"SAFETY_PROVIDER_UNAVAILABLE:{error}"],
            )
        if "gmgn" in enrichment:
            gmgn_snapshot = enrichment["gmgn"].get("value")
            if gmgn_snapshot is not None:
                gmgn_wallet = wallet_intelligence(
                    gmgn_snapshot.holders, gmgn_snapshot.traders, gmgn_snapshot.info
                )
                self.store.save_gmgn_intelligence(token_id, gmgn_snapshot.to_dict(), gmgn_wallet)
            elif enrichment["gmgn"].get("error"):
                error = str(enrichment["gmgn"]["error"])
                self.store.set_provider_health("gmgn", False, 1, error)
                log_event(self.log, logging.WARNING, "gmgn_degraded", token=address, error=error)
        self.store.save_snapshot(token_id, market, safety.holder_count)
        self.store.record_candidate_snapshot_success(candidate_id, market.captured_at)
        hard_rejections = self.safety_gates.evaluate(market, safety)
        if hard_rejections:
            rejected_score = self.scoring.score(
                {name: None for name in self.settings.weights}, hard_rejections
            )
            self.store.save_evaluation(
                token_id,
                rejected_score,
                {
                    "market": market.to_dict(),
                    "safety": asdict(safety),
                    "unknown_fields": list(self.settings.weights),
                    "terminal_safety_rejection": True,
                },
            )
            self.store.update_candidate(
                candidate_id,
                CandidateState.REJECTED_UNSAFE,
                hard_rejections[0],
                market,
                rejected_score,
                hard_rejections=hard_rejections,
            )
            log_event(
                self.log,
                logging.WARNING,
                "candidate_rejected",
                token=address,
                candidate_id=candidate_id,
                state=CandidateState.REJECTED_UNSAFE,
                reason=hard_rejections,
            )
            return str(CandidateState.REJECTED_UNSAFE)
        expiry = self.safety_gates.expiry(market, candidate["first_discovered_at"])
        if expiry:
            self.store.update_candidate(
                candidate_id,
                CandidateState.EXPIRED,
                expiry,
                market,
                waiting_reasons=[expiry],
                expired=True,
            )
            log_event(
                self.log,
                logging.INFO,
                "candidate_expired",
                token=address,
                candidate_id=candidate_id,
                reason=expiry,
            )
            return str(CandidateState.EXPIRED)
        radar = self.radar.evaluate(
            market,
            previous,
            candidate["first_discovered_at"],
            basic_safety_passed=not safety_unavailable,
        )
        radar_now = False
        if radar.triggered:
            current_priority = priority(
                radar.score,
                float(candidate["confidence"] or 0),
                False,
                int(gmgn_wallet.get("counts", {}).get("smart", 0)),
                gmgn_wallet.get("activity_quality") == "ORGANIC_LIKELY",
            )
            rp = radar_payload(discovery, market, radar, len(previous) + 1)
            rp.update(
                {
                    "priority": current_priority,
                    "entry_quality": entry_quality(
                        candidate["radar_market_cap_usd"] or market.market_cap_usd,
                        market.market_cap_usd,
                    ),
                    "gmgn": gmgn_snapshot.to_dict() if gmgn_snapshot else None,
                    "wallet_intelligence": gmgn_wallet,
                    "social_presence": social_presence(
                        gmgn_snapshot.info if gmgn_snapshot else {},
                        candidate["first_discovered_at"],
                    ),
                    "confidence": candidate["confidence"],
                }
            )
            radar_now = self.store.trigger_radar(
                candidate_id,
                radar.score,
                radar.reasons,
                market,
                rp,
                software_version=self.settings.software_version,
                radar_version=self.settings.radar_version,
                config_fingerprint=self.settings.config_fingerprint(),
            )
            if radar_now:
                log_event(
                    self.log,
                    logging.INFO,
                    "early_radar_triggered",
                    token=address,
                    chain=chain,
                    candidate_id=candidate_id,
                    score=radar.score,
                    reasons=radar.reasons,
                    market_cap=market.market_cap_usd,
                    liquidity=market.liquidity_usd,
                )

        creator_history = (
            self.store.creator_report(discovery.deployer) if discovery.deployer else None
        )
        developer = self.developers.assess(discovery.deployer, creator_history)
        narrative = self.narratives.assess(discovery, market)
        social = self.social.assess(market, previous[-1] if previous else None)
        onchain = self.onchain.assess(safety)
        momentum = self.momentum.assess_history(
            market, previous, self.settings.min_snapshots_for_momentum
        )
        safety_score = (
            None
            if safety_unavailable
            else (
                2.5
                if chain == "bsc"
                and {
                    "BSC_TRANSFER_RESTRICTIONS_UNKNOWN",
                    "BSC_HOLDER_CONCENTRATION_UNKNOWN",
                }.intersection(safety.warnings)
                else (
                    5.0
                    if chain == "bsc" or (not safety.mint_authority and not safety.freeze_authority)
                    else 0.0
                )
            )
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
        entry_state = entry_quality(
            candidate["radar_market_cap_usd"] or market.market_cap_usd, market.market_cap_usd
        )
        if entry_state in {"CHASING", "LATE"}:
            waiting.append("LATE_ENTRY_NOT_QUALIFIED")
        narrative_state = narrative_context(
            narrative.get("label"), candidate["first_discovered_at"]
        )
        component_percent = {
            name: (
                float(score.component_scores.get(name, 0))
                / float(score.component_maxima.get(name, 1))
                * 100
            )
            if score.component_scores.get(name) is not None and score.component_maxima.get(name)
            else None
            for name in score.component_maxima
        }
        pillars = {
            "market_quality": intelligence_pillar(
                component_percent.get("momentum"),
                score.confidence,
                ["market snapshots and momentum"],
                freshness="CURRENT",
            ),
            "wallet_quality": intelligence_pillar(
                min(100, int(gmgn_wallet.get("counts", {}).get("smart", 0)) * 25)
                if gmgn_snapshot
                else None,
                0.7 if gmgn_snapshot else 0,
                [str(gmgn_wallet.get("smart_money"))],
                [] if gmgn_snapshot else ["GMGN wallet evidence unavailable"],
            ),
            "narrative_quality": intelligence_pillar(
                component_percent.get("narrative"),
                score.confidence,
                [narrative_state["quality"], narrative_state["freshness"]],
            ),
            "social_quality": intelligence_pillar(
                component_percent.get("social"),
                score.confidence,
                ["observed market/social evidence"],
            ),
            "safety_quality": intelligence_pillar(
                component_percent.get("safety"),
                0 if safety_unavailable else 1,
                list(safety.warnings),
                ["safety provider unavailable"] if safety_unavailable else [],
            ),
            "entry_quality": intelligence_pillar(
                {
                    "VERY_EARLY": 95,
                    "EARLY": 85,
                    "ACCEPTABLE": 70,
                    "EXTENDED": 45,
                    "CHASING": 20,
                    "LATE": 5,
                }.get(entry_state),
                0.9,
                [entry_state],
            ),
        }
        convergence = signal_convergence(pillars)
        setup = setup_quality(pillars, entry_state)
        survival_result = survival_engine(
            {
                "liquidity_usd": market.liquidity_usd,
                "connected_wallet_percent": gmgn_wallet.get("connected_wallet_percent"),
                "creator_quality": developer.get("classification"),
                "sell_pressure": (
                    market.sells_5m / (market.buys_5m + market.sells_5m)
                    if market.buys_5m is not None
                    and market.sells_5m is not None
                    and market.buys_5m + market.sells_5m > 0
                    else None
                ),
            }
        )
        payoff_result = payoff_engine(
            {
                "market_cap_usd": market.market_cap_usd,
                "liquidity_usd": market.liquidity_usd,
                "price_change_from_launch_percent": market.price_change_5m,
            },
            survival_result["grade"],
        )
        independent_pillars = sum(
            pillar.get("score") is not None and float(pillar.get("confidence") or 0) >= 0.45
            for pillar in pillars.values()
        )
        current_alpha = candidate["authoritative_state"] or AlphaState.DISCOVERED
        try:
            current_alpha = AlphaState(current_alpha)
        except ValueError:
            current_alpha = (
                AlphaState.STANDARD_RADAR
                if candidate["radar_triggered_at"]
                else AlphaState.DISCOVERED
            )
        promoted_alpha = promotion_decision(
            current_alpha,
            score=float(score.normalized_score or 0),
            confidence=score.confidence,
            entry=EntryState(entry_state),
            survival=survival_result["grade"],
            payoff=payoff_result["grade"],
            independent_pillars=independent_pillars,
        )
        alpha_decision = AlphaDecision(
            promoted_alpha,
            EntryState(entry_state),
            score.confidence,
            [f"MARKET_STAGE_{promoted_alpha}"],
            [],
            {
                "market_cap_usd": market.market_cap_usd,
                "liquidity_usd": market.liquidity_usd,
                "normalized_score": score.normalized_score,
                "independent_pillars": independent_pillars,
            },
            stage="T+MARKET",
            survival=SurvivalGrade(survival_result["grade"]),
            payoff=PayoffGrade(payoff_result["grade"]),
        )
        self.store.record_v14_decision(
            candidate_id,
            None,
            alpha_decision,
            self.settings,
            observed_at=market.captured_at,
            providers=enrichment,
        )
        trade = tradeability(market.liquidity_usd)
        self.store.record_v15_tradeability(candidate_id, market.captured_at, trade)
        age_minutes = max(
            0.0,
            (
                datetime.fromisoformat(market.captured_at)
                - datetime.fromisoformat(candidate["first_discovered_at"])
            ).total_seconds()
            / 60,
        )
        v15_stage = _v15_stage(discovery, market)
        liquidity_quality = (
            None if market.liquidity_usd is None else min(100.0, market.liquidity_usd / 250)
        )
        momentum_quality = (
            None
            if momentum.get("score") is None
            else min(100.0, float(momentum["score"]) / 15 * 100)
        )
        wallet_quality = (
            min(100.0, float(gmgn_wallet.get("counts", {}).get("smart", 0)) * 25)
            if gmgn_snapshot
            else None
        )
        concentration = economic_concentration(
            _economic_holder_rows(gmgn_snapshot.holders if gmgn_snapshot else None)
        )
        actor_independence = (
            max(0.0, 100.0 - float(concentration["effective_actor_concentration"]) * 2)
            if gmgn_snapshot and gmgn_snapshot.holders is not None
            else None
        )
        buyer_cohorts = (
            list((gmgn_snapshot.info or {}).get("buyer_cohorts") or []) if gmgn_snapshot else []
        )
        buyer_replacement = buyer_trajectory(buyer_cohorts)
        freshness_limit = max(60.0, self.settings.gmgn_cache_ttl_seconds * 2)
        provenance = [
            {
                "field_name": "market",
                "value": {
                    "market_cap_usd": market.market_cap_usd,
                    "liquidity_usd": market.liquidity_usd,
                },
                "provider": market.source,
                "retrieved_at": market.captured_at,
                "age_seconds": 0,
                "confidence": 1,
                "conflict_state": "KNOWN",
            },
            {
                "field_name": "safety",
                "value": {
                    "top10_percent": safety.top10_percent,
                    "holder_count": safety.holder_count,
                },
                "provider": safety.source,
                "retrieved_at": safety.checked_at,
                "age_seconds": _evidence_age_seconds(safety.checked_at, market.captured_at) or 0,
                "confidence": 0 if safety_unavailable else 1,
                "conflict_state": "UNKNOWN" if safety_unavailable else "KNOWN",
            },
        ]
        if gmgn_snapshot:
            gmgn_age = _evidence_age_seconds(gmgn_snapshot.retrieved_at, market.captured_at) or 0
            provenance.append(
                {
                    "field_name": "wallet_quality",
                    "value": {
                        "buyer_diversity": gmgn_wallet.get("buyer_diversity"),
                        "activity_quality": gmgn_wallet.get("activity_quality"),
                    },
                    "provider": "gmgn",
                    "retrieved_at": gmgn_snapshot.retrieved_at,
                    "age_seconds": gmgn_age,
                    "confidence": 0.7,
                    "conflict_state": "STALE" if gmgn_age > freshness_limit else "KNOWN",
                }
            )
        self.store.record_v15_provider_evidence(candidate_id, provenance)
        stale_evidence = [
            row["field_name"] for row in provenance if float(row["age_seconds"]) > freshness_limit
        ]
        provider_conflicts = [warning for warning in safety.warnings if "CONFLICT" in warning]
        survival_quality = survival_result.get("score")
        payoff_quality = payoff_result.get("score")
        if v15_stage == Stage.MIGRATED:
            v15_features = {
                "amm_liquidity": liquidity_quality,
                "tradeability": {"GOOD": 90, "LIMITED": 55, "POOR": 20}.get(trade["grade"]),
                "migration_continuity": None,
                "buyer_quality": wallet_quality,
                "buyer_replacement": buyer_replacement.get("score"),
                "actor_independence": actor_independence,
                "post_migration_momentum": momentum_quality,
                "survival_quality": survival_quality,
                "payoff_quality": payoff_quality,
            }
        elif v15_stage == Stage.BONDING:
            v15_features = {
                "curve_progress": discovery.metadata.get("bonding_curve_progress_percent"),
                "momentum_acceleration": momentum_quality,
                "buyer_retention": (gmgn_snapshot.info or {}).get("buyer_retention_score")
                if gmgn_snapshot
                else None,
                "buyer_replacement": buyer_replacement.get("score"),
                "concentration_trend": (gmgn_snapshot.info or {}).get("concentration_trend_score")
                if gmgn_snapshot
                else None,
                "survival_quality": survival_quality,
                "payoff_quality": payoff_quality,
            }
        elif v15_stage == Stage.REVIVAL:
            v15_features = {
                "abnormal_volume": momentum_quality,
                "new_wallet_cohort": buyer_replacement.get("score"),
                "fresh_catalyst": discovery.metadata.get("fresh_catalyst_score"),
                "renewed_liquidity": liquidity_quality,
                "narrative_relevance": narrative.get("score"),
                "survival_quality": survival_quality,
                "payoff_quality": payoff_quality,
            }
        else:
            v15_features = {
                "launch_verified": 100 if candidate["source_event_timestamp"] else None,
                "early_demand": momentum_quality,
                "buyer_independence": actor_independence,
                "creator_quality": (
                    None
                    if developer.get("score") is None
                    else min(100.0, float(developer["score"]) / 15 * 100)
                ),
                "early_liquidity": liquidity_quality,
                "survival_quality": survival_quality,
                "payoff_quality": payoff_quality,
            }
        v15_features.update(
            {
                "call_market_cap": candidate["radar_market_cap_usd"] or market.market_cap_usd,
                "current_market_cap": market.market_cap_usd,
                "age_minutes": age_minutes,
                "vertical_acceleration": momentum.get("acceleration"),
                "sell_restriction_unknown": "BSC_TRANSFER_RESTRICTIONS_UNKNOWN" in safety.warnings,
                "concentration_unknown": (
                    safety.top10_percent is None and actor_independence is None
                ),
                "toxic_creator": str(developer.get("classification")) in {"TOXIC", "KNOWN_BAD"},
                "poor_tradeability": trade["grade"] == "POOR",
                "connected_concentration": (
                    actor_independence is not None and actor_independence < 40
                ),
                "buyer_collapse": buyer_replacement.get("state") == "BUYER_COLLAPSE",
                "terminal_safety_failure": bool(safety.rejection_reasons),
                "provider_conflicts": provider_conflicts,
                "stale_evidence": stale_evidence,
                "critical_unknowns": [],
                "why_now": [
                    reason
                    for reason in (
                        "momentum acceleration"
                        if momentum_quality and momentum_quality >= 70
                        else None,
                        "tradeable liquidity" if trade["grade"] == "GOOD" else None,
                    )
                    if reason
                ],
            }
        )
        if self.historical_context is not None:
            self.historical_context.apply(
                chain,
                address,
                market.captured_at,
                str(v15_stage),
                v15_features,
            )
            historical = v15_features.get("historical_context") or {}
            if historical.get("state") == "APPLIED":
                v15_features["why_now"] = [
                    *v15_features["why_now"],
                    "approved historical context",
                ][:2]
        v15_decision = evaluate_v15(v15_stage, v15_features)
        self.store.record_v15_decision(
            candidate_id, v15_decision, self.settings, address, chain, market
        )
        if promoted_alpha in {
            AlphaState.HOT_RADAR,
            AlphaState.PRIORITY_RADAR,
            AlphaState.QUALIFIED_SIGNAL,
        }:
            self.store.record_immutable_call(
                candidate_id,
                str(promoted_alpha),
                {
                    "call_timestamp": market.captured_at,
                    "market_cap_usd": market.market_cap_usd,
                    "price_usd": market.price_usd,
                    "liquidity_usd": market.liquidity_usd,
                    "score": score.normalized_score,
                    "confidence": score.confidence,
                    "entry_state": entry_state,
                    "features": alpha_decision.feature_vector,
                    "providers": enrichment,
                },
                self.settings,
            )
        self.store.record_narrative_event(
            token_id,
            f"narrative:{token_id}:{narrative_state['identity']}:{narrative_state['freshness']}",
            narrative_state,
        )
        self.store.record_confidence_context(
            candidate_id,
            market.captured_at,
            score.normalized_score,
            score.confidence,
            convergence,
            setup,
            "EVALUATED",
        )
        intelligence = {
            "developer": developer,
            "narrative": narrative,
            "social": social,
            "onchain": onchain,
            "momentum": momentum,
            "pillars": pillars,
            "convergence": convergence,
            "setup_quality": setup,
            "entry_quality": entry_state,
            "narrative_context": narrative_state,
            "risks": list(safety.warnings)
            + (["Very young token"] if market.pair_created_at else []),
            "thesis": [
                x
                for x in [
                    f"{narrative.get('label')} metadata fit" if narrative.get("label") else None,
                    "buying and volume acceleration observed" if momentum.get("score") else None,
                    "distribution measured on Solana RPC"
                    if safety.top10_percent is not None
                    else None,
                ]
                if x
            ],
        }
        evidence = {
            "market": market.to_dict(),
            "safety": asdict(safety),
            "intelligence": intelligence,
            "gmgn": gmgn_snapshot.to_dict() if gmgn_snapshot else None,
            "wallet_intelligence": gmgn_wallet,
            "unknown_fields": [
                name
                for name, value in {
                    "holder_count": safety.holder_count,
                    "bundled_percent": safety.bundled_percent,
                    "deployer_percent": safety.deployer_percent,
                    "developer_score": developer.get("score"),
                    "social_velocity": social.get("score"),
                }.items()
                if value is None
            ],
        }
        self.store.save_evaluation(token_id, score, evidence)
        realtime_feature = self.realtime_features.latest(token_id, market.captured_at)
        realtime_capital = (realtime_feature or {}).get("capital_trajectory") or {}
        realtime_buyers = (realtime_feature or {}).get("buyer_arrival") or {}
        realtime_available = (realtime_feature or {}).get("available_timestamp") or market.captured_at
        realtime_observed = (realtime_feature or {}).get("decision_timestamp") or market.captured_at
        v3_evidence = {
            "market_cap": TimedValue(
                market.market_cap_usd,
                market.captured_at,
                market.captured_at,
                market.source,
                "KNOWN" if market.market_cap_usd is not None else "UNKNOWN",
            ),
            "liquidity": TimedValue(
                market.liquidity_usd,
                market.captured_at,
                market.captured_at,
                market.source,
                "KNOWN" if market.liquidity_usd is not None else "UNKNOWN",
            ),
            "price": TimedValue(
                market.price_usd,
                market.captured_at,
                market.captured_at,
                market.source,
                "KNOWN" if market.price_usd is not None else "UNKNOWN",
            ),
            "real_sol_reserve": TimedValue(
                realtime_capital.get("real_sol_reserve"),
                realtime_observed,
                realtime_available,
                "canonical_realtime_fabric",
                "KNOWN"
                if realtime_capital.get("real_sol_reserve") is not None
                else "UNKNOWN",
            ),
            "independent_buyer_velocity": TimedValue(
                realtime_buyers.get("independent_new_buyers_per_second"),
                realtime_observed,
                realtime_available,
                "canonical_realtime_fabric",
                "KNOWN"
                if realtime_buyers.get("independent_new_buyers_per_second") is not None
                else "UNKNOWN",
            ),
        }
        v3_envelope = self.v3_shadow.evaluate(
            decision_timestamp=market.captured_at,
            evidence=v3_evidence,
            forecast=None,
            actionability=EntryActionability(
                valid=trade["grade"] in {"GOOD", "LIMITED"},
                score={"GOOD": 0.9, "LIMITED": 0.55, "POOR": 0.1}.get(trade["grade"]),
                sellable=None if trade["grade"] == "UNKNOWN" else trade["grade"] != "POOR",
                delay_seconds=0,
                reason="TRADEABILITY_UNKNOWN" if trade["grade"] == "UNKNOWN" else None,
            ),
            legacy_result={
                "classification": str(score.classification),
                "normalized_score": score.normalized_score,
                "confidence": score.confidence,
                "hard_rejections": list(score.hard_rejections),
            },
            v15_result=v15_decision.to_dict(),
            positive_evidence=v15_decision.why_now,
            negative_evidence=v15_decision.critical_unknowns,
            hard_risk_evidence=(
                v15_decision.failure_reasons
                if v15_features.get("terminal_safety_failure")
                else []
            ),
            feature_versions={"shadow_adapter": "v3.0.0", "control_features": "v15"},
        )
        self.store.save_v3_shadow_decision(
            candidate_id=candidate_id,
            token_id=token_id,
            envelope=v3_envelope.to_dict(),
            control_decision={
                "legacy": v3_envelope.legacy_result,
                "v15": v3_envelope.v15_result,
            },
            v2_decision={"state": "NOT_EVALUATED_LIVE_RESEARCH_ONLY"},
            features={
                "v15": v15_features,
                "realtime_trajectory": realtime_feature,
                "v3_available": {
                    name: value.value for name, value in v3_evidence.items()
                },
            },
            latency={
                "provider_seconds": _evidence_age_seconds(
                    market.captured_at, market.captured_at
                ),
                "model_seconds": None,
                "discord_seconds": None,
            },
            veto_reasons=[
                *score.hard_rejections,
                *v15_decision.failure_reasons,
                *([v3_envelope.abstain_reason] if v3_envelope.abstain_reason else []),
            ],
        )
        signal_grade = score.classification in {
            SignalClass.WATCH,
            SignalClass.STRONG,
            SignalClass.HIGH_CONVICTION,
        } and v15_decision.signal_tier in {
            SignalTier.PREMIUM,
            SignalTier.STRONG,
            SignalTier.HIGH_RISK_MOMENTUM,
            SignalTier.CATALYST_REVIVAL,
        }
        existing_signal_id = candidate["signal_id"]
        if existing_signal_id:
            if signal_grade and not waiting:
                update = self.store.update_signal_intelligence(
                    int(existing_signal_id),
                    str(score.classification),
                    score,
                    intelligence["thesis"],
                    {
                        "symbol": market.symbol,
                        "token_address": address,
                        "chain": chain,
                        "pair_address": market.pair_address,
                        "normalized_score": score.normalized_score,
                        "confidence": score.confidence,
                        "shadow": self.settings.shadow_mode,
                    },
                )
                reason = update or "SIGNAL_MONITORING"
            else:
                deterioration = waiting or ["NORMALIZED_SCORE_BELOW_WATCH"]
                self.store.update_signal_intelligence(
                    int(existing_signal_id),
                    "BELOW_WATCH",
                    score,
                    deterioration,
                    {
                        "symbol": market.symbol,
                        "token_address": address,
                        "chain": chain,
                        "pair_address": market.pair_address,
                        "normalized_score": score.normalized_score,
                        "confidence": score.confidence,
                        "shadow": self.settings.shadow_mode,
                    },
                )
                reason = deterioration[0]
            self.store.update_candidate(
                candidate_id,
                CandidateState.SIGNALLED,
                reason,
                market,
                score,
                waiting_reasons=sorted(set(waiting)),
                unknown_fields=evidence["unknown_fields"],
                signal_id=int(existing_signal_id),
            )
            return str(CandidateState.SIGNALLED)
        if waiting or not signal_grade:
            has_radar = radar_now or candidate["radar_triggered_at"] is not None
            state = (
                CandidateState.EARLY_RADAR
                if has_radar
                else (CandidateState.PENDING_EVIDENCE if waiting else CandidateState.CANDIDATE)
            )
            reason = waiting[0] if waiting else "SCORE_BELOW_WATCH"
            self.store.update_candidate(
                candidate_id,
                state,
                reason,
                market,
                score,
                waiting_reasons=sorted(set(waiting)),
                unknown_fields=evidence["unknown_fields"],
            )
            log_event(
                self.log,
                logging.INFO,
                "candidate_snapshot",
                token=address,
                candidate_id=candidate_id,
                state=state,
                reason=reason,
                score=score.normalized_score,
                confidence=score.confidence,
                market_cap=market.market_cap_usd,
                liquidity=market.liquidity_usd,
                snapshot_count=len(previous) + 1,
            )
            return str(state)
        log_event(
            self.log,
            logging.INFO,
            "evaluation_decision",
            token=address,
            classification=score.classification,
            score=score.normalized_score,
            confidence=score.confidence,
            rejections=score.hard_rejections,
        )
        payload = signal_payload(
            discovery,
            market,
            safety,
            intelligence,
            score,
            self.settings.shadow_mode,
        )
        payload.update(
            {
                "v15_signal_tier": str(v15_decision.signal_tier),
                "runner_score": v15_decision.runner_score,
                "failure_score": v15_decision.failure_score,
                "setup_conviction": v15_decision.setup_conviction,
                "evidence_coverage": v15_decision.evidence_coverage,
                "entry_status": str(v15_decision.entry_status),
                "survival_grade": v15_decision.survival_grade,
                "v15_stage": str(v15_decision.stage),
                "provider_conflicts": v15_decision.provider_conflicts,
                "critical_unknowns": v15_decision.critical_unknowns,
                "failure_reasons": v15_decision.failure_reasons,
                "why_now": v15_decision.why_now,
                "tradeability": trade,
                "actor_concentration": concentration,
                "buyer_replacement": buyer_replacement,
                "historical_context": v15_features.get("historical_context"),
                "realtime_intelligence": realtime_feature,
            }
        )
        signal_id = self.store.create_signal(
            token_id,
            market,
            score,
            {
                "developer": developer,
                "narrative": narrative,
                "social": social,
                "onchain": onchain,
                "candidate_history": {
                    "candidate_id": candidate_id,
                    "snapshot_count": len(previous) + 1,
                    "first_discovered_at": candidate["first_discovered_at"],
                },
            },
            intelligence["risks"],
            payload,
            safety.holder_count,
        )
        if signal_id:
            self.store.update_candidate(
                candidate_id,
                CandidateState.QUALIFIED_SIGNAL,
                f"PROMOTED_{score.classification}",
                market,
                score,
                unknown_fields=evidence["unknown_fields"],
                signal_id=signal_id,
            )
            log_event(
                self.log,
                logging.INFO,
                "candidate_promoted",
                token=address,
                candidate_id=candidate_id,
                signal_id=signal_id,
                state=score.classification,
                score=score.normalized_score,
                confidence=score.confidence,
            )
        return str(score.classification)

    async def monitor_candidates_once(self) -> dict[str, int]:
        results: dict[str, int] = {}
        reconciled = self.store.reconcile_stale_candidates(self.settings.candidate_max_age_minutes)
        if reconciled:
            results["STALE_PENDING_RECONCILIATION"] = reconciled
        candidates = self.store.active_candidates(
            self.settings.max_active_candidates,
            self.settings.max_active_candidates_per_chain,
            self.settings.scheduler_fresh_reserved_slots,
            self.settings.scheduler_radar_reserved_slots,
            self.settings.scheduler_near_signal_reserved_slots,
            genesis_reserved=self.settings.scheduler_genesis_reserved_slots,
            priority_reserved=self.settings.scheduler_priority_reserved_slots,
        )
        prefetched: dict[tuple[str, str], Any] = {}
        batch_fetch = getattr(self.market, "market_snapshots", None)
        if batch_fetch:
            by_chain: dict[str, list[str]] = {}
            for candidate in candidates:
                by_chain.setdefault(str(candidate["chain"]), []).append(
                    str(candidate["token_address"])
                )
            for chain, addresses in by_chain.items():
                for address, snapshot in (await batch_fetch(addresses, chain)).items():
                    prefetched[(chain, address)] = snapshot
        for candidate in candidates:
            try:
                key = (str(candidate["chain"]), str(candidate["token_address"]))
                result = await self._monitor_candidate(
                    candidate,
                    market_override=prefetched.get(key, _MARKET_UNSET),
                )
            except Exception:
                self.log.exception(
                    "candidate monitoring failed",
                    extra={"fields": {"candidate_id": candidate["id"]}},
                )
                result = "ERROR"
            results[result] = results.get(result, 0) + 1
        return results

    async def monitor_outcomes_once(self) -> int:
        since = (
            datetime.now(UTC) - timedelta(hours=self.settings.outcome_max_age_hours)
        ).isoformat()
        monitored = 0
        for token in self.store.outcome_watchlist(since, self.settings.max_outcome_watchlist):
            try:
                snapshot = await self.market.market_snapshot(token["token_address"], token["chain"])
            except ProviderError:
                continue
            if snapshot and snapshot.market_cap_usd is not None and snapshot.market_cap_usd > 0:
                self.store.save_snapshot(int(token["token_id"]), snapshot)
                monitored += 1
        if monitored:
            self.actor_intelligence.build_wallet_copyability(matured_before=iso())
        return monitored

    async def scan_once(self) -> dict[str, int]:
        results: dict[str, int] = {}
        try:
            discoveries = await self.discovery.poll()
        except ProviderError as exc:
            log_event(self.log, logging.ERROR, "discovery_failure", error=str(exc))
            return {"DISCOVERY_FAILURE": 1}
        for discovered in fair_chain_sample(discoveries, self.settings.max_discoveries_per_cycle):
            try:
                result = await self.evaluate(discovered)
            except Exception:
                self.log.exception(
                    "token evaluation failed", extra={"fields": {"token": discovered.token_address}}
                )
                result = "ERROR"
            results[result] = results.get(result, 0) + 1
        return results

    async def flush_outbox(self) -> int:
        sent = 0
        for row in self.store.claim_outbox():
            claim_token = row["claim_token"]
            try:
                delivery_started = datetime.now(UTC)
                payload = json.loads(row["payload_json"])
                content = format_discord_event(row["event_type"], payload)
                has_guild_settings = bool(
                    self.store.conn.execute("SELECT COUNT(*) FROM guild_settings").fetchone()[0]
                )
                destinations = [
                    d
                    for d in self.store.alert_destinations()
                    if self.store.alert_allowed(d["alert_tier"], row["event_type"], payload)
                    and str(payload.get("chain") or "").lower()
                    in json.loads(d.get("enabled_chains_json") or "[]")
                ]
                if destinations and hasattr(self.notifier, "send_to"):
                    for destination in destinations:
                        self.store.ensure_guild_alert_delivery(
                            int(row["id"]), destination["guild_id"], destination["alert_channel_id"]
                        )
                    failures = []
                    remote_ids = []
                    for delivery in self.store.pending_guild_alert_deliveries(int(row["id"])):
                        try:
                            remote = await self.notifier.send_to(
                                int(delivery["channel_id"]), content
                            )
                            self.store.mark_guild_alert_delivery(int(delivery["id"]), True, remote)
                            remote_ids.append(str(remote or ""))
                        except Exception as exc:  # noqa: BLE001 - isolate arbitrary notifier failures per guild
                            self.store.mark_guild_alert_delivery(
                                int(delivery["id"]), False, error=str(exc)
                            )
                            failures.append(exc)
                    if failures:
                        raise failures[0]
                    remote_id = ",".join(remote_ids)
                else:
                    channels = self.settings.discord_channel_ids
                    if not channels and self.settings.discord_channel_id:
                        channels = (self.settings.discord_channel_id,)
                    if has_guild_settings:
                        self.store.mark_outbox_sent(
                            int(row["id"]), "policy-suppressed", claim_token
                        )
                        sent += 1
                        continue
                    if not channels and hasattr(self.notifier, "send_to"):
                        self.store.mark_outbox_sent(
                            int(row["id"]), "no-configured-destination", claim_token
                        )
                        sent += 1
                        continue
                if not destinations and channels and hasattr(self.notifier, "send_to"):
                    self.store.ensure_alert_deliveries(int(row["id"]), channels)
                    failures = []
                    remote_ids = []
                    for delivery in self.store.pending_alert_deliveries(int(row["id"])):
                        try:
                            remote = await self.notifier.send_to(
                                int(delivery["channel_id"]), content
                            )
                            self.store.mark_alert_delivery(int(delivery["id"]), True, remote)
                            remote_ids.append(str(remote or ""))
                        except Exception as exc:  # noqa: BLE001 - isolate arbitrary notifier failures per channel
                            self.store.mark_alert_delivery(
                                int(delivery["id"]), False, error=str(exc)
                            )
                            failures.append(exc)
                    if failures:
                        raise failures[0]
                    remote_id = ",".join(remote_ids)
                elif not destinations:
                    remote_id = await self.notifier.send(content)
                self.store.mark_outbox_sent(int(row["id"]), remote_id, claim_token)
                trigger = (payload.get("realtime_intelligence") or {}).get(
                    "trigger_event_id"
                )
                if trigger:
                    with self.store._lock, self.store.conn:
                        self.store.conn.execute(
                            "UPDATE canonical_events SET discord_sent_timestamp=? "
                            "WHERE event_id=?",
                            (iso(), str(trigger)),
                        )
                self.store.record_latency(
                    "DISCORD_DELIVERY",
                    (datetime.now(UTC) - delivery_started).total_seconds() * 1000,
                    "discord",
                    {"outbox_id": row["id"], "event_type": row["event_type"]},
                )
                self.store.set_provider_health("discord", True, 0, None)
                sent += 1
            except Exception as exc:  # noqa: BLE001 - outbox must persist any delivery failure
                self.store.mark_outbox_error(int(row["id"]), str(exc), claim_token)
                previous = self.store.conn.execute(
                    "SELECT consecutive_failures FROM provider_health WHERE provider='discord'"
                ).fetchone()
                self.store.set_provider_health(
                    "discord", False, int(previous[0]) + 1 if previous else 1, str(exc)
                )
                log_event(
                    self.log, logging.ERROR, "discord_failure", outbox_id=row["id"], error=str(exc)
                )
                continue
        return sent

    async def run(self) -> None:
        recovered_realtime = self.realtime_fabric.recover_stale_claims()
        log_event(
            self.log,
            logging.INFO,
            "restart_recovery",
            active_signals=len(self.store.active_signals()),
            pending_outbox=len(self.store.pending_outbox()),
            recovered_realtime_claims=recovered_realtime,
        )

        async def scanner() -> None:
            while not self.stop_event.is_set():
                await self.scan_once()
                await self.flush_outbox()
                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(), self.settings.discovery_interval_seconds
                    )
                except TimeoutError:
                    pass

        async def tracker() -> None:
            while not self.stop_event.is_set():
                try:
                    await self.tracker.monitor_once()
                    await self.flush_outbox()
                except Exception:
                    self.log.exception("tracking cycle failed")
                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(), self.settings.monitor_interval_seconds
                    )
                except TimeoutError:
                    pass

        async def candidate_monitor() -> None:
            while not self.stop_event.is_set():
                await self.monitor_candidates_once()
                await self.flush_outbox()
                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(), self.settings.candidate_monitor_interval_seconds
                    )
                except TimeoutError:
                    pass

        async def outcome_monitor() -> None:
            while not self.stop_event.is_set():
                await self.monitor_outcomes_once()
                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(), self.settings.outcome_monitor_interval_seconds
                    )
                except TimeoutError:
                    pass

        async def launch_worker() -> None:
            while not self.stop_event.is_set():
                try:
                    event = await asyncio.wait_for(self.launch_queue.queue.get(), timeout=0.5)
                except TimeoutError:
                    continue
                try:
                    await self.handle_launch_event(event)
                except Exception:
                    self.log.exception(
                        "launch event processing failed",
                        extra={"fields": {"event_key": event.event_key}},
                    )
                finally:
                    self.launch_queue.task_done(event)

        async def realtime_worker() -> None:
            while not self.stop_event.is_set():
                rows = self.realtime_fabric.claim_pending(
                    self.settings.realtime_processing_batch
                )
                if not rows:
                    self.realtime_wake.clear()
                    try:
                        await asyncio.wait_for(self.realtime_wake.wait(), timeout=0.5)
                    except TimeoutError:
                        pass
                    continue
                for event in rows:
                    try:
                        await self.handle_realtime_event(event)
                    except Exception as exc:
                        self.realtime_fabric.fail(event.event_id, str(exc))
                        self.log.exception(
                            "canonical event processing failed",
                            extra={"fields": {"event_id": event.event_id}},
                        )

        tasks = [scanner(), candidate_monitor(), outcome_monitor(), tracker()]
        if self.launch_sources:
            tasks.append(launch_worker())
            tasks.extend(
                source.run(self.offer_launch_event, self.stop_event)
                for source in self.launch_sources
            )
        if self.settings.realtime_fabric_enabled:
            tasks.append(realtime_worker())
        if self.realtime_sources:
            tasks.extend(
                source.run_events(self.offer_realtime_event, self.stop_event)
                for source in self.realtime_sources
            )
        await asyncio.gather(*tasks)

    def stop(self) -> None:
        self.stop_event.set()

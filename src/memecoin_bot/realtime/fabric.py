from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from memecoin_bot.models import DiscoveryEvent, iso
from memecoin_bot.realtime.events import CanonicalEvent, CanonicalEventType, ProviderState


def _json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"), sort_keys=True)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class IngestResult:
    event_id: str
    status: str
    is_new: bool
    confirmation_count: int
    conflict: bool = False


class CanonicalEventFabric:
    """Persistent central dedupe and projection boundary for every realtime source."""

    def __init__(self, store: Any):
        self.store = store

    def publish(self, event: CanonicalEvent) -> IngestResult:
        event.validate()
        now = iso()
        semantic = event.semantic_fingerprint()
        source_event_id = str(event.source_event_id or "")
        with self.store._lock, self.store.conn:
            row = self.store.conn.execute(
                "SELECT * FROM canonical_events WHERE canonical_key=?", (event.canonical_key,)
            ).fetchone()
            if row is None:
                self.store.conn.execute(
                    "INSERT INTO canonical_events(event_id,canonical_key,event_type,canonical_token,"
                    "chain,platform,first_seen_source,source_timestamp,received_timestamp,"
                    "available_timestamp,normalized_timestamp,slot_or_block,transaction_signature,"
                    "pool_identity,confidence,semantic_fingerprint,raw_provenance_json,payload_json,"
                    "schema_version,confirmation_sources_json,provider_latency_json,first_seen_at,last_seen_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        event.event_id,
                        event.canonical_key,
                        str(event.event_type),
                        event.canonical_token,
                        event.chain,
                        event.platform,
                        event.source,
                        event.source_timestamp,
                        event.received_timestamp,
                        event.available_timestamp,
                        now,
                        event.slot_or_block,
                        event.transaction_signature,
                        event.pool_identity,
                        event.confidence,
                        semantic,
                        _json(event.raw_provenance),
                        _json(event.payload),
                        event.schema_version,
                        _json([event.source]),
                        _json({event.source: event.provider_latency_ms}),
                        now,
                        now,
                    ),
                )
                self._insert_source(event, semantic, source_event_id)
                self._observe_provider_event(event)
                return IngestResult(event.event_id, "NEW", True, 1)

            event_id = str(row["event_id"])
            existing_source = self.store.conn.execute(
                "SELECT 1 FROM canonical_event_sources WHERE event_id=? AND source=? "
                "AND source_event_id=?",
                (event_id, event.source, source_event_id),
            ).fetchone()
            if existing_source:
                return IngestResult(
                    event_id,
                    "DUPLICATE",
                    False,
                    len(_loads(row["confirmation_sources_json"], [])),
                )

            conflict = semantic != str(row["semantic_fingerprint"])
            if conflict:
                self.store.conn.execute(
                    "INSERT OR IGNORE INTO canonical_event_conflicts(event_id,source,"
                    "existing_fingerprint,incoming_fingerprint,existing_payload_json,"
                    "incoming_payload_json,detected_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        event_id,
                        event.source,
                        row["semantic_fingerprint"],
                        semantic,
                        row["payload_json"],
                        _json(event.payload),
                        now,
                    ),
                )
            self._insert_source(event, semantic, source_event_id, event_id=event_id)
            self._observe_provider_event(event)
            sources = sorted(set(_loads(row["confirmation_sources_json"], [])) | {event.source})
            latencies = _loads(row["provider_latency_json"], {})
            latencies[event.source] = event.provider_latency_ms
            conflicts = _loads(row["conflicts_json"], [])
            if conflict:
                conflicts.append(
                    {
                        "source": event.source,
                        "existing": row["semantic_fingerprint"],
                        "incoming": semantic,
                        "detected_at": now,
                    }
                )
            self.store.conn.execute(
                "UPDATE canonical_events SET confirmation_sources_json=?,provider_latency_json=?,"
                "conflicts_json=?,confidence=MAX(confidence,?),last_seen_at=? WHERE event_id=?",
                (_json(sources), _json(latencies), _json(conflicts), event.confidence, now, event_id),
            )
            return IngestResult(
                event_id,
                "CONFLICT" if conflict else "CONFIRMED",
                False,
                len(sources),
                conflict,
            )

    def _observe_provider_event(self, event: CanonicalEvent) -> None:
        if event.event_type == CanonicalEventType.PROVIDER_HEALTH:
            return
        self.store.conn.execute(
            "INSERT INTO provider_health(provider,healthy,consecutive_failures,last_success_at,"
            "updated_at,state,last_message_at,last_valid_event_at,latency_ms,events_received) "
            "VALUES(?,1,0,?,?,'CONNECTED',?,?,?,1) ON CONFLICT(provider) DO UPDATE SET "
            "healthy=1,consecutive_failures=0,last_success_at=excluded.last_success_at,"
            "updated_at=excluded.updated_at,state='CONNECTED',last_message_at=excluded.last_message_at,"
            "last_valid_event_at=excluded.last_valid_event_at,latency_ms=excluded.latency_ms,"
            "events_received=provider_health.events_received+1",
            (
                event.source,
                event.received_timestamp,
                event.available_timestamp,
                event.received_timestamp,
                event.available_timestamp,
                event.provider_latency_ms,
            ),
        )

    def _insert_source(
        self,
        event: CanonicalEvent,
        semantic: str,
        source_event_id: str,
        *,
        event_id: str | None = None,
    ) -> None:
        self.store.conn.execute(
            "INSERT INTO canonical_event_sources(event_id,source,source_event_id,source_timestamp,"
            "received_timestamp,available_timestamp,provider_latency_ms,availability_latency_ms,"
            "confidence,semantic_fingerprint,raw_provenance_json,payload_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id or event.event_id,
                event.source,
                source_event_id,
                event.source_timestamp,
                event.received_timestamp,
                event.available_timestamp,
                event.provider_latency_ms,
                event.availability_latency_ms,
                event.confidence,
                semantic,
                _json(event.raw_provenance),
                _json(event.payload),
            ),
        )

    def recover_stale_claims(self, lease_seconds: float = 120) -> int:
        cutoff = (datetime.now(UTC) - timedelta(seconds=lease_seconds)).isoformat()
        with self.store._lock, self.store.conn:
            cur = self.store.conn.execute(
                "UPDATE canonical_events SET processing_status='PENDING',claimed_at=NULL,"
                "processing_error='STALE_CLAIM_RECOVERED' WHERE processing_status='PROCESSING' "
                "AND claimed_at<?",
                (cutoff,),
            )
            return int(cur.rowcount)

    def claim_pending(self, limit: int = 100) -> list[CanonicalEvent]:
        if limit <= 0:
            return []
        now = iso()
        with self.store._lock:
            self.store.conn.execute("BEGIN IMMEDIATE")
            try:
                rows = list(
                    self.store.conn.execute(
                        "SELECT * FROM canonical_events WHERE processing_status='PENDING' "
                        "ORDER BY available_timestamp,event_id LIMIT ?",
                        (limit,),
                    )
                )
                if rows:
                    placeholders = ",".join("?" for _ in rows)
                    self.store.conn.execute(
                        f"UPDATE canonical_events SET processing_status='PROCESSING',claimed_at=?,"
                        f"processing_attempts=processing_attempts+1 WHERE event_id IN ({placeholders})",
                        (now, *(row["event_id"] for row in rows)),
                    )
                self.store.conn.commit()
            except Exception:
                self.store.conn.rollback()
                raise
        return [self._event_from_row(row) for row in rows]

    @staticmethod
    def _event_from_row(row: Any) -> CanonicalEvent:
        return CanonicalEvent(
            event_id=str(row["event_id"]),
            event_type=CanonicalEventType(str(row["event_type"])),
            canonical_token=str(row["canonical_token"]),
            chain=str(row["chain"]),
            platform=str(row["platform"]),
            source=str(row["first_seen_source"]),
            source_timestamp=str(row["source_timestamp"]),
            received_timestamp=str(row["received_timestamp"]),
            available_timestamp=str(row["available_timestamp"]),
            slot_or_block=row["slot_or_block"],
            transaction_signature=row["transaction_signature"],
            pool_identity=row["pool_identity"],
            confidence=float(row["confidence"]),
            raw_provenance=_loads(row["raw_provenance_json"], {}),
            payload=_loads(row["payload_json"], {}),
            schema_version=str(row["schema_version"]),
        )

    def project(self, event: CanonicalEvent) -> tuple[int | None, int | None]:
        """Project one canonical event into the existing token/candidate authority."""
        token_id: int | None = None
        candidate_id: int | None = None
        if event.event_type != CanonicalEventType.PROVIDER_HEALTH:
            token_id, _ = self.store.upsert_discovery(
                DiscoveryEvent(
                    token_address=event.canonical_token,
                    chain=event.chain,
                    symbol=event.payload.get("symbol"),
                    name=event.payload.get("name"),
                    source=event.source,
                    discovered_at=event.received_timestamp,
                    estimated_creation_timestamp=(
                        event.source_timestamp
                        if event.event_type == CanonicalEventType.TOKEN_CREATED
                        else None
                    ),
                    pair_address=event.pool_identity,
                    deployer=event.payload.get("creator"),
                    metadata={
                        "canonical_event_id": event.event_id,
                        "platform": event.platform,
                        "bonding_curve": event.payload.get("bonding_curve"),
                    },
                )
            )
            candidate_id, _ = self.store.ensure_candidate(
                token_id, event.received_timestamp, "v1.5-runner-failure"
            )
        with self.store._lock, self.store.conn:
            self.store.conn.execute(
                "UPDATE canonical_events SET token_id=? WHERE event_id=?",
                (token_id, event.event_id),
            )
            if token_id is not None:
                self._project_token_state(event, token_id, candidate_id)
                self._project_timeline(event, token_id)
                self._project_actor_and_context(event, token_id)
            if event.event_type == CanonicalEventType.PROVIDER_HEALTH:
                self._project_provider_health(event)
        return token_id, candidate_id

    def _project_token_state(
        self, event: CanonicalEvent, token_id: int, candidate_id: int | None
    ) -> None:
        payload = event.payload
        row = self.store.conn.execute(
            "SELECT * FROM token_realtime_state WHERE token_id=?", (token_id,)
        ).fetchone()
        launched_at = (
            event.source_timestamp
            if event.event_type == CanonicalEventType.TOKEN_CREATED
            else str(row["launched_at"])
            if row
            else event.source_timestamp
        )
        initial_real = payload.get("initial_real_token_reserves")
        if initial_real is None and event.event_type == CanonicalEventType.TOKEN_CREATED:
            initial_real = payload.get("real_token_reserves")
        migration_state = str(row["migration_state"]) if row else "PRE_MIGRATION"
        migration_started = row["migration_started_at"] if row else None
        migration_completed = row["migration_completed_at"] if row else None
        if event.event_type == CanonicalEventType.MIGRATION_STARTED:
            migration_state, migration_started = "MIGRATING", event.source_timestamp
        elif event.event_type in {
            CanonicalEventType.MIGRATION_COMPLETED,
            CanonicalEventType.POOL_CREATED,
        }:
            migration_state, migration_completed = "MIGRATED", event.source_timestamp
        existing_event_at = _timestamp(str(row["last_event_at"])) if row else None
        current_is_latest = existing_event_at is None or _timestamp(event.source_timestamp) >= existing_event_at
        evidence = _loads(row["evidence_json"], {}) if row else {}
        evidence[str(event.event_type)] = {
            "event_id": event.event_id,
            "source": event.source,
            "available_at": event.available_timestamp,
        }
        values = {
            "creator_address": payload.get("creator"),
            "bonding_curve_address": payload.get("bonding_curve"),
            "quote_mint": payload.get("quote_mint"),
            "initial_real_token_reserves": initial_real,
            "latest_real_token_reserves": payload.get("real_token_reserves"),
            "latest_real_quote_reserves": payload.get("real_quote_reserves"),
            "latest_virtual_token_reserves": payload.get("virtual_token_reserves"),
            "latest_virtual_quote_reserves": payload.get("virtual_quote_reserves"),
            "token_total_supply": payload.get("token_total_supply"),
            "curve_complete": payload.get("curve_complete"),
        }
        if row and not current_is_latest:
            for key in list(values):
                values[key] = row[key]
        self.store.conn.execute(
            "INSERT INTO token_realtime_state(token_id,platform,launched_at,creator_address,"
            "bonding_curve_address,quote_mint,initial_real_token_reserves,latest_real_token_reserves,"
            "latest_real_quote_reserves,latest_virtual_token_reserves,latest_virtual_quote_reserves,"
            "token_total_supply,curve_complete,migration_state,migration_started_at,"
            "migration_completed_at,pool_identity,monitoring_temperature,last_event_at,updated_at,"
            "evidence_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(token_id) DO UPDATE SET creator_address=COALESCE(excluded.creator_address,"
            "token_realtime_state.creator_address),bonding_curve_address=COALESCE("
            "excluded.bonding_curve_address,token_realtime_state.bonding_curve_address),"
            "quote_mint=COALESCE(excluded.quote_mint,token_realtime_state.quote_mint),"
            "initial_real_token_reserves=COALESCE(token_realtime_state.initial_real_token_reserves,"
            "excluded.initial_real_token_reserves),latest_real_token_reserves=COALESCE("
            "excluded.latest_real_token_reserves,token_realtime_state.latest_real_token_reserves),"
            "latest_real_quote_reserves=COALESCE(excluded.latest_real_quote_reserves,"
            "token_realtime_state.latest_real_quote_reserves),latest_virtual_token_reserves=COALESCE("
            "excluded.latest_virtual_token_reserves,token_realtime_state.latest_virtual_token_reserves),"
            "latest_virtual_quote_reserves=COALESCE(excluded.latest_virtual_quote_reserves,"
            "token_realtime_state.latest_virtual_quote_reserves),token_total_supply=COALESCE("
            "excluded.token_total_supply,token_realtime_state.token_total_supply),curve_complete=COALESCE("
            "excluded.curve_complete,token_realtime_state.curve_complete),migration_state=excluded.migration_state,"
            "migration_started_at=COALESCE(excluded.migration_started_at,token_realtime_state.migration_started_at),"
            "migration_completed_at=COALESCE(excluded.migration_completed_at,token_realtime_state.migration_completed_at),"
            "pool_identity=COALESCE(excluded.pool_identity,token_realtime_state.pool_identity),"
            "last_event_at=MAX(token_realtime_state.last_event_at,excluded.last_event_at),"
            "updated_at=excluded.updated_at,evidence_json=excluded.evidence_json",
            (
                token_id,
                event.platform,
                launched_at,
                values["creator_address"],
                values["bonding_curve_address"],
                values["quote_mint"],
                values["initial_real_token_reserves"],
                values["latest_real_token_reserves"],
                values["latest_real_quote_reserves"],
                values["latest_virtual_token_reserves"],
                values["latest_virtual_quote_reserves"],
                values["token_total_supply"],
                values["curve_complete"],
                migration_state,
                migration_started,
                migration_completed,
                event.pool_identity or (row["pool_identity"] if row else None),
                row["monitoring_temperature"] if row else "GENESIS",
                event.source_timestamp if current_is_latest else str(row["last_event_at"]),
                iso(),
                _json(evidence),
            ),
        )
        if candidate_id is not None:
            self.store.conn.execute(
                "UPDATE candidates SET last_realtime_event_at=MAX(COALESCE(last_realtime_event_at,''),?),"
                "updated_at=MAX(updated_at,?) WHERE id=?",
                (event.source_timestamp, event.available_timestamp, candidate_id),
            )
        if event.event_type in {
            CanonicalEventType.BONDING_CURVE_STATE,
            CanonicalEventType.BONDING_CURVE_PROGRESS,
        }:
            self._project_curve(event, token_id)
        if event.event_type in {
            CanonicalEventType.MIGRATION_STARTED,
            CanonicalEventType.MIGRATION_COMPLETED,
            CanonicalEventType.POOL_CREATED,
        }:
            self._project_migration(event, token_id)

    def _project_curve(self, event: CanonicalEvent, token_id: int) -> None:
        payload = event.payload
        state = self.store.conn.execute(
            "SELECT initial_real_token_reserves FROM token_realtime_state WHERE token_id=?",
            (token_id,),
        ).fetchone()
        initial = int(state[0]) if state and state[0] is not None else None
        real_token = payload.get("real_token_reserves")
        progress = None
        if initial and real_token is not None:
            progress = max(0.0, min(1.0, (initial - int(real_token)) / initial))
        mode = "LIVE_NATIVE" if event.source.startswith(("solana_", "helius_")) else "LIVE_REDUNDANT"
        self.store.conn.execute(
            "INSERT OR IGNORE INTO curve_observations_v15(event_id,token_id,observed_at,available_at,"
            "slot_or_block,virtual_token_reserves,virtual_quote_reserves,real_token_reserves,"
            "real_quote_reserves,token_total_supply,curve_complete,creator_address,quote_mint,"
            "real_sol_reserves,virtual_sol_reserves,curve_progress,source,evidence_mode,payload_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event.event_id,
                token_id,
                event.source_timestamp,
                event.available_timestamp,
                event.slot_or_block,
                payload.get("virtual_token_reserves"),
                payload.get("virtual_quote_reserves"),
                real_token,
                payload.get("real_quote_reserves"),
                payload.get("token_total_supply"),
                payload.get("curve_complete"),
                payload.get("creator"),
                payload.get("quote_mint"),
                payload.get("real_sol_reserves"),
                payload.get("virtual_sol_reserves"),
                progress,
                event.source,
                mode,
                _json(payload),
            ),
        )

    def _project_timeline(self, event: CanonicalEvent, token_id: int) -> None:
        if event.event_type not in {
            CanonicalEventType.TOKEN_TRADE,
            CanonicalEventType.WALLET_BUY,
            CanonicalEventType.WALLET_SELL,
            CanonicalEventType.CREATOR_ACTIVITY,
            CanonicalEventType.FUNDER_RELATIONSHIP,
            CanonicalEventType.BUNDLE_EVIDENCE,
            CanonicalEventType.WASH_EVIDENCE,
            CanonicalEventType.LIQUIDITY_ADDED,
            CanonicalEventType.LIQUIDITY_REMOVED,
        }:
            return
        payload = event.payload
        side = payload.get("side")
        if side is None and event.event_type == CanonicalEventType.WALLET_BUY:
            side = "buy"
        elif side is None and event.event_type == CanonicalEventType.WALLET_SELL:
            side = "sell"
        self.store.conn.execute(
            "INSERT OR IGNORE INTO token_event_timeline_v15(event_id,token_id,event_type,"
            "event_timestamp,available_timestamp,slot_or_block,transaction_signature,actor,"
            "counterparty,side,quote_amount,token_amount,quote_symbol,creator_linked,funder,"
            "wallet_cluster,jito_tip_lamports,likely_bundled,wash_probability,source,payload_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event.event_id,
                token_id,
                str(event.event_type),
                event.source_timestamp,
                event.available_timestamp,
                event.slot_or_block,
                event.transaction_signature,
                payload.get("actor") or payload.get("user"),
                payload.get("counterparty"),
                side,
                payload.get("sol_amount") or payload.get("quote_amount"),
                payload.get("token_amount"),
                payload.get("quote_symbol") or ("SOL" if payload.get("sol_amount") is not None else None),
                payload.get("creator_linked"),
                payload.get("funder"),
                payload.get("wallet_cluster"),
                payload.get("jito_tip_lamports"),
                payload.get("likely_bundled"),
                payload.get("wash_probability"),
                event.source,
                _json(payload),
            ),
        )

    def _project_migration(self, event: CanonicalEvent, token_id: int) -> None:
        now = iso()
        if event.event_type == CanonicalEventType.MIGRATION_STARTED:
            self.store.conn.execute(
                "INSERT INTO migration_continuity_v15(token_id,migration_timestamp,updated_at) "
                "VALUES(?,?,?) ON CONFLICT(token_id) DO UPDATE SET migration_timestamp=COALESCE("
                "migration_continuity_v15.migration_timestamp,excluded.migration_timestamp),"
                "updated_at=excluded.updated_at",
                (token_id, event.source_timestamp, now),
            )
        else:
            self.store.conn.execute(
                "INSERT INTO migration_continuity_v15(token_id,migration_timestamp,"
                "pool_creation_timestamp,updated_at) VALUES(?,?,?,?) ON CONFLICT(token_id) DO UPDATE SET "
                "migration_timestamp=COALESCE(migration_continuity_v15.migration_timestamp,"
                "excluded.migration_timestamp),pool_creation_timestamp=COALESCE("
                "migration_continuity_v15.pool_creation_timestamp,excluded.pool_creation_timestamp),"
                "updated_at=excluded.updated_at",
                (token_id, event.source_timestamp, event.source_timestamp, now),
            )

    def _project_actor_and_context(self, event: CanonicalEvent, token_id: int) -> None:
        payload = event.payload
        if event.event_type == CanonicalEventType.TOKEN_CREATED and payload.get("creator"):
            creator = str(payload["creator"])
            self.store.conn.execute(
                "INSERT OR IGNORE INTO creator_launches_v14(creator_address,token_id,launched_at,"
                "evidence_json) VALUES(?,?,?,?)",
                (creator, token_id, event.source_timestamp, _json({"event_id": event.event_id})),
            )
        if event.event_type == CanonicalEventType.FUNDER_RELATIONSHIP:
            funded = payload.get("funded_wallet") or payload.get("to_wallet")
            funder = payload.get("funder_wallet") or payload.get("from_wallet")
            if funded and funder:
                self.store.conn.execute(
                    "INSERT INTO wallet_funding_edges_v15(chain,funded_wallet,funder_wallet,"
                    "first_funded_at,last_funded_at,amount_native,transaction_signature,source,"
                    "confidence,evidence_json) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(chain,"
                    "funded_wallet,funder_wallet,first_funded_at) DO UPDATE SET last_funded_at="
                    "MAX(wallet_funding_edges_v15.last_funded_at,excluded.last_funded_at),"
                    "evidence_json=excluded.evidence_json",
                    (
                        event.chain,
                        str(funded),
                        str(funder),
                        event.source_timestamp,
                        event.source_timestamp,
                        payload.get("amount_native"),
                        event.transaction_signature,
                        event.source,
                        event.confidence,
                        _json(payload),
                    ),
                )
        if event.event_type == CanonicalEventType.SOCIAL_OBSERVATION:
            first_trade = self.store.conn.execute(
                "SELECT MIN(event_timestamp) FROM token_event_timeline_v15 WHERE token_id=? "
                "AND event_type='TOKEN_TRADE'",
                (token_id,),
            ).fetchone()[0]
            causal = (
                "SOCIAL_LEADS_PRICE"
                if first_trade and event.source_timestamp < first_trade
                else "SOCIAL_FOLLOWS_PRICE"
                if first_trade and event.source_timestamp > first_trade
                else "SOCIAL_CONFIRMS_PRICE"
                if first_trade
                else "PRICE_SEQUENCE_UNKNOWN"
            )
            self.store.conn.execute(
                "INSERT OR IGNORE INTO social_observations_v15(event_id,token_id,observed_at,"
                "available_at,source,unique_mentioners,mention_count,source_diversity,bot_spam_share,"
                "account_quality,engagement,official_posts,investor_posts,causal_ordering,payload_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event.event_id,
                    token_id,
                    event.source_timestamp,
                    event.available_timestamp,
                    event.source,
                    payload.get("unique_mentioners"),
                    payload.get("mention_count"),
                    payload.get("source_diversity"),
                    payload.get("bot_spam_share"),
                    payload.get("account_quality"),
                    payload.get("engagement"),
                    payload.get("official_posts"),
                    payload.get("investor_posts"),
                    causal,
                    _json(payload),
                ),
            )
        if event.event_type == CanonicalEventType.NARRATIVE_OBSERVATION:
            identity = str(payload.get("narrative_identity") or "UNKNOWN")
            self.store.conn.execute(
                "INSERT OR IGNORE INTO narrative_observations_v15(event_id,token_id,observed_at,"
                "available_at,narrative_identity,leader_token_id,copycat_distance,launch_density,"
                "capital_concentration,capital_fragmentation,velocity,acceleration,saturation,decay,"
                "revival,source,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event.event_id,
                    token_id,
                    event.source_timestamp,
                    event.available_timestamp,
                    identity,
                    payload.get("leader_token_id"),
                    payload.get("copycat_distance"),
                    payload.get("launch_density"),
                    payload.get("capital_concentration"),
                    payload.get("capital_fragmentation"),
                    payload.get("velocity"),
                    payload.get("acceleration"),
                    payload.get("saturation"),
                    payload.get("decay"),
                    payload.get("revival"),
                    event.source,
                    _json(payload),
                ),
            )

    def _project_provider_health(self, event: CanonicalEvent) -> None:
        payload = event.payload
        state = ProviderState(str(payload.get("state") or ProviderState.DEGRADED))
        healthy = state == ProviderState.CONNECTED
        provider = str(payload.get("provider") or event.source)
        error = payload.get("error")
        self.store.conn.execute(
            "INSERT INTO provider_health(provider,healthy,consecutive_failures,last_success_at,"
            "last_failure_at,last_error,updated_at,state,last_message_at,last_valid_event_at,"
            "error_count,rate_limit_count,latency_ms,reconnect_attempts,gap_detected_at,"
            "gap_recovered_at,events_received,credits_used,metadata_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET "
            "healthy=excluded.healthy,consecutive_failures=excluded.consecutive_failures,"
            "last_success_at=COALESCE(excluded.last_success_at,provider_health.last_success_at),"
            "last_failure_at=COALESCE(excluded.last_failure_at,provider_health.last_failure_at),"
            "last_error=excluded.last_error,updated_at=excluded.updated_at,state=excluded.state,"
            "last_message_at=COALESCE(excluded.last_message_at,provider_health.last_message_at),"
            "last_valid_event_at=COALESCE(excluded.last_valid_event_at,provider_health.last_valid_event_at),"
            "error_count=MAX(provider_health.error_count,excluded.error_count),"
            "rate_limit_count=MAX(provider_health.rate_limit_count,excluded.rate_limit_count),"
            "latency_ms=excluded.latency_ms,reconnect_attempts=MAX(provider_health.reconnect_attempts,"
            "excluded.reconnect_attempts),gap_detected_at=COALESCE(excluded.gap_detected_at,"
            "provider_health.gap_detected_at),gap_recovered_at=COALESCE(excluded.gap_recovered_at,"
            "provider_health.gap_recovered_at),events_received=MAX(provider_health.events_received,"
            "excluded.events_received),credits_used=MAX(provider_health.credits_used,excluded.credits_used),"
            "metadata_json=excluded.metadata_json",
            (
                provider,
                int(healthy),
                int(payload.get("consecutive_failures") or 0),
                event.source_timestamp if healthy else None,
                event.source_timestamp if not healthy else None,
                error,
                event.available_timestamp,
                str(state),
                payload.get("last_message_at"),
                payload.get("last_valid_event_at"),
                int(payload.get("error_count") or 0),
                int(payload.get("rate_limit_count") or 0),
                payload.get("latency_ms"),
                int(payload.get("reconnect_attempts") or 0),
                payload.get("gap_detected_at"),
                payload.get("gap_recovered_at"),
                int(payload.get("events_received") or 0),
                float(payload.get("credits_used") or 0),
                _json(payload.get("metadata") or {}),
            ),
        )

    def complete(
        self,
        event_id: str,
        *,
        feature_ready_timestamp: str | None = None,
        model_start_timestamp: str | None = None,
        model_finish_timestamp: str | None = None,
        decision_timestamp: str | None = None,
    ) -> None:
        with self.store._lock, self.store.conn:
            self.store.conn.execute(
                "UPDATE canonical_events SET processing_status='PROCESSED',processing_error=NULL,"
                "claimed_at=NULL,feature_ready_timestamp=COALESCE(?,feature_ready_timestamp),"
                "model_start_timestamp=COALESCE(?,model_start_timestamp),model_finish_timestamp=COALESCE("
                "?,model_finish_timestamp),decision_timestamp=COALESCE(?,decision_timestamp) WHERE event_id=?",
                (
                    feature_ready_timestamp,
                    model_start_timestamp,
                    model_finish_timestamp,
                    decision_timestamp,
                    event_id,
                ),
            )

    def fail(self, event_id: str, error: str, max_attempts: int = 5) -> None:
        with self.store._lock, self.store.conn:
            row = self.store.conn.execute(
                "SELECT processing_attempts FROM canonical_events WHERE event_id=?", (event_id,)
            ).fetchone()
            attempts = int(row[0]) if row else max_attempts
            state = "FAILED" if attempts >= max_attempts else "PENDING"
            self.store.conn.execute(
                "UPDATE canonical_events SET processing_status=?,processing_error=?,claimed_at=NULL "
                "WHERE event_id=?",
                (state, error[:1_000], event_id),
            )

    def reconcile(self) -> dict[str, int]:
        rows = self.store.conn.execute(
            "SELECT COUNT(*) total,SUM(CASE WHEN processing_status='PROCESSED' AND token_id IS NULL "
            "AND event_type!='PROVIDER_HEALTH' THEN 1 ELSE 0 END) orphaned,"
            "SUM(CASE WHEN processing_status='FAILED' THEN 1 ELSE 0 END) failed FROM canonical_events"
        ).fetchone()
        duplicates = self.store.conn.execute(
            "SELECT COUNT(*) FROM (SELECT canonical_key,COUNT(*) n FROM canonical_events "
            "GROUP BY canonical_key HAVING n>1)"
        ).fetchone()[0]
        return {
            "total": int(rows[0] or 0),
            "orphaned": int(rows[1] or 0),
            "failed": int(rows[2] or 0),
            "duplicate_canonical_keys": int(duplicates or 0),
        }

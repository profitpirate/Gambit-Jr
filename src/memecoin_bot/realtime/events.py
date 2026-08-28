from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from memecoin_bot.models import iso


class CanonicalEventType(StrEnum):
    TOKEN_CREATED = "TOKEN_CREATED"
    TOKEN_TRADE = "TOKEN_TRADE"
    BONDING_CURVE_STATE = "BONDING_CURVE_STATE"
    BONDING_CURVE_PROGRESS = "BONDING_CURVE_PROGRESS"
    MIGRATION_STARTED = "MIGRATION_STARTED"
    MIGRATION_COMPLETED = "MIGRATION_COMPLETED"
    POOL_CREATED = "POOL_CREATED"
    LIQUIDITY_ADDED = "LIQUIDITY_ADDED"
    LIQUIDITY_REMOVED = "LIQUIDITY_REMOVED"
    WALLET_BUY = "WALLET_BUY"
    WALLET_SELL = "WALLET_SELL"
    CREATOR_ACTIVITY = "CREATOR_ACTIVITY"
    FUNDER_RELATIONSHIP = "FUNDER_RELATIONSHIP"
    BUNDLE_EVIDENCE = "BUNDLE_EVIDENCE"
    WASH_EVIDENCE = "WASH_EVIDENCE"
    SOCIAL_OBSERVATION = "SOCIAL_OBSERVATION"
    NARRATIVE_OBSERVATION = "NARRATIVE_OBSERVATION"
    PROVIDER_HEALTH = "PROVIDER_HEALTH"


class ProviderState(StrEnum):
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    DISCONNECTED = "DISCONNECTED"
    RECOVERING = "RECOVERING"
    NOT_CONFIGURED = "NOT_CONFIGURED"


def _timestamp(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(UTC)


def _stable_json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True, slots=True)
class CanonicalEvent:
    event_id: str
    event_type: CanonicalEventType
    canonical_token: str
    chain: str
    platform: str
    source: str
    source_timestamp: str
    received_timestamp: str
    available_timestamp: str
    slot_or_block: str | None = None
    transaction_signature: str | None = None
    pool_identity: str | None = None
    confidence: float = 1.0
    raw_provenance: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "canonical-event-v1"
    source_event_id: str | None = None

    @classmethod
    def create(
        cls,
        event_type: CanonicalEventType | str,
        canonical_token: str,
        chain: str,
        platform: str,
        source: str,
        source_timestamp: str,
        *,
        received_timestamp: str | None = None,
        available_timestamp: str | None = None,
        slot_or_block: str | int | None = None,
        transaction_signature: str | None = None,
        pool_identity: str | None = None,
        confidence: float = 1.0,
        raw_provenance: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        schema_version: str = "canonical-event-v1",
        source_event_id: str | None = None,
    ) -> CanonicalEvent:
        kind = CanonicalEventType(event_type)
        received = received_timestamp or iso()
        available = available_timestamp or received
        slot = str(slot_or_block) if slot_or_block not in (None, "") else None
        key = cls.canonical_key_for(
            kind,
            canonical_token,
            chain,
            transaction_signature,
            slot,
            pool_identity,
            source_event_id,
            source_timestamp,
        )
        event_id = hashlib.sha256(key.encode()).hexdigest()
        value = cls(
            event_id=event_id,
            event_type=kind,
            canonical_token=canonical_token,
            chain=chain.lower(),
            platform=platform.lower(),
            source=source,
            source_timestamp=source_timestamp,
            received_timestamp=received,
            available_timestamp=available,
            slot_or_block=slot,
            transaction_signature=transaction_signature,
            pool_identity=pool_identity,
            confidence=float(confidence),
            raw_provenance=dict(raw_provenance or {}),
            payload=dict(payload or {}),
            schema_version=schema_version,
            source_event_id=source_event_id,
        )
        value.validate()
        return value

    @staticmethod
    def canonical_key_for(
        event_type: CanonicalEventType,
        canonical_token: str,
        chain: str,
        transaction_signature: str | None,
        slot_or_block: str | None,
        pool_identity: str | None,
        source_event_id: str | None,
        source_timestamp: str,
    ) -> str:
        token = canonical_token.strip().lower()
        chain_key = chain.strip().lower()
        if event_type == CanonicalEventType.TOKEN_CREATED:
            identity = token
        elif event_type in {
            CanonicalEventType.MIGRATION_STARTED,
            CanonicalEventType.MIGRATION_COMPLETED,
            CanonicalEventType.POOL_CREATED,
        }:
            identity = pool_identity or transaction_signature or f"{token}:{slot_or_block or ''}"
        elif transaction_signature:
            suffix = str((source_event_id or "").rsplit(":", 1)[-1])
            identity = f"{transaction_signature}:{suffix}"
        elif slot_or_block:
            identity = f"{token}:{slot_or_block}:{source_event_id or ''}"
        else:
            identity = source_event_id or f"{token}:{source_timestamp}"
        return "|".join((chain_key, str(event_type), token, str(identity).lower()))

    @property
    def canonical_key(self) -> str:
        return self.canonical_key_for(
            self.event_type,
            self.canonical_token,
            self.chain,
            self.transaction_signature,
            self.slot_or_block,
            self.pool_identity,
            self.source_event_id,
            self.source_timestamp,
        )

    @property
    def provider_latency_ms(self) -> float:
        source = _timestamp(self.source_timestamp, "source_timestamp")
        received = _timestamp(self.received_timestamp, "received_timestamp")
        return max(0.0, (received - source).total_seconds() * 1_000)

    @property
    def availability_latency_ms(self) -> float:
        received = _timestamp(self.received_timestamp, "received_timestamp")
        available = _timestamp(self.available_timestamp, "available_timestamp")
        return max(0.0, (available - received).total_seconds() * 1_000)

    def validate(self) -> None:
        if not self.event_id or len(self.event_id) != 64:
            raise ValueError("event_id must be a deterministic SHA-256 key")
        if not self.canonical_token.strip():
            raise ValueError("canonical_token is required")
        if not self.chain.strip() or not self.platform.strip() or not self.source.strip():
            raise ValueError("chain, platform, and source are required")
        source = _timestamp(self.source_timestamp, "source_timestamp")
        received = _timestamp(self.received_timestamp, "received_timestamp")
        available = _timestamp(self.available_timestamp, "available_timestamp")
        if available < received:
            raise ValueError("available_timestamp cannot precede received_timestamp")
        # Provider clocks can lead chain block time slightly. Preserve that as
        # provenance, but reject impossible hour-scale inversions.
        if source > received and (source - received).total_seconds() > 300:
            raise ValueError("source_timestamp is implausibly far in the future")
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between zero and one")
        if not self.schema_version:
            raise ValueError("schema_version is required")

    def semantic_payload(self) -> dict[str, Any]:
        keys = {
            "actor",
            "side",
            "sol_amount",
            "quote_amount",
            "token_amount",
            "real_sol_reserves",
            "real_quote_reserves",
            "real_token_reserves",
            "virtual_sol_reserves",
            "virtual_quote_reserves",
            "virtual_token_reserves",
            "token_total_supply",
            "curve_complete",
            "creator",
            "bonding_curve",
            "pool",
        }
        return {
            key: self.payload[key]
            for key in sorted(keys)
            if key in self.payload and self.payload[key] is not None
        }

    def semantic_fingerprint(self) -> str:
        return hashlib.sha256(_stable_json(self.semantic_payload()).encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["event_type"] = str(self.event_type)
        return value

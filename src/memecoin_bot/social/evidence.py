from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit, urlunsplit


class SocialLinkClass(StrEnum):
    COMMUNITY = "COMMUNITY"
    PROFILE = "PROFILE"
    OFFICIAL_PROJECT = "OFFICIAL_PROJECT"
    UNKNOWN = "UNKNOWN"
    NONE = "NONE"


@dataclass(frozen=True, slots=True)
class SocialLink:
    token_id: str
    source_platform: str
    source_url: str | None
    classification: SocialLinkClass
    classification_confidence: float
    observed_at: str
    first_seen_at: str
    provenance: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        _utc(self.observed_at)
        first = _utc(self.first_seen_at)
        if first > _utc(self.observed_at):
            raise ValueError("social link first_seen_at cannot follow observed_at")
        if not 0 <= self.classification_confidence <= 1:
            raise ValueError("classification confidence must be between zero and one")


@dataclass(frozen=True, slots=True)
class SocialEvidence:
    token_id: str
    platform: str
    source_type: str
    observed_at: str
    authors: tuple[str, ...] = ()
    mentions: int = 0
    engagement: float | None = None
    velocity: float | None = None
    acceleration: float | None = None
    first_seen: str | None = None
    community_profile_class: SocialLinkClass = SocialLinkClass.UNKNOWN
    confidence: float = 0.5
    provenance: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        observed = _utc(self.observed_at)
        if self.first_seen and _utc(self.first_seen) > observed:
            raise ValueError("social evidence first_seen cannot follow observed_at")
        if self.mentions < 0:
            raise ValueError("social mentions cannot be negative")
        if not 0 <= self.confidence <= 1:
            raise ValueError("social confidence must be between zero and one")
        if not self.token_id or not self.platform or not self.source_type:
            raise ValueError("token, platform and source type are required")

    def dedupe_key(self) -> str:
        """Collapse obvious mirrors only when a shared canonical marker is present."""
        shared = (
            self.provenance.get("canonical_content_sha256")
            or self.provenance.get("canonical_url")
            or self.provenance.get("source_event_id")
        )
        scope = "mirror" if shared else f"{self.platform}:{self.source_type}"
        marker = str(shared or f"{self.observed_at}:{','.join(self.authors)}")
        return hashlib.sha256(f"{self.token_id}|{scope}|{marker}".encode()).hexdigest()


def classify_social_link(
    token_id: str,
    platform: str,
    source_url: str | None,
    observed_at: str,
    *,
    first_seen_at: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> SocialLink:
    """Classify from evidence available at observation time; it is not scoring policy."""
    metadata = metadata or {}
    normalized = _normalize_url(source_url)
    label = " ".join(
        str(metadata.get(name) or "") for name in ("kind", "label", "title", "description")
    ).lower()
    if not normalized:
        classification, confidence = SocialLinkClass.NONE, 1.0
    elif metadata.get("official") is True or any(
        marker in label for marker in ("official project", "official token", "project website")
    ):
        classification, confidence = SocialLinkClass.OFFICIAL_PROJECT, 0.9
    elif any(marker in label for marker in ("community", "group", "channel", "dao")):
        classification, confidence = SocialLinkClass.COMMUNITY, 0.85
    elif any(marker in label for marker in ("profile", "creator", "founder", "personal")):
        classification, confidence = SocialLinkClass.PROFILE, 0.8
    else:
        parts = urlsplit(normalized)
        path = parts.path.lower()
        host = parts.netloc.lower()
        if host in {"t.me", "telegram.me", "discord.gg", "discord.com"}:
            classification, confidence = SocialLinkClass.COMMUNITY, 0.65
        elif "/@" in path or host.endswith(("warpcast.com", "farcaster.xyz")):
            classification, confidence = SocialLinkClass.PROFILE, 0.65
        else:
            classification, confidence = SocialLinkClass.UNKNOWN, 0.35
    result = SocialLink(
        token_id=token_id,
        source_platform=platform.lower(),
        source_url=normalized,
        classification=classification,
        classification_confidence=confidence,
        observed_at=_utc(observed_at).isoformat(),
        first_seen_at=_utc(first_seen_at or observed_at).isoformat(),
        provenance={"point_in_time": True, **metadata},
    )
    result.validate()
    return result


def fuse_social_evidence(items: list[SocialEvidence], *, as_of: str) -> list[SocialEvidence]:
    cutoff = _utc(as_of)
    selected: dict[str, SocialEvidence] = {}
    for item in sorted(items, key=lambda value: value.observed_at):
        item.validate()
        if _utc(item.observed_at) > cutoff:
            continue
        selected.setdefault(item.dedupe_key(), item)
    return list(selected.values())


def social_research_features(
    items: list[SocialEvidence],
    *,
    as_of: str,
    price_move_at: str | None = None,
    first_sell_at: str | None = None,
) -> dict[str, Any]:
    """Generate unweighted hypothesis fields from point-in-time evidence only."""
    evidence = fuse_social_evidence(items, as_of=as_of)
    classes = {item.community_profile_class for item in evidence}
    authors = [author for item in evidence for author in item.authors]
    counts = {author: authors.count(author) for author in set(authors)}
    first = min(
        (_utc(item.first_seen or item.observed_at) for item in evidence),
        default=None,
    )
    observed = sorted(_utc(item.observed_at) for item in evidence)
    hours = (observed[-1] - observed[0]).total_seconds() / 3600 if len(observed) > 1 else 0
    mentions = sum(item.mentions for item in evidence)
    creator_hashes = {
        str(item.provenance["creator_author_hash"])
        for item in evidence
        if item.provenance.get("creator_author_hash")
    }
    creator_mentions = sum(counts.get(value, 0) for value in creator_hashes)
    price_move = _utc(price_move_at) if price_move_at else None
    first_sell = _utc(first_sell_at) if first_sell_at else None
    community_after_sell = sum(
        item.mentions
        for item in evidence
        if item.community_profile_class == SocialLinkClass.COMMUNITY
        and first_sell
        and _utc(item.observed_at) > first_sell
    )
    acceleration_values = [item.acceleration for item in evidence if item.acceleration is not None]
    return {
        "community_linked": SocialLinkClass.COMMUNITY in classes,
        "profile_linked": SocialLinkClass.PROFILE in classes,
        "official_project_linked": SocialLinkClass.OFFICIAL_PROJECT in classes,
        "cross_platform_count": len({item.platform for item in evidence}),
        "unique_social_sources": len(
            {(item.platform, item.source_type) for item in evidence}
        ),
        "unique_authors": len(counts),
        "author_concentration": max(counts.values()) / len(authors) if authors else None,
        "creator_share": creator_mentions / len(authors) if authors else None,
        "mention_velocity": mentions / hours if hours > 0 else None,
        "mention_acceleration": (
            sum(acceleration_values) / len(acceleration_values)
            if acceleration_values
            else None
        ),
        "first_social_mention_at": first.isoformat() if first else None,
        "social_before_price_move": bool(first and price_move and first < price_move),
        "social_after_price_move": bool(first and price_move and first >= price_move),
        "community_persistence_after_first_sell": community_after_sell,
        "hypothesis_only": True,
        "hard_coded_weight": None,
    }


class SocialEvidenceStore:
    def __init__(self, warehouse: Any):
        self.warehouse = warehouse

    def persist_evidence(self, item: SocialEvidence) -> tuple[str, bool]:
        item.validate()
        evidence_id = item.dedupe_key()
        with self.warehouse._lock, self.warehouse.conn:
            cursor = self.warehouse.conn.execute(
                "INSERT OR IGNORE INTO social_evidence_v15("
                "evidence_id,token_id,platform,source_type,observed_at,authors_json,mentions,"
                "engagement,velocity,acceleration,first_seen,community_profile_class,confidence,"
                "provenance_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    evidence_id,
                    item.token_id,
                    item.platform,
                    item.source_type,
                    item.observed_at,
                    json.dumps(item.authors, separators=(",", ":")),
                    item.mentions,
                    item.engagement,
                    item.velocity,
                    item.acceleration,
                    item.first_seen,
                    str(item.community_profile_class),
                    item.confidence,
                    json.dumps(item.provenance, default=str, separators=(",", ":"), sort_keys=True),
                    datetime.now(UTC).isoformat(),
                ),
            )
        return evidence_id, cursor.rowcount == 1

    def persist_link(self, item: SocialLink) -> tuple[str, bool]:
        item.validate()
        payload = asdict(item)
        link_id = hashlib.sha256(
            f"{item.token_id}|{item.source_platform}|{item.source_url or ''}".encode()
        ).hexdigest()
        with self.warehouse._lock, self.warehouse.conn:
            cursor = self.warehouse.conn.execute(
                "INSERT OR IGNORE INTO social_links_v15(link_id,token_id,source_platform,"
                "source_url,classification,classification_confidence,observed_at,first_seen_at,"
                "provenance_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    link_id,
                    item.token_id,
                    item.source_platform,
                    item.source_url,
                    str(item.classification),
                    item.classification_confidence,
                    item.observed_at,
                    item.first_seen_at,
                    json.dumps(payload["provenance"], separators=(",", ":"), sort_keys=True),
                ),
            )
        return link_id, cursor.rowcount == 1


def _normalize_url(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    parts = urlsplit(value.strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError("social timestamps must include timezone")
    return parsed.astimezone(UTC)

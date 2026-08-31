from __future__ import annotations

import hashlib
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from .models import NarrativeMatch, SocialPost

CASHTAG = re.compile(r"(?<!\w)\$([A-Za-z][A-Za-z0-9_]{1,14})")
HASHTAG = re.compile(r"(?<!\w)#([\w]{2,40})", re.UNICODE)
QUOTED = re.compile(r"[\"“‘']([^\"”’']{3,80})[\"”’']")
SOLANA_ADDRESS = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
_STOP = {
    "about", "after", "again", "against", "also", "another", "because", "been", "before", "being", "between", "coin", "crypto", "from", "have", "here", "into", "just", "launch", "like", "market", "more", "new", "news", "official", "only", "pump", "really", "sol", "solana", "that", "their", "there", "they", "this", "token", "trading", "very", "what", "when", "where", "which", "with", "would", "your", "will", "today", "tomorrow", "soon", "live", "going", "make", "made", "first", "best", "good", "great", "look", "watch",
}
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def normalize(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value or "")
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    return " ".join(word.lower() for word in _TOKEN.findall(folded))


def _eligible_word(word: str) -> bool:
    return len(word) >= 3 and word not in _STOP and not word.isdigit() and not SOLANA_ADDRESS.fullmatch(word)


def phrase_keys(text: str, *, max_keys: int = 96) -> set[str]:
    normalized = normalize(text)
    words = [word for word in normalized.split() if _eligible_word(word)]
    output: set[str] = set()
    for tag in CASHTAG.findall(text or ""):
        key = normalize(tag)
        if key and key not in _STOP:
            output.add(key)
    for tag in HASHTAG.findall(text or ""):
        key = normalize(tag)
        if key and key not in _STOP:
            output.add(key)
    for value in QUOTED.findall(text or ""):
        key = normalize(value)
        if 1 <= len(key.split()) <= 6:
            output.add(key)
    output.update(words)
    for width in (2, 3):
        for index in range(max(0, len(words) - width + 1)):
            phrase = " ".join(words[index:index + width])
            if len(phrase) <= 56:
                output.add(phrase)
    return set(sorted(output, key=lambda value: (-len(value.split()), -len(value), value))[:max_keys])


def token_keys(name: str | None, symbol: str | None, description: str | None = None) -> set[str]:
    output: set[str] = set()
    for value in (name, symbol):
        key = normalize(value or "")
        if key and key not in _STOP:
            output.add(key)
            parts = [item for item in key.split() if _eligible_word(item)]
            output.update(parts)
            if 1 < len(parts) <= 4:
                output.add(" ".join(parts))
    if description:
        output.update(key for key in phrase_keys(description, max_keys=24) if len(key.split()) >= 2)
    return output


@dataclass(slots=True)
class _NarrativeRecord:
    key: str
    first_seen_ns: int
    last_seen_ns: int
    expires_ns: int
    authors: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)
    max_authority: float = 0.0
    max_followers: int = 0
    engagement: float = 0.0
    post_ids: set[str] = field(default_factory=set)

    def score(self, now_ns: int) -> float:
        age_seconds = max(0.0, (now_ns - self.last_seen_ns) / 1_000_000_000)
        freshness = max(0.0, 1.0 - age_seconds / 1_800.0)
        diversity = min(1.0, len(self.authors) / 3.0)
        follower_component = min(1.0, self.max_followers / 1_000_000.0)
        engagement_component = min(1.0, self.engagement / 50_000.0)
        return min(0.995, 0.52 * self.max_authority + 0.18 * diversity + 0.12 * follower_component + 0.08 * engagement_component + 0.10 * freshness)


class ActiveNarrativeCache:
    """Pre-launch social/narrative evidence with O(1) launch-time matching."""

    def __init__(self, *, ttl_seconds: float = 1_800.0, minimum_authority: float = 0.55, exact_single_source_authority: float = 0.88, decision_threshold: float = 0.76) -> None:
        self.ttl_ns = max(1, int(ttl_seconds * 1_000_000_000))
        self.minimum_authority = minimum_authority
        self.exact_single_source_authority = exact_single_source_authority
        self.decision_threshold = decision_threshold
        self._lock = threading.Lock()
        self._records: Mapping[str, _NarrativeRecord] = MappingProxyType({})

    def observe(self, post: SocialPost) -> int:
        authority = max(0.0, min(1.0, float(post.authority)))
        if authority < self.minimum_authority:
            return 0
        keys = phrase_keys(post.text)
        if not keys:
            return 0
        with self._lock:
            now = post.received_ns or time.time_ns()
            replacement = {key: record for key, record in self._records.items() if record.expires_ns > now}
            for key in keys:
                record = replacement.get(key)
                if record is None:
                    record = _NarrativeRecord(key=key, first_seen_ns=post.created_ns, last_seen_ns=post.received_ns, expires_ns=post.received_ns + self.ttl_ns)
                    replacement[key] = record
                record.last_seen_ns = max(record.last_seen_ns, post.received_ns)
                record.expires_ns = max(record.expires_ns, post.received_ns + self.ttl_ns)
                record.authors.add(post.author_handle.lower() or post.author_id)
                record.sources.add(post.platform)
                record.max_authority = max(record.max_authority, authority)
                record.max_followers = max(record.max_followers, int(post.followers))
                record.engagement = max(record.engagement, float(post.engagement))
                record.post_ids.add(post.post_id)
            self._records = MappingProxyType(replacement)
        return len(keys)

    def match(self, *, name: str | None, symbol: str | None, description: str | None = None, now_ns: int | None = None) -> NarrativeMatch:
        now = now_ns or time.time_ns()
        candidates = token_keys(name, symbol, description)
        if not candidates:
            return NarrativeMatch(False, 0.0, evidence={"reason": "NO_TOKEN_KEYS"})
        snapshot = self._records
        matches: list[tuple[float, bool, _NarrativeRecord]] = []
        exact_name = normalize(name or "")
        exact_symbol = normalize(symbol or "")
        for key in candidates:
            record = snapshot.get(key)
            if record is None or record.expires_ns <= now:
                continue
            exact = key in {exact_name, exact_symbol}
            score = record.score(now)
            if exact:
                score = min(0.995, score + 0.08)
            matches.append((score, exact, record))
        if not matches:
            return NarrativeMatch(False, 0.0, evidence={"reason": "NO_ACTIVE_MATCH"})
        score, exact, record = max(matches, key=lambda item: (item[0], item[1], item[2].last_seen_ns))
        source_count = len(record.authors)
        allowed = score >= self.decision_threshold and (source_count >= 2 or (exact and record.max_authority >= self.exact_single_source_authority))
        return NarrativeMatch(
            matched=allowed,
            score=score,
            key=record.key,
            source_count=source_count,
            authority=record.max_authority,
            age_ms=max(0.0, (now - record.last_seen_ns) / 1_000_000),
            exact=exact,
            evidence={
                "authors": sorted(record.authors),
                "platforms": sorted(record.sources),
                "followers": record.max_followers,
                "engagement": record.engagement,
                "post_ids_sha256": hashlib.sha256(",".join(sorted(record.post_ids)).encode()).hexdigest(),
                "candidate_keys": sorted(candidates),
            },
        )

    def prune(self, now_ns: int | None = None) -> int:
        now = now_ns or time.time_ns()
        with self._lock:
            before = len(self._records)
            self._records = MappingProxyType({key: record for key, record in self._records.items() if record.expires_ns > now})
            return before - len(self._records)

    def size(self) -> int:
        return len(self._records)

    def active_keys(self) -> tuple[str, ...]:
        return tuple(self._records)

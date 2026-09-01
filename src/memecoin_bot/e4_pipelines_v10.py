from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import queue
import re
import threading
import time
import unicodedata
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from enum import IntEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Sequence
from urllib.parse import urlparse

LOGGER = logging.getLogger("gambit.e4.pipelines.v10")

E4_WALLET = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"
HOT_PATH_BUDGET_NS = 36_000_000

_BASE58_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
_CASHTAG_RE = re.compile(r"(?<!\w)\$([A-Za-z][A-Za-z0-9]{1,14})(?!\w)")
_HASHTAG_RE = re.compile(r"(?<!\w)#([A-Za-z][A-Za-z0-9_]{1,31})(?!\w)")
_WORD_RE = re.compile(r"[a-z0-9]+")

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "for",
        "from",
        "has",
        "have",
        "he",
        "her",
        "here",
        "his",
        "i",
        "in",
        "is",
        "it",
        "its",
        "just",
        "me",
        "my",
        "new",
        "not",
        "of",
        "on",
        "or",
        "our",
        "out",
        "she",
        "so",
        "that",
        "the",
        "their",
        "them",
        "there",
        "they",
        "this",
        "to",
        "up",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "with",
        "you",
        "your",
        "coin",
        "token",
        "crypto",
        "solana",
        "pump",
        "launch",
        "launched",
        "launching",
        "live",
        "today",
        "now",
        "official",
    }
)


class CreatorTier(IntEnum):
    NEGATIVE = 0
    UNKNOWN = 1
    WATCH = 2
    APPROVED = 3
    ELITE = 4


@dataclass(frozen=True, slots=True)
class CreatorProfile:
    creator: str
    tier: CreatorTier
    score: float
    wins: int = 0
    losses: int = 0
    trades: int = 0
    gross_win_rate: float = 0.0
    gross_pnl_sol: float = 0.0
    source: str = "unknown"
    evidence: tuple[str, ...] = ()
    common_social_handles: tuple[str, ...] = ()
    common_metadata_hosts: tuple[str, ...] = ()
    median_peak_market_cap_usd: float | None = None
    max_peak_market_cap_usd: float | None = None
    updated_ns: int = 0

    @property
    def approved(self) -> bool:
        return self.tier >= CreatorTier.APPROVED

    @property
    def negative(self) -> bool:
        return self.tier == CreatorTier.NEGATIVE


@dataclass(frozen=True, slots=True)
class CreatorSnapshot:
    profiles: Mapping[str, CreatorProfile]
    loaded_ns: int
    version: str


@dataclass(frozen=True, slots=True)
class SocialSignal:
    signal_id: str
    source: str
    source_account: str
    text: str
    created_ns: int
    observed_ns: int
    expires_ns: int
    authority: float
    novelty: float
    engagement_velocity: float
    phrases: tuple[str, ...]
    terms: tuple[str, ...]
    contract_addresses: tuple[str, ...]
    provenance: str = "stream"


@dataclass(frozen=True, slots=True)
class NarrativeMatch:
    matched: bool
    score: float
    signal_ids: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    matched_phrases: tuple[str, ...] = ()
    reason: str = "no prelaunch narrative match"
    age_ms: float | None = None


@dataclass(frozen=True, slots=True)
class E4Observation:
    observation_id: str
    mint: str
    creator: str | None
    signature: str | None
    observed_ns: int
    slot: int | None = None
    sol_amount: float | None = None
    fdv_usd: float | None = None
    source: str = "pump-trade-event"


@dataclass(frozen=True, slots=True)
class LaunchIntent:
    intent_id: str
    creator: str
    mint: str | None
    issued_ns: int
    expires_ns: int
    max_buy_sol: float | None
    source: str
    nonce: str


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _score01(value: Any) -> float:
    number = _finite(value)
    if number > 1.0:
        number /= 100.0
    return min(1.0, max(0.0, number))


def _integer(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "approved", "elite"}


def _normalise_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return " ".join(_WORD_RE.findall(text.lower()))


def _normalise_handle(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "x.com/" in text or "twitter.com/" in text:
        try:
            text = urlparse(text).path.strip("/").split("/", 1)[0]
        except Exception:
            pass
    return text.lower().lstrip("@").strip()


def _sequence(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Sequence):
        return ()
    return tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOGGER.exception("Could not read E4 pipeline model path=%s", path)
        return None


def _tier_from_record(record: Mapping[str, Any], *, discovered: bool = False) -> CreatorTier:
    status = str(record.get("status") or record.get("tier") or "").strip().upper()
    if status in {"NEGATIVE", "BLOCKED", "BLACKLIST", "REJECT"}:
        return CreatorTier.NEGATIVE
    if status in {"ELITE", "AUTO", "IMMEDIATE"}:
        return CreatorTier.ELITE
    if status in {"APPROVED", "STRONG", "PROMOTED"}:
        return CreatorTier.APPROVED
    if status in {"WATCH", "PROBATION"}:
        return CreatorTier.WATCH

    wins = _integer(record.get("wins") or record.get("e4_observed_wins"))
    losses = _integer(record.get("losses"))
    trades = max(wins + losses, _integer(record.get("trades")))
    rate = _score01(record.get("gross_win_rate") if record.get("gross_win_rate") is not None else (wins / trades if trades else 0.0))
    if trades >= 3 and rate <= 0.25:
        return CreatorTier.NEGATIVE
    if trades >= 3 and wins >= 2 and rate >= 0.75:
        return CreatorTier.ELITE
    if trades >= 3 and wins >= 2 and rate >= 0.60:
        return CreatorTier.APPROVED

    max_peak = _finite(
        record.get("max_peak_market_cap_usd")
        or record.get("best_market_cap_usd")
        or record.get("max_market_cap_usd")
    )
    profitable_launches = _integer(record.get("profitable_launches") or record.get("runner_count"))
    if discovered and (max_peak >= 30_000 or profitable_launches >= 2):
        return CreatorTier.APPROVED
    return CreatorTier.WATCH if discovered or trades else CreatorTier.UNKNOWN


def _profile_from_record(creator: str, record: Mapping[str, Any], source: str) -> CreatorProfile:
    wins = _integer(record.get("wins") or record.get("e4_observed_wins"))
    losses = _integer(record.get("losses"))
    trades = max(wins + losses, _integer(record.get("trades")))
    rate = _score01(record.get("gross_win_rate") if record.get("gross_win_rate") is not None else (wins / trades if trades else 0.0))
    tier = _tier_from_record(record, discovered=source != "e4-history")
    score = _score01(record.get("score"))
    if score <= 0:
        score = {
            CreatorTier.NEGATIVE: 0.05,
            CreatorTier.UNKNOWN: 0.0,
            CreatorTier.WATCH: 0.58,
            CreatorTier.APPROVED: 0.82,
            CreatorTier.ELITE: 0.94,
        }[tier]
        if trades:
            score = min(0.99, max(score, 0.52 + 0.42 * rate + 0.03 * math.log1p(trades)))
    evidence = list(_sequence(record.get("evidence")))
    if wins or losses:
        evidence.append(f"e4:{wins}W/{losses}L")
    return CreatorProfile(
        creator=creator,
        tier=tier,
        score=score,
        wins=wins,
        losses=losses,
        trades=trades,
        gross_win_rate=rate,
        gross_pnl_sol=_finite(record.get("gross_pnl_sol") or record.get("winning_pnl_sol") or record.get("e4_gross_pnl_sol")),
        source=source,
        evidence=tuple(dict.fromkeys(evidence)),
        common_social_handles=tuple(_normalise_handle(item) for item in _sequence(record.get("common_social_handles") or record.get("social_handles")) if _normalise_handle(item)),
        common_metadata_hosts=_sequence(record.get("common_metadata_hosts") or record.get("metadata_hosts")),
        median_peak_market_cap_usd=(
            _finite(record.get("median_peak_market_cap_usd"), float("nan"))
            if record.get("median_peak_market_cap_usd") is not None
            else None
        ),
        max_peak_market_cap_usd=(
            _finite(record.get("max_peak_market_cap_usd") or record.get("best_market_cap_usd"), float("nan"))
            if (record.get("max_peak_market_cap_usd") is not None or record.get("best_market_cap_usd") is not None)
            else None
        ),
        updated_ns=_integer(record.get("updated_ns")) or time.time_ns(),
    )


def _merge_profiles(left: CreatorProfile, right: CreatorProfile) -> CreatorProfile:
    # E4 outcome history has veto authority; otherwise preserve the strongest
    # independent evidence while retaining provenance from both datasets.
    if left.negative or right.negative:
        negative = left if left.negative else right
        positive = right if left.negative else left
        if negative.trades >= 3 and negative.gross_win_rate <= 0.25:
            tier = CreatorTier.NEGATIVE
        else:
            tier = max(left.tier, right.tier)
    else:
        tier = max(left.tier, right.tier)
    wins = max(left.wins, right.wins)
    losses = max(left.losses, right.losses)
    trades = max(left.trades, right.trades, wins + losses)
    rate = wins / trades if trades else max(left.gross_win_rate, right.gross_win_rate)
    return CreatorProfile(
        creator=left.creator,
        tier=tier,
        score=max(left.score, right.score),
        wins=wins,
        losses=losses,
        trades=trades,
        gross_win_rate=rate,
        gross_pnl_sol=max(left.gross_pnl_sol, right.gross_pnl_sol),
        source="+".join(dict.fromkeys((left.source + "+" + right.source).split("+"))),
        evidence=tuple(dict.fromkeys(left.evidence + right.evidence)),
        common_social_handles=tuple(dict.fromkeys(left.common_social_handles + right.common_social_handles)),
        common_metadata_hosts=tuple(dict.fromkeys(left.common_metadata_hosts + right.common_metadata_hosts)),
        median_peak_market_cap_usd=right.median_peak_market_cap_usd or left.median_peak_market_cap_usd,
        max_peak_market_cap_usd=max(
            left.max_peak_market_cap_usd or 0.0,
            right.max_peak_market_cap_usd or 0.0,
        )
        or None,
        updated_ns=max(left.updated_ns, right.updated_ns),
    )


class CreatorRegistry:
    """Atomically reloadable, lock-free-on-read creator expectancy registry."""

    def __init__(
        self,
        expectancy_path: Path | None = None,
        discovered_path: Path | None = None,
        operator_path: Path | None = None,
    ) -> None:
        self.expectancy_path = expectancy_path or Path(
            os.getenv("E4_CREATOR_EXPECTANCY_PATH", "models/e4/e4-creator-expectancy.json")
        )
        self.discovered_path = discovered_path or Path(
            os.getenv("E4_DISCOVERED_CREATORS_PATH", "models/e4/e4-discovered-creators.json")
        )
        self.operator_path = operator_path or Path(
            os.getenv("E4_OPERATOR_CLUSTERS_PATH", "models/e4/e4-operator-clusters.json")
        )
        self._snapshot = CreatorSnapshot(MappingProxyType({}), time.time_ns(), "empty")
        self._reload_lock = threading.Lock()
        self.reload()

    @property
    def snapshot(self) -> CreatorSnapshot:
        return self._snapshot

    def lookup(self, creator: str | None) -> CreatorProfile | None:
        if not creator:
            return None
        return self._snapshot.profiles.get(str(creator))

    def reload(self) -> CreatorSnapshot:
        with self._reload_lock:
            profiles: dict[str, CreatorProfile] = {}
            versions: list[str] = []
            expectancy = _read_json(self.expectancy_path)
            if isinstance(expectancy, Mapping):
                versions.append(str(expectancy.get("version") or "expectancy"))
                records = expectancy.get("creators")
                if isinstance(records, Mapping):
                    iterator = records.items()
                else:
                    iterator = (
                        (str(row.get("creator") or ""), row)
                        for row in expectancy.get("top_creators") or []
                        if isinstance(row, Mapping)
                    )
                for creator, record in iterator:
                    creator = str(creator or "")
                    if not creator or not isinstance(record, Mapping):
                        continue
                    profiles[creator] = _profile_from_record(creator, record, "e4-history")

            discovered = _read_json(self.discovered_path)
            if isinstance(discovered, Mapping):
                versions.append(str(discovered.get("version") or "discovered"))
                records = discovered.get("creators") or discovered.get("profiles") or discovered.get("approved") or []
                if isinstance(records, Mapping):
                    iterator = records.items()
                else:
                    iterator = (
                        (str(row.get("creator") or row.get("wallet") or ""), row)
                        for row in records
                        if isinstance(row, Mapping)
                    )
                for creator, record in iterator:
                    creator = str(creator or "")
                    if not creator or not isinstance(record, Mapping):
                        continue
                    candidate = _profile_from_record(creator, record, "external-runner-history")
                    profiles[creator] = _merge_profiles(profiles[creator], candidate) if creator in profiles else candidate

            operators = _read_json(self.operator_path)
            if isinstance(operators, Mapping):
                versions.append(str(operators.get("version") or "operators"))
                creator_to_cluster = operators.get("creator_to_cluster") or {}
                clusters = operators.get("clusters") or {}
                if isinstance(creator_to_cluster, Mapping) and isinstance(clusters, Mapping):
                    for creator, cluster_id in creator_to_cluster.items():
                        record = clusters.get(str(cluster_id))
                        if not isinstance(record, Mapping):
                            continue
                        operator_tier = _tier_from_record(record, discovered=True)
                        operator_score = _score01(record.get("score"))
                        if creator in profiles:
                            current = profiles[str(creator)]
                            candidate = CreatorProfile(
                                creator=str(creator),
                                tier=operator_tier,
                                score=operator_score,
                                source="operator-cluster",
                                evidence=(f"operator:{cluster_id}",),
                                updated_ns=time.time_ns(),
                            )
                            profiles[str(creator)] = _merge_profiles(current, candidate)

            self._snapshot = CreatorSnapshot(
                MappingProxyType(dict(profiles)),
                time.time_ns(),
                "+".join(versions) or "empty",
            )
            LOGGER.info(
                "E4 creator registry loaded profiles=%d elite=%d approved=%d negative=%d",
                len(profiles),
                sum(row.tier == CreatorTier.ELITE for row in profiles.values()),
                sum(row.tier == CreatorTier.APPROVED for row in profiles.values()),
                sum(row.tier == CreatorTier.NEGATIVE for row in profiles.values()),
            )
            return self._snapshot


class NarrativeCache:
    """Short-lived prelaunch narrative index with lock-free launch matching."""

    def __init__(self, ttl_seconds: float | None = None, max_signals: int = 4096) -> None:
        self.ttl_ns = int((ttl_seconds or _finite(os.getenv("E4_NARRATIVE_TTL_SECONDS"), 1_800.0)) * 1e9)
        self.max_signals = max(128, int(max_signals))
        self._signals: Mapping[str, SocialSignal] = MappingProxyType({})
        self._term_index: Mapping[str, tuple[str, ...]] = MappingProxyType({})
        self._phrase_counts: Counter[str] = Counter()
        self._source_last_seen: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    @property
    def size(self) -> int:
        return len(self._signals)

    @staticmethod
    def _terms(text: str) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                word
                for word in _normalise_text(text).split()
                if len(word) >= 3 and word not in _STOPWORDS and not word.isdigit()
            )
        )

    @classmethod
    def _phrases(cls, text: str) -> tuple[str, ...]:
        terms = list(cls._terms(text))
        phrases: list[str] = []
        phrases.extend(_normalise_text(tag) for tag in _CASHTAG_RE.findall(text))
        phrases.extend(_normalise_text(tag) for tag in _HASHTAG_RE.findall(text))
        for length in (4, 3, 2):
            for index in range(0, max(0, len(terms) - length + 1)):
                phrase = " ".join(terms[index : index + length])
                if len(phrase) >= 8:
                    phrases.append(phrase)
        phrases.extend(term for term in terms if len(term) >= 5)
        return tuple(dict.fromkeys(phrase for phrase in phrases if phrase and phrase not in _STOPWORDS))

    def observe(
        self,
        *,
        source: str,
        source_account: str,
        text: str,
        created_ns: int | None = None,
        observed_ns: int | None = None,
        authority: float = 0.0,
        engagement_velocity: float = 0.0,
        provenance: str = "stream",
    ) -> SocialSignal | None:
        observed_ns = int(observed_ns or time.time_ns())
        created_ns = int(created_ns or observed_ns)
        # Future-dated or stale posts cannot become prelaunch authority.
        if created_ns > observed_ns + 5_000_000_000 or observed_ns - created_ns > self.ttl_ns:
            return None
        phrases = self._phrases(text)
        terms = self._terms(text)
        contracts = tuple(dict.fromkeys(_BASE58_RE.findall(text)))
        if not phrases and not contracts:
            return None
        account = _normalise_handle(source_account) or str(source_account or source).lower()
        key = f"{source}|{account}|{created_ns}|{text}".encode("utf-8", "ignore")
        signal_id = hashlib.blake2s(key, digest_size=12).hexdigest()
        with self._lock:
            existing = self._signals.get(signal_id)
            if existing is not None:
                return existing
            now = observed_ns
            live = {
                identifier: signal
                for identifier, signal in self._signals.items()
                if signal.expires_ns >= now
            }
            if len(live) >= self.max_signals:
                ordered = sorted(live.values(), key=lambda row: row.created_ns, reverse=True)[: self.max_signals - 1]
                live = {row.signal_id: row for row in ordered}
            novelty_values = [1.0 / (1.0 + self._phrase_counts[phrase]) for phrase in phrases]
            novelty = statistics_mean(novelty_values, 1.0)
            authority = _score01(authority)
            engagement = min(1.0, max(0.0, _finite(engagement_velocity)))
            signal = SocialSignal(
                signal_id=signal_id,
                source=str(source),
                source_account=account,
                text=str(text),
                created_ns=created_ns,
                observed_ns=observed_ns,
                expires_ns=created_ns + self.ttl_ns,
                authority=authority,
                novelty=novelty,
                engagement_velocity=engagement,
                phrases=phrases,
                terms=terms,
                contract_addresses=contracts,
                provenance=provenance,
            )
            live[signal_id] = signal
            for phrase in phrases:
                self._phrase_counts[phrase] += 1
            index: dict[str, list[str]] = {}
            for identifier, row in live.items():
                for term in row.terms:
                    index.setdefault(term, []).append(identifier)
                for contract in row.contract_addresses:
                    index.setdefault(contract.lower(), []).append(identifier)
            self._signals = MappingProxyType(live)
            self._term_index = MappingProxyType(
                {term: tuple(dict.fromkeys(identifiers)) for term, identifiers in index.items()}
            )
            self._source_last_seen[(str(source), account)] = observed_ns
            return signal

    def match_launch(
        self,
        *,
        name: str | None,
        symbol: str | None,
        uri: str | None,
        mint: str | None,
        launch_ns: int,
    ) -> NarrativeMatch:
        started = time.perf_counter_ns()
        name_norm = _normalise_text(name)
        symbol_norm = _normalise_text(symbol)
        uri_norm = _normalise_text(uri)
        launch_terms = tuple(
            dict.fromkeys(
                word
                for word in (name_norm + " " + symbol_norm + " " + uri_norm).split()
                if len(word) >= 3 and word not in _STOPWORDS
            )
        )
        candidate_ids: set[str] = set()
        for term in launch_terms:
            candidate_ids.update(self._term_index.get(term, ()))
        if mint:
            candidate_ids.update(self._term_index.get(str(mint).lower(), ()))
        if not candidate_ids:
            return NarrativeMatch(False, 0.0, reason="no indexed prelaunch phrase")

        scored: list[tuple[float, SocialSignal, tuple[str, ...]]] = []
        for identifier in candidate_ids:
            signal = self._signals.get(identifier)
            if signal is None or signal.created_ns > launch_ns or signal.expires_ns < launch_ns:
                continue
            exact_contract = bool(mint and mint in signal.contract_addresses)
            matched_phrases = tuple(
                phrase
                for phrase in signal.phrases
                if phrase == name_norm
                or phrase == symbol_norm
                or (phrase and phrase in name_norm)
                or (name_norm and name_norm in phrase)
            )
            overlap = len(set(launch_terms) & set(signal.terms)) / max(1, len(set(launch_terms)))
            exact_symbol = bool(symbol_norm and symbol_norm in signal.phrases)
            exact_name = bool(name_norm and name_norm in signal.phrases)
            semantic = max(overlap, 1.0 if exact_name else 0.0, 0.92 if exact_symbol else 0.0)
            if exact_contract:
                semantic = 1.0
            age_seconds = max(0.0, (launch_ns - signal.created_ns) / 1e9)
            recency = math.exp(-age_seconds / max(30.0, self.ttl_ns / 1e9 / 4.0))
            score = (
                0.34 * signal.authority
                + 0.19 * signal.novelty
                + 0.13 * signal.engagement_velocity
                + 0.22 * semantic
                + 0.12 * recency
            )
            if exact_contract:
                score = max(score, 0.98)
            if semantic >= 0.70:
                scored.append((score, signal, matched_phrases))
        if not scored:
            return NarrativeMatch(False, 0.0, reason="prelaunch posts did not match launch strongly enough")
        scored.sort(key=lambda row: row[0], reverse=True)
        top_score, top, phrases = scored[0]
        qualifying = [row for row in scored if row[0] >= max(0.60, top_score - 0.10)]
        independent_sources = {row[1].source_account for row in qualifying}
        if len(independent_sources) >= 2:
            top_score = min(1.0, top_score + 0.06)
        threshold = _finite(os.getenv("E4_SOCIAL_MATCH_THRESHOLD"), 0.76)
        elapsed = time.perf_counter_ns() - started
        return NarrativeMatch(
            matched=top_score >= threshold,
            score=top_score,
            signal_ids=tuple(row[1].signal_id for row in qualifying[:8]),
            sources=tuple(sorted(independent_sources)),
            matched_phrases=tuple(dict.fromkeys(phrase for row in qualifying for phrase in row[2])),
            reason=(
                f"prelaunch narrative score={top_score:.3f} sources={len(independent_sources)} latency_ns={elapsed}"
                if top_score >= threshold
                else f"narrative score below threshold: {top_score:.3f}"
            ),
            age_ms=(launch_ns - top.created_ns) / 1e6,
        )


def statistics_mean(values: Sequence[float], default: float = 0.0) -> float:
    return sum(values) / len(values) if values else default


class E4Learner:
    """Non-blocking E4 teacher queue; enrichment always runs off the entry path."""

    def __init__(self, journal_path: Path | None = None) -> None:
        self.journal_path = journal_path or Path(
            os.getenv("E4_TEACHER_JOURNAL", "var/e4/e4-teacher-observations.jsonl")
        )
        self._queue: queue.SimpleQueue[E4Observation | None] = queue.SimpleQueue()
        self._callbacks: list[Callable[[E4Observation], None]] = []
        self._seen: set[str] = set()
        self._seen_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._latest_by_mint: Mapping[str, E4Observation] = MappingProxyType({})

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, name="e4-teacher", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout=timeout)

    def register_callback(self, callback: Callable[[E4Observation], None]) -> None:
        self._callbacks.append(callback)

    def observe(self, observation: E4Observation) -> bool:
        with self._seen_lock:
            if observation.observation_id in self._seen:
                return False
            self._seen.add(observation.observation_id)
            if len(self._seen) > 100_000:
                self._seen = set(list(self._seen)[-50_000:])
            latest = dict(self._latest_by_mint)
            latest[observation.mint] = observation
            if len(latest) > 10_000:
                latest = dict(sorted(latest.items(), key=lambda row: row[1].observed_ns, reverse=True)[:5_000])
            self._latest_by_mint = MappingProxyType(latest)
        self.start()
        self._queue.put(observation)
        return True

    def latest(self, mint: str) -> E4Observation | None:
        return self._latest_by_mint.get(mint)

    def _worker(self) -> None:
        while not self._stop.is_set():
            observation = self._queue.get()
            if observation is None:
                continue
            try:
                self.journal_path.parent.mkdir(parents=True, exist_ok=True)
                with self.journal_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(asdict(observation), separators=(",", ":")) + "\n")
                for callback in tuple(self._callbacks):
                    try:
                        callback(observation)
                    except Exception:
                        LOGGER.exception("E4 learner callback failed mint=%s", observation.mint)
            except Exception:
                LOGGER.exception("E4 learner journal write failed")


class LaunchIntentRegistry:
    """Authenticated, single-use prelaunch intents for cooperating deployers."""

    def __init__(self) -> None:
        self._by_creator: Mapping[str, tuple[LaunchIntent, ...]] = MappingProxyType({})
        self._used: set[str] = set()
        self._lock = threading.Lock()

    @staticmethod
    def _canonical(payload: Mapping[str, Any]) -> bytes:
        clean = {str(key): value for key, value in payload.items() if key not in {"signature", "hmac"}}
        return json.dumps(clean, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def ingest(self, payload: Mapping[str, Any], *, secret: str | None = None) -> LaunchIntent:
        secret = secret or os.getenv("E4_LAUNCH_INTENT_SECRET")
        if not secret:
            raise ValueError("E4_LAUNCH_INTENT_SECRET is required")
        supplied = str(payload.get("signature") or payload.get("hmac") or "")
        expected = hmac.new(secret.encode("utf-8"), self._canonical(payload), hashlib.sha256).hexdigest()
        if not supplied or not hmac.compare_digest(supplied.lower(), expected.lower()):
            raise ValueError("invalid launch-intent HMAC")
        creator = str(payload.get("creator") or "")
        if not creator:
            raise ValueError("launch intent requires creator")
        now = time.time_ns()
        issued_ns = _integer(payload.get("issued_ns")) or now
        expires_ns = _integer(payload.get("expires_ns"))
        if expires_ns <= now or expires_ns - issued_ns > 86_400_000_000_000:
            raise ValueError("launch intent is expired or has excessive lifetime")
        nonce = str(payload.get("nonce") or "")
        if not nonce:
            raise ValueError("launch intent requires nonce")
        intent_id = hashlib.blake2s(self._canonical(payload), digest_size=16).hexdigest()
        intent = LaunchIntent(
            intent_id=intent_id,
            creator=creator,
            mint=str(payload.get("mint") or "") or None,
            issued_ns=issued_ns,
            expires_ns=expires_ns,
            max_buy_sol=(
                _finite(payload.get("max_buy_sol")) if payload.get("max_buy_sol") is not None else None
            ),
            source=str(payload.get("source") or "authorized-deployer"),
            nonce=nonce,
        )
        with self._lock:
            current = {key: tuple(value) for key, value in self._by_creator.items()}
            current[creator] = tuple(
                row for row in current.get(creator, ()) if row.expires_ns >= now and row.intent_id not in self._used
            ) + (intent,)
            self._by_creator = MappingProxyType(current)
        return intent

    def match(self, creator: str | None, mint: str | None, now_ns: int | None = None) -> LaunchIntent | None:
        if not creator:
            return None
        now_ns = int(now_ns or time.time_ns())
        for intent in self._by_creator.get(str(creator), ()):
            if intent.intent_id in self._used or intent.expires_ns < now_ns:
                continue
            if intent.mint and mint and intent.mint != mint:
                continue
            return intent
        return None

    def consume(self, intent_id: str) -> None:
        with self._lock:
            self._used.add(intent_id)


@dataclass(slots=True)
class PipelineMetrics:
    creator_lookups: int = 0
    creator_hits: int = 0
    social_matches: int = 0
    e4_observations: int = 0
    hot_path_samples_ns: deque[int] = field(default_factory=lambda: deque(maxlen=100_000))

    def record_hot_path(self, elapsed_ns: int) -> None:
        self.hot_path_samples_ns.append(int(elapsed_ns))

    def snapshot(self) -> dict[str, Any]:
        values = sorted(self.hot_path_samples_ns)
        percentile = lambda q: values[min(len(values) - 1, int((len(values) - 1) * q))] if values else None
        return {
            "creator_lookups": self.creator_lookups,
            "creator_hits": self.creator_hits,
            "social_matches": self.social_matches,
            "e4_observations": self.e4_observations,
            "hot_path_samples": len(values),
            "hot_path_p50_ns": percentile(0.50),
            "hot_path_p95_ns": percentile(0.95),
            "hot_path_p99_ns": percentile(0.99),
            "hot_path_max_ns": values[-1] if values else None,
            "budget_ns": HOT_PATH_BUDGET_NS,
        }


class PipelineRuntime:
    def __init__(self) -> None:
        self.creators = CreatorRegistry()
        self.narratives = NarrativeCache()
        self.teacher = E4Learner()
        self.intents = LaunchIntentRegistry()
        self.metrics = PipelineMetrics()
        self._launch_context: MutableMapping[str, dict[str, Any]] = {}
        self._context_lock = threading.Lock()

    def creator_profile(self, creator: str | None) -> CreatorProfile | None:
        started = time.perf_counter_ns()
        self.metrics.creator_lookups += 1
        profile = self.creators.lookup(creator)
        if profile is not None:
            self.metrics.creator_hits += 1
        self.metrics.record_hot_path(time.perf_counter_ns() - started)
        return profile

    def observe_social_post(self, **kwargs: Any) -> SocialSignal | None:
        return self.narratives.observe(**kwargs)

    def observe_launch(
        self,
        *,
        mint: str,
        creator: str | None,
        name: str | None,
        symbol: str | None,
        uri: str | None,
        launch_ns: int,
        metadata_host: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter_ns()
        profile = self.creator_profile(creator)
        narrative = self.narratives.match_launch(
            name=name,
            symbol=symbol,
            uri=uri,
            mint=mint,
            launch_ns=launch_ns,
        )
        if narrative.matched:
            self.metrics.social_matches += 1
        intent = self.intents.match(creator, mint, launch_ns)
        context = {
            "mint": mint,
            "creator": creator,
            "name": name,
            "symbol": symbol,
            "uri": uri,
            "metadata_host": metadata_host or (urlparse(uri).netloc.lower() if uri else ""),
            "launch_ns": launch_ns,
            "creator_profile": profile,
            "narrative_match": narrative,
            "launch_intent": intent,
            "prearmed": intent is not None,
        }
        with self._context_lock:
            self._launch_context[mint] = context
            if len(self._launch_context) > 50_000:
                oldest = sorted(self._launch_context.items(), key=lambda row: row[1].get("launch_ns", 0))[:10_000]
                for key, _ in oldest:
                    self._launch_context.pop(key, None)
        self.metrics.record_hot_path(time.perf_counter_ns() - started)
        return context

    def context(self, mint: str) -> Mapping[str, Any]:
        return self._launch_context.get(mint, MappingProxyType({}))

    def observe_e4_buy(
        self,
        *,
        mint: str,
        creator: str | None,
        signature: str | None,
        observed_ns: int,
        slot: int | None = None,
        sol_amount: float | None = None,
        fdv_usd: float | None = None,
        source: str = "pump-trade-event",
    ) -> E4Observation:
        observation_id = hashlib.blake2s(
            f"{signature or ''}|{mint}|{slot or 0}".encode("utf-8"), digest_size=16
        ).hexdigest()
        observation = E4Observation(
            observation_id=observation_id,
            mint=mint,
            creator=creator,
            signature=signature,
            observed_ns=observed_ns,
            slot=slot,
            sol_amount=sol_amount,
            fdv_usd=fdv_usd,
            source=source,
        )
        if self.teacher.observe(observation):
            self.metrics.e4_observations += 1
        with self._context_lock:
            context = self._launch_context.setdefault(mint, {"mint": mint, "creator": creator})
            context["e4_observation"] = observation
            context["e4_confirmed"] = True
        return observation


_RUNTIME: PipelineRuntime | None = None
_RUNTIME_LOCK = threading.Lock()


def get_runtime() -> PipelineRuntime:
    global _RUNTIME
    if _RUNTIME is not None:
        return _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            _RUNTIME = PipelineRuntime()
    return _RUNTIME


def reset_runtime_for_tests(runtime: PipelineRuntime | None = None) -> PipelineRuntime:
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is not None:
            _RUNTIME.teacher.stop()
        _RUNTIME = runtime or PipelineRuntime()
        return _RUNTIME


# V11_CANONICAL_PIPELINE_API
# The V10 primitives above remain reusable and directly tested. V11 exposes one
# canonical manager for production and compatibility with the earlier engine.
from .e4_pipeline_manager_v11 import PipelineManager  # noqa: E402
manager = PipelineManager()  # canonical same-process V11 authority

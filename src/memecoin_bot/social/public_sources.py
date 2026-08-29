from __future__ import annotations

import hashlib
import html
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode

import aiohttp

from .evidence import SocialEvidence, SocialLinkClass

JsonRequester = Callable[..., Awaitable[tuple[Any, float]]]
TextRequester = Callable[..., Awaitable[tuple[str, float]]]


class PublicPreviewUnavailable(RuntimeError):
    pass


async def request_json(
    url: str,
    timeout: float,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[Any, float]:
    begun = time.perf_counter()
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    async with (
        aiohttp.ClientSession(timeout=client_timeout) as session,
        session.get(url, headers=headers) as response,
    ):
        response.raise_for_status()
        payload = await response.json()
    return payload, (time.perf_counter() - begun) * 1000


async def request_text(
    url: str,
    timeout: float,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[str, float]:
    begun = time.perf_counter()
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    async with (
        aiohttp.ClientSession(timeout=client_timeout) as session,
        session.get(url, headers=headers) as response,
    ):
        response.raise_for_status()
        payload = await response.text()
    return payload, (time.perf_counter() - begun) * 1000


class NeynarFarcasterClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.neynar.com/v2/farcaster",
        requester: JsonRequester = request_json,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.requester = requester

    async def search(
        self, token_id: str, query: str, timeout: float, *, limit: int = 10
    ) -> tuple[SocialEvidence, float]:
        parameters = urlencode({"q": query, "limit": min(max(limit, 1), 100)})
        payload, latency = await self.requester(
            f"{self.base_url}/cast/search/?{parameters}",
            timeout,
            headers={"x-api-key": self.api_key},
        )
        result = payload.get("result", payload) if isinstance(payload, dict) else {}
        casts = result.get("casts", []) if isinstance(result, dict) else []
        if not isinstance(casts, list):
            raise TypeError("Neynar search response omitted casts list")
        timestamps = [_timestamp(cast.get("timestamp")) for cast in casts if cast.get("timestamp")]
        authors = tuple(
            dict.fromkeys(
                _hash(str((cast.get("author") or {}).get("fid")))
                for cast in casts
                if (cast.get("author") or {}).get("fid") is not None
            )
        )
        engagement = sum(_cast_engagement(cast) for cast in casts)
        observed = max(timestamps, default=datetime.now(UTC))
        first = min(timestamps, default=observed)
        velocity = _velocity(len(casts), first, observed)
        evidence = SocialEvidence(
            token_id=token_id,
            platform="farcaster",
            source_type="neynar_cast_search",
            observed_at=observed.isoformat(),
            authors=authors,
            mentions=len(casts),
            engagement=engagement,
            velocity=velocity,
            first_seen=first.isoformat(),
            community_profile_class=SocialLinkClass.UNKNOWN,
            confidence=0.7 if casts else 0.4,
            provenance={
                "query_sha256": _hash(query.lower()),
                "raw_content_persisted": False,
                "author_concentration": _concentration(authors),
                "credential_redacted": True,
                "point_in_time": True,
            },
        )
        evidence.validate()
        return evidence, latency


class YouTubeResearchClient:
    def __init__(
        self,
        api_key: str,
        *,
        cache_ttl_seconds: float = 21_600,
        maximum_searches: int = 8,
        base_url: str = "https://www.googleapis.com/youtube/v3",
        requester: JsonRequester = request_json,
    ):
        self.api_key = api_key
        self.cache_ttl_seconds = max(float(cache_ttl_seconds), 60)
        self.maximum_searches = max(int(maximum_searches), 1)
        self.base_url = base_url.rstrip("/")
        self.requester = requester
        self.searches_used = 0
        self._cache: dict[str, tuple[float, SocialEvidence, tuple[float, ...]]] = {}

    async def search(
        self,
        token_id: str,
        query: str,
        timeout: float,
        *,
        high_priority: bool = False,
        max_results: int = 5,
    ) -> tuple[SocialEvidence | None, tuple[float, ...]]:
        if not high_priority:
            return None, ()
        cache_key = _hash(f"{token_id}|{query.lower().strip()}|{max_results}")
        cached = self._cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < self.cache_ttl_seconds:
            return cached[1], cached[2]
        if self.searches_used >= self.maximum_searches:
            raise RuntimeError("YouTube search budget exhausted for this process")
        self.searches_used += 1
        parameters = urlencode(
            {
                "part": "snippet",
                "type": "video",
                "order": "date",
                "maxResults": min(max(int(max_results), 1), 10),
                "q": query,
                "key": self.api_key,
            }
        )
        payload, search_latency = await self.requester(
            f"{self.base_url}/search?{parameters}", timeout
        )
        items = payload.get("items", []) if isinstance(payload, dict) else []
        if not isinstance(items, list):
            raise TypeError("YouTube search response omitted items")
        ids = [str((item.get("id") or {}).get("videoId")) for item in items]
        ids = [value for value in ids if value and value != "None"]
        statistics: dict[str, dict[str, Any]] = {}
        stats_latency = 0.0
        if ids:
            detail_query = urlencode(
                {"part": "statistics,snippet", "id": ",".join(ids), "key": self.api_key}
            )
            details, stats_latency = await self.requester(
                f"{self.base_url}/videos?{detail_query}", timeout
            )
            detail_items = details.get("items", []) if isinstance(details, dict) else []
            statistics = {
                str(item.get("id")): item for item in detail_items if isinstance(item, dict)
            }
        timestamps = [
            _timestamp((item.get("snippet") or {}).get("publishedAt"))
            for item in items
            if (item.get("snippet") or {}).get("publishedAt")
        ]
        channels = tuple(
            dict.fromkeys(
                _hash(str((item.get("snippet") or {}).get("channelId")))
                for item in items
                if (item.get("snippet") or {}).get("channelId")
            )
        )
        views = comments = 0
        for item in statistics.values():
            values = item.get("statistics") or {}
            views += _integer(values.get("viewCount"))
            comments += _integer(values.get("commentCount"))
        observed = max(timestamps, default=datetime.now(UTC))
        first = min(timestamps, default=observed)
        evidence = SocialEvidence(
            token_id=token_id,
            platform="youtube",
            source_type="youtube_high_priority_search",
            observed_at=observed.isoformat(),
            authors=channels,
            mentions=len(items),
            engagement=float(views + comments),
            velocity=_velocity(len(items), first, observed),
            first_seen=first.isoformat(),
            community_profile_class=SocialLinkClass.UNKNOWN,
            confidence=0.65 if items else 0.35,
            provenance={
                "query_sha256": _hash(query.lower()),
                "unique_channels": len(channels),
                "view_count": views,
                "comment_count": comments,
                "publication_recency_observed": bool(timestamps),
                "raw_content_persisted": False,
                "credential_redacted": True,
                "high_priority_only": True,
                "point_in_time": True,
            },
        )
        evidence.validate()
        latencies = (search_latency,) + ((stats_latency,) if ids else ())
        self._cache[cache_key] = (time.monotonic(), evidence, latencies)
        return evidence, latencies


class TelegramPublicWebClient:
    def __init__(
        self,
        *,
        requester: TextRequester = request_text,
        minimum_interval_seconds: float = 1,
    ):
        self.requester = requester
        self.minimum_interval_seconds = max(float(minimum_interval_seconds), 0)
        self._last_request_at = 0.0

    async def search_channel(
        self, channel: str, token_id: str, query: str, timeout: float
    ) -> tuple[SocialEvidence, float]:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.minimum_interval_seconds:
            await _sleep(self.minimum_interval_seconds - elapsed)
        safe_channel = re.sub(r"[^A-Za-z0-9_]", "", channel.lstrip("@"))
        if not safe_channel:
            raise ValueError("Telegram public channel name is invalid")
        page, latency = await self.requester(
            f"https://t.me/s/{quote(safe_channel)}",
            timeout,
            headers={"User-Agent": "Gambit-Jr/1.5 public-research"},
        )
        self._last_request_at = time.monotonic()
        return self.parse_public_preview(page, safe_channel, token_id, query), latency

    @staticmethod
    def parse_public_preview(
        page: str, channel: str, token_id: str, query: str
    ) -> SocialEvidence:
        if "data-post=" not in page:
            raise PublicPreviewUnavailable("Telegram public web preview is unavailable")
        blocks = re.split(r'<div class="tgme_widget_message_wrap', page)[1:]
        matches: list[tuple[datetime, str, int]] = []
        needle = query.casefold()
        for block in blocks:
            text_match = re.search(
                r'tgme_widget_message_text[^>]*>(.*?)</div>', block, re.DOTALL
            )
            timestamp_match = re.search(r'<time[^>]+datetime="([^"]+)"', block)
            if not text_match or not timestamp_match:
                continue
            visible = html.unescape(re.sub(r"<[^>]+>", " ", text_match.group(1)))
            if needle and needle not in visible.casefold():
                continue
            views_match = re.search(r'tgme_widget_message_views">([^<]+)', block)
            matches.append(
                (_timestamp(timestamp_match.group(1)), visible, _compact_number(views_match.group(1) if views_match else "0"))
            )
        observed = max((value[0] for value in matches), default=datetime.now(UTC))
        first = min((value[0] for value in matches), default=observed)
        evidence = SocialEvidence(
            token_id=token_id,
            platform="telegram",
            source_type="telegram_public_web",
            observed_at=observed.isoformat(),
            authors=(_hash(channel.lower()),) if matches else (),
            mentions=len(matches),
            engagement=float(sum(value[2] for value in matches)),
            velocity=_velocity(len(matches), first, observed),
            first_seen=first.isoformat(),
            community_profile_class=SocialLinkClass.COMMUNITY,
            confidence=0.65 if matches else 0.35,
            provenance={
                "channel_hash": _hash(channel.lower()),
                "query_sha256": _hash(query.lower()),
                "public_preview": True,
                "visible_authors_limited": True,
                "raw_content_persisted": False,
                "point_in_time": True,
            },
        )
        evidence.validate()
        return evidence


class MastodonPublicClient:
    def __init__(
        self,
        instance_urls: Iterable[str],
        *,
        access_token: str | None = None,
        requester: JsonRequester = request_json,
    ):
        self.instance_urls = tuple(
            dict.fromkeys(value.rstrip("/") for value in instance_urls if value.startswith("http"))
        )
        if not self.instance_urls:
            raise ValueError("at least one Mastodon instance URL is required")
        self.access_token = access_token
        self.requester = requester

    async def search(
        self, token_id: str, query: str, timeout: float
    ) -> tuple[SocialEvidence, tuple[float, ...], int]:
        headers = {"Authorization": f"Bearer {self.access_token}"} if self.access_token else None
        errors = 0
        latencies = []
        for instance in self.instance_urls:
            try:
                search = {"q": query, "limit": 10}
                if self.access_token:
                    search["type"] = "statuses"
                parameters = urlencode(search)
                payload, latency = await self.requester(
                    f"{instance}/api/v2/search?{parameters}", timeout, headers=headers
                )
                latencies.append(latency)
                statuses = payload.get("statuses", []) if isinstance(payload, dict) else []
                if not isinstance(statuses, list):
                    raise TypeError("Mastodon search response omitted statuses")
                accounts = payload.get("accounts", []) if isinstance(payload, dict) else []
                hashtags = payload.get("hashtags", []) if isinstance(payload, dict) else []
                if not isinstance(accounts, list) or not isinstance(hashtags, list):
                    raise TypeError("Mastodon search response omitted public result lists")
                timestamps = [
                    _timestamp(item.get("created_at"))
                    for item in statuses
                    if item.get("created_at")
                ]
                observed = max(timestamps, default=datetime.now(UTC))
                first = min(timestamps, default=observed)
                authors = tuple(
                    dict.fromkeys(
                        _hash(str((item.get("account") or {}).get("id")))
                        for item in statuses
                        if (item.get("account") or {}).get("id") is not None
                    )
                )
                public_profiles = tuple(
                    _hash(str(item.get("id")))
                    for item in accounts
                    if isinstance(item, dict) and item.get("id") is not None
                )
                authors = tuple(dict.fromkeys((*authors, *public_profiles)))
                mentions = len(statuses) + len(accounts) + len(hashtags)
                evidence = SocialEvidence(
                    token_id=token_id,
                    platform="mastodon",
                    source_type="mastodon_public_search",
                    observed_at=observed.isoformat(),
                    authors=authors,
                    mentions=mentions,
                    engagement=float(
                        sum(
                            _integer(item.get("reblogs_count"))
                            + _integer(item.get("favourites_count"))
                            + _integer(item.get("replies_count"))
                            for item in statuses
                        )
                    ),
                    velocity=_velocity(mentions, first, observed),
                    first_seen=first.isoformat(),
                    community_profile_class=SocialLinkClass.UNKNOWN,
                    confidence=0.55 if statuses else 0.3,
                    provenance={
                        "instance_host_hash": _hash(instance.lower()),
                        "query_sha256": _hash(query.lower()),
                        "raw_content_persisted": False,
                        "public_accounts": len(accounts),
                        "public_hashtags": len(hashtags),
                        "access_token_used": bool(self.access_token),
                        "credential_redacted": True,
                        "point_in_time": True,
                    },
                )
                evidence.validate()
                return evidence, tuple(latencies), errors
            except (aiohttp.ClientError, TimeoutError, TypeError, ValueError, KeyError):
                errors += 1
        raise PublicPreviewUnavailable("all configured Mastodon instances were unavailable")


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


def _cast_engagement(cast: dict[str, Any]) -> float:
    reactions = cast.get("reactions") or {}
    replies = cast.get("replies") or {}
    return float(
        _integer(reactions.get("likes_count"))
        + _integer(reactions.get("recasts_count"))
        + _integer(replies.get("count"))
    )


def _timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError("social timestamps must include timezone")
    return parsed.astimezone(UTC)


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _compact_number(value: str) -> int:
    text = value.strip().replace(",", "").upper()
    multiplier = 1_000_000 if text.endswith("M") else 1_000 if text.endswith("K") else 1
    try:
        return int(float(text.rstrip("KM")) * multiplier)
    except ValueError:
        return 0


def _velocity(count: int, first: datetime, observed: datetime) -> float | None:
    hours = (observed - first).total_seconds() / 3600
    return count / hours if hours > 0 else None


def _concentration(authors: tuple[str, ...]) -> float | None:
    if not authors:
        return None
    counts = [authors.count(author) for author in set(authors)]
    return max(counts) / len(authors)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import aiohttp

from .models import SocialPost
from .narrative import ActiveNarrativeCache

LOGGER = logging.getLogger("gambit.e4.pipeline.x_stream")
X_API_BASE = "https://api.x.com/2"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOGGER.exception("Unable to load X authority configuration path=%s", path)
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


@dataclass(frozen=True, slots=True)
class XAccount:
    handle: str
    authority: float
    followers: int = 0
    enabled: bool = True


class XAccountRegistry:
    def __init__(self, path: Path) -> None:
        payload = _read_json(path)
        raw = payload.get("accounts") or payload.get("handles") or {}
        accounts: dict[str, XAccount] = {}
        if isinstance(raw, Mapping):
            for handle, value in raw.items():
                if isinstance(value, Mapping):
                    score = float(value.get("authority", value.get("score", 0.0)) or 0.0)
                    followers = int(value.get("followers") or 0)
                    enabled = bool(value.get("enabled", True))
                else:
                    score = float(value or 0.0)
                    followers = 0
                    enabled = True
                normalized = str(handle).lower().lstrip("@")
                if normalized:
                    accounts[normalized] = XAccount(normalized, max(0.0, min(1.0, score)), followers, enabled)
        for handle in (item.strip().lower().lstrip("@") for item in os.getenv("E4_X_TRACKED_ACCOUNTS", "").split(",")):
            if handle:
                accounts.setdefault(handle, XAccount(handle, 0.75))
        self.accounts = accounts

    def enabled_handles(self) -> tuple[str, ...]:
        return tuple(sorted(handle for handle, row in self.accounts.items() if row.enabled))

    def authority(self, handle: str, followers: int = 0, verified: bool = False) -> float:
        normalized = handle.lower().lstrip("@")
        configured = self.accounts.get(normalized)
        if configured:
            return configured.authority
        follower_score = min(0.82, max(0.0, followers) / 1_000_000.0)
        return min(0.88, follower_score + (0.06 if verified else 0.0))


class XFilteredStream:
    """Official X filtered stream; launch matching is local and pre-cached."""

    tag_prefix = "gambit-e4-v10"

    def __init__(self, *, bearer_token: str, accounts: XAccountRegistry, cache: ActiveNarrativeCache, api_base: str = X_API_BASE) -> None:
        self.bearer_token = bearer_token.strip()
        self.accounts = accounts
        self.cache = cache
        self.api_base = api_base.rstrip("/")
        self.posts_seen = 0
        self.posts_accepted = 0
        self.reconnects = 0
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.bearer_token and self.accounts.enabled_handles())

    def headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.bearer_token}", "content-type": "application/json", "user-agent": "Gambit-E4-V10/1.0"}

    def desired_rules(self) -> list[dict[str, str]]:
        handles = list(self.accounts.enabled_handles())
        chunk_size = max(1, min(20, int(os.getenv("E4_X_RULE_ACCOUNT_CHUNK", "16"))))
        rules = []
        for index in range(0, len(handles), chunk_size):
            chunk = handles[index:index + chunk_size]
            rules.append({"value": "(" + " OR ".join(f"from:{handle}" for handle in chunk) + ") -is:retweet", "tag": f"{self.tag_prefix}:{index // chunk_size}"})
        return rules

    async def reconcile_rules(self, session: aiohttp.ClientSession) -> None:
        if not self.enabled or os.getenv("E4_X_MANAGE_RULES", "true").lower() in {"0", "false", "no", "off"}:
            return
        url = f"{self.api_base}/tweets/search/stream/rules"
        async with session.get(url, headers=self.headers()) as response:
            payload = await response.json(content_type=None)
            if response.status >= 400:
                raise RuntimeError(f"X rules GET HTTP {response.status}: {payload}")
        ours = [str(item.get("id")) for item in payload.get("data") or [] if str(item.get("tag") or "").startswith(self.tag_prefix)]
        if ours:
            async with session.post(url, headers=self.headers(), json={"delete": {"ids": ours}}) as response:
                body = await response.text()
                if response.status >= 400:
                    raise RuntimeError(f"X rules delete HTTP {response.status}: {body[:500]}")
        desired = self.desired_rules()
        if desired:
            async with session.post(url, headers=self.headers(), json={"add": desired}) as response:
                body = await response.text()
                if response.status >= 400:
                    raise RuntimeError(f"X rules add HTTP {response.status}: {body[:500]}")

    def parse_payload(self, payload: Mapping[str, Any], received_ns: int | None = None) -> SocialPost | None:
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return None
        includes = payload.get("includes")
        users: dict[str, Mapping[str, Any]] = {}
        if isinstance(includes, Mapping):
            for user in includes.get("users") or ():
                if isinstance(user, Mapping):
                    users[str(user.get("id") or "")] = user
        author_id = str(data.get("author_id") or "")
        user = users.get(author_id, {})
        handle = str(user.get("username") or author_id).lower().lstrip("@")
        configured = self.accounts.accounts.get(handle)
        if configured is None or not configured.enabled:
            return None
        metrics = data.get("public_metrics") if isinstance(data.get("public_metrics"), Mapping) else {}
        user_metrics = user.get("public_metrics") if isinstance(user.get("public_metrics"), Mapping) else {}
        followers = int(user_metrics.get("followers_count") or configured.followers or 0)
        engagement = float(sum(float(metrics.get(key) or 0) for key in ("like_count", "retweet_count", "reply_count", "quote_count", "bookmark_count")))
        created = str(data.get("created_at") or "")
        try:
            from datetime import datetime
            created_ns = int(datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() * 1_000_000_000)
        except (TypeError, ValueError):
            created_ns = received_ns or time.time_ns()
        return SocialPost(
            post_id=str(data.get("id") or ""), author_id=author_id, author_handle=handle,
            text=str(data.get("text") or ""), created_ns=created_ns,
            received_ns=received_ns or time.time_ns(),
            authority=self.accounts.authority(handle, followers, bool(user.get("verified"))),
            followers=followers, engagement=engagement, platform="x",
        )

    async def run(self, stop: asyncio.Event) -> None:
        if not self.enabled:
            return
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=90)
        backoff = 1.0
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                await self.reconcile_rules(session)
            except Exception:
                LOGGER.exception("X filtered-stream rule reconciliation failed")
            while not stop.is_set():
                params = {
                    "tweet.fields": "created_at,author_id,public_metrics,lang,entities",
                    "expansions": "author_id",
                    "user.fields": "username,verified,public_metrics",
                }
                try:
                    async with session.get(f"{self.api_base}/tweets/search/stream", params=params, headers=self.headers()) as response:
                        if response.status >= 400:
                            body = await response.text()
                            raise RuntimeError(f"X stream HTTP {response.status}: {body[:500]}")
                        backoff = 1.0
                        async for raw in response.content:
                            if stop.is_set():
                                break
                            line = raw.strip()
                            if not line:
                                continue
                            self.posts_seen += 1
                            try:
                                payload = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            post = self.parse_payload(payload)
                            if post is not None:
                                self.posts_accepted += 1
                                self.cache.observe(post)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    self.reconnects += 1
                    LOGGER.warning("X filtered stream disconnected: %s", self.last_error)
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=backoff)
                    except asyncio.TimeoutError:
                        pass
                    backoff = min(60.0, backoff * 2.0)

    def stats(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "tracked_accounts": len(self.accounts.enabled_handles()), "posts_seen": self.posts_seen, "posts_accepted": self.posts_accepted, "reconnects": self.reconnects, "last_error": self.last_error}

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import socket
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Sequence

import aiohttp


def read_accounts(path: Path) -> list[str]:
    if not path.exists():
        return []
    output: list[str] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip().split()[0].lstrip("@") if line.strip() else ""
        if value and value.lower() not in seen:
            seen.add(value.lower())
            output.append(value)
    return output


def chunks(values: Sequence[str], max_query_chars: int) -> list[list[str]]:
    groups: list[list[str]] = []
    current: list[str] = []
    length = 0
    for value in values:
        clause = f"from:{value}"
        addition = len(clause) + (4 if current else 0)
        if current and length + addition > max_query_chars:
            groups.append(current)
            current = []
            length = 0
        current.append(value)
        length += len(clause) + (4 if len(current) > 1 else 0)
    if current:
        groups.append(current)
    return groups


def authority(followers: int, configured_score: float = 0.0, verified: bool = False) -> float:
    follower_component = min(1.0, max(0.0, math.log10(max(1, followers)) / 6.0))
    # Verification is supporting evidence, not automatic maximum authority.
    verification_bonus = 0.04 if verified else 0.0
    return min(1.0, max(configured_score, follower_component + verification_bonus))


def timestamp_ns(value: object) -> int:
    if isinstance(value, (int, float)):
        number = int(value)
        return number if number > 10_000_000_000_000 else number * 1_000_000_000
    text = str(value or "").strip()
    if not text:
        return time.time_ns()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1_000_000_000)
    except ValueError:
        return time.time_ns()


class NoveltyTracker:
    def __init__(self, max_posts: int = 20_000) -> None:
        self.seen_terms: Counter[str] = Counter()
        self.posts: deque[tuple[str, ...]] = deque(maxlen=max_posts)

    def score(self, text: str) -> float:
        terms = tuple(
            token.lower().strip("$#@.,:;!?()[]{}\"'")
            for token in text.split()
            if len(token.strip("$#@.,:;!?()[]{}\"'")) >= 3
        )
        terms = tuple(term for term in terms if term)
        if not terms:
            return 0.0
        rarity = sum(1.0 / (1.0 + self.seen_terms[term]) for term in set(terms)) / len(set(terms))
        if len(self.posts) == self.posts.maxlen:
            old = self.posts[0]
            for term in set(old):
                self.seen_terms[term] = max(0, self.seen_terms[term] - 1)
        self.posts.append(terms)
        for term in set(terms):
            self.seen_terms[term] += 1
        return min(1.0, max(0.05, rarity))


class UdpPublisher:
    def __init__(self, host: str, port: int) -> None:
        self.address = (host, port)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def publish(self, payload: Mapping[str, Any]) -> None:
        encoded = json.dumps(dict(payload), separators=(",", ":")).encode("utf-8")
        if len(encoded) > 60_000:
            raise ValueError("social signal exceeds UDP envelope")
        self.socket.sendto(encoded, self.address)


async def json_lines(response: aiohttp.ClientResponse) -> AsyncIterator[dict[str, Any]]:
    buffer = b""
    async for chunk in response.content.iter_any():
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, Mapping):
                yield dict(payload)


async def sync_rules(
    session: aiohttp.ClientSession,
    rules_url: str,
    token: str,
    accounts: Sequence[str],
    max_query_chars: int,
) -> None:
    if not accounts:
        return
    headers = {"Authorization": f"Bearer {token}", "content-type": "application/json"}
    async with session.get(rules_url, headers=headers) as response:
        if response.status >= 400:
            raise RuntimeError(f"rules GET failed HTTP {response.status}: {(await response.text())[:300]}")
        existing = await response.json(content_type=None)
    existing_rows = existing.get("data") if isinstance(existing, Mapping) else []
    ids = [str(row.get("id")) for row in existing_rows or [] if isinstance(row, Mapping) and row.get("id")]
    if ids:
        async with session.post(rules_url, headers=headers, json={"delete": {"ids": ids}}) as response:
            if response.status >= 400:
                raise RuntimeError(f"rules DELETE failed HTTP {response.status}: {(await response.text())[:300]}")
    additions = [
        {
            "value": " OR ".join(f"from:{account}" for account in group),
            "tag": f"gambit-e4-{index}",
        }
        for index, group in enumerate(chunks(accounts, max_query_chars), start=1)
    ]
    for start in range(0, len(additions), 25):
        async with session.post(rules_url, headers=headers, json={"add": additions[start : start + 25]}) as response:
            if response.status >= 400:
                raise RuntimeError(f"rules ADD failed HTTP {response.status}: {(await response.text())[:300]}")


async def stream(args: argparse.Namespace) -> int:
    accounts = read_accounts(args.accounts)
    if not accounts:
        raise RuntimeError(f"no monitored X accounts found in {args.accounts}")
    if not args.bearer_token:
        raise RuntimeError("X bearer token is required")
    publisher = UdpPublisher(args.udp_host, args.udp_port)
    novelty = NoveltyTracker(args.novelty_window)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=90)
    connector = aiohttp.TCPConnector(limit=8, ttl_dns_cache=600, keepalive_timeout=60)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        if args.sync_rules:
            await sync_rules(
                session,
                args.rules_url,
                args.bearer_token,
                accounts,
                args.max_rule_chars,
            )
        headers = {"Authorization": f"Bearer {args.bearer_token}", "Accept": "application/json"}
        params = {
            "tweet.fields": "author_id,created_at,public_metrics,entities,lang",
            "expansions": "author_id",
            "user.fields": "username,verified,public_metrics",
        }
        reconnects = 0
        while True:
            try:
                async with session.get(args.stream_url, headers=headers, params=params) as response:
                    if response.status >= 400:
                        raise RuntimeError(f"stream HTTP {response.status}: {(await response.text())[:300]}")
                    async for envelope in json_lines(response):
                        data = envelope.get("data") if isinstance(envelope.get("data"), Mapping) else {}
                        text = str(data.get("text") or "")
                        if not text:
                            continue
                        includes = envelope.get("includes") if isinstance(envelope.get("includes"), Mapping) else {}
                        users = includes.get("users") if isinstance(includes.get("users"), list) else []
                        user = next(
                            (row for row in users if isinstance(row, Mapping) and str(row.get("id")) == str(data.get("author_id"))),
                            users[0] if users and isinstance(users[0], Mapping) else {},
                        )
                        metrics = data.get("public_metrics") if isinstance(data.get("public_metrics"), Mapping) else {}
                        user_metrics = user.get("public_metrics") if isinstance(user.get("public_metrics"), Mapping) else {}
                        followers = int(user_metrics.get("followers_count") or 0)
                        engagements = sum(
                            int(metrics.get(key) or 0)
                            for key in ("like_count", "retweet_count", "reply_count", "quote_count", "bookmark_count")
                        )
                        created_ns = timestamp_ns(data.get("created_at"))
                        payload = {
                            "kind": "social_post",
                            "source": "x_filtered_stream",
                            "id": str(data.get("id") or ""),
                            "handle": str(user.get("username") or data.get("author_id") or ""),
                            "text": text,
                            "created_ns": created_ns,
                            "followers": followers,
                            "authority": authority(
                                followers,
                                float(user.get("e4_authority_score") or 0.0),
                                bool(user.get("verified")),
                            ),
                            "novelty": novelty.score(text),
                            "engagement_velocity": min(1.0, engagements / 500.0),
                            "ttl_seconds": args.ttl_seconds,
                        }
                        publisher.publish(payload)
                        print(json.dumps({"post": payload["id"], "handle": payload["handle"], "novelty": payload["novelty"]}), flush=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reconnects += 1
                print(json.dumps({"error": f"{type(exc).__name__}: {exc}", "reconnects": reconnects}), flush=True)
                await asyncio.sleep(min(30.0, 0.5 * (2 ** min(6, reconnects))))


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Stream curated X accounts into the E4 V10 narrative cache")
    value.add_argument("--accounts", type=Path, default=Path(os.getenv("E4_SOCIAL_ACCOUNTS_PATH", "models/e4/e4-social-accounts.txt")))
    value.add_argument("--bearer-token", default=os.getenv("X_BEARER_TOKEN", ""))
    value.add_argument("--stream-url", default=os.getenv("E4_X_STREAM_URL", "https://api.x.com/2/tweets/search/stream"))
    value.add_argument("--rules-url", default=os.getenv("E4_X_RULES_URL", "https://api.x.com/2/tweets/search/stream/rules"))
    value.add_argument("--sync-rules", action="store_true", default=os.getenv("E4_X_SYNC_RULES", "false").lower() in {"1", "true", "yes"})
    value.add_argument("--max-rule-chars", type=int, default=450)
    value.add_argument("--udp-host", default=os.getenv("E4_PIPELINE_UDP_HOST", "127.0.0.1"))
    value.add_argument("--udp-port", type=int, default=int(os.getenv("E4_PIPELINE_UDP_PORT", "19104")))
    value.add_argument("--ttl-seconds", type=float, default=1800.0)
    value.add_argument("--novelty-window", type=int, default=20_000)
    return value


def main() -> int:
    args = parser().parse_args()
    return asyncio.run(stream(args))


if __name__ == "__main__":
    raise SystemExit(main())

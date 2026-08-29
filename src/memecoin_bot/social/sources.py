from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from typing import Any

import aiohttp

from memecoin_bot.models import iso
from memecoin_bot.realtime.events import CanonicalEvent, CanonicalEventType

Emit = Callable[[CanonicalEvent], Awaitable[None]]
KnownToken = Callable[[str, str], bool]

SOLANA_ADDRESS = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
BNB_ADDRESS = re.compile(r"(?<![0-9a-fA-F])0x[0-9a-fA-F]{40}(?![0-9a-fA-F])")


def social_events_from_text(
    text: str,
    *,
    source: str,
    platform: str,
    source_event_id: str,
    source_event_at: str,
    received_at: str | None = None,
    author_id: str | None = None,
    channel_id: str | None = None,
    engagement: float | None = None,
    known_token: KnownToken | None = None,
) -> list[CanonicalEvent]:
    """Convert authorized text to minimal CA evidence without persisting its content."""
    received = received_at or iso()
    text_hash = hashlib.sha256(text.encode()).hexdigest()
    matches = [("solana", value) for value in SOLANA_ADDRESS.findall(text)]
    matches.extend(("bsc", value.lower()) for value in BNB_ADDRESS.findall(text))
    events = []
    for index, (chain, address) in enumerate(dict.fromkeys(matches)):
        if known_token and not known_token(chain, address):
            continue
        events.append(
            CanonicalEvent.create(
                CanonicalEventType.SOCIAL_OBSERVATION,
                address,
                chain,
                platform,
                source,
                source_event_at,
                received_timestamp=received,
                available_timestamp=received,
                source_event_id=f"{source_event_id}:{index}",
                confidence=0.7,
                raw_provenance={
                    "authorized_source": True,
                    "content_persisted": False,
                    "point_in_time": True,
                },
                payload={
                    "mention_count": 1,
                    "unique_mentioners": 1 if author_id else None,
                    "source_diversity": 1,
                    "engagement": engagement,
                    "author_hash": _hash_identifier(author_id),
                    "channel_hash": _hash_identifier(channel_id),
                    "content_sha256": text_hash,
                    "social_lead_lag_requires_market_state": True,
                },
            )
        )
    return events


class AuthorizedDiscordSocialSource:
    name = "discord_authorized_social"

    def __init__(self, channel_ids: Iterable[int], known_token: KnownToken):
        self.channel_ids = frozenset(int(value) for value in channel_ids)
        self.known_token = known_token

    def parse_message(
        self,
        *,
        message_id: int,
        channel_id: int,
        author_id: int,
        content: str,
        created_at: datetime,
        author_is_bot: bool = False,
    ) -> list[CanonicalEvent]:
        if author_is_bot or channel_id not in self.channel_ids:
            return []
        return social_events_from_text(
            content,
            source=self.name,
            platform="discord",
            source_event_id=str(message_id),
            source_event_at=_timestamp(created_at),
            author_id=str(author_id),
            channel_id=str(channel_id),
            known_token=self.known_token,
        )


class BlueskyJetstreamSocialSource:
    name = "bluesky_jetstream_social"
    websocket_url = (
        "wss://jetstream.us-east.bsky.network/xrpc/"
        "network.bsky.jetstream.subscribeEvents?collections=app.bsky.feed.post&kinds=commit"
    )

    def __init__(self, known_token: KnownToken, silence_seconds: float = 90):
        self.known_token = known_token
        self.silence_seconds = silence_seconds
        self.cursor: str | None = None

    def parse_message(self, raw: dict[str, Any], received_at: str | None = None) -> list[CanonicalEvent]:
        payload = raw.get("payload") or {}
        if payload.get("operation") == "delete":
            return []
        record = payload.get("record") or {}
        text = str(record.get("text") or "")
        self.cursor = str(raw.get("cursor") or payload.get("seq") or self.cursor or "") or None
        return social_events_from_text(
            text,
            source=self.name,
            platform="bluesky",
            source_event_id=f"{payload.get('did')}:{payload.get('collection')}:{payload.get('rkey')}",
            source_event_at=str(payload.get("time") or record.get("createdAt") or received_at or iso()),
            received_at=received_at,
            author_id=str(payload.get("did") or "") or None,
            engagement=None,
            known_token=self.known_token,
        )

    async def run_events(self, emit: Emit, stop: asyncio.Event) -> None:
        reconnects = 0
        while not stop.is_set():
            url = self.websocket_url
            if self.cursor:
                url = f"{url}&cursor={self.cursor}"
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=15)
            try:
                async with (
                    aiohttp.ClientSession(timeout=timeout) as session,
                    session.ws_connect(
                        url,
                        protocols=("xrpc.v1.json",),
                        heartbeat=30,
                        autoping=True,
                    ) as websocket,
                ):
                    reconnects = 0
                    while not stop.is_set():
                        message = await asyncio.wait_for(
                            websocket.receive(), timeout=self.silence_seconds
                        )
                        if message.type == aiohttp.WSMsgType.TEXT:
                            for event in self.parse_message(json.loads(message.data), iso()):
                                await emit(event)
                        elif message.type in {
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        }:
                            raise ConnectionError(f"Bluesky Jetstream closed: {message.type}")
            except (TimeoutError, aiohttp.ClientError, ConnectionError, json.JSONDecodeError):
                reconnects += 1
                try:
                    await asyncio.wait_for(stop.wait(), timeout=min(60, 2**min(reconnects, 5)))
                except TimeoutError:
                    pass


class TelegramAuthorizedSocialSource:
    """Optional Telethon adapter; imports the declared social extra only when enabled."""

    name = "telegram_authorized_social"

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        session_name: str,
        channels: Iterable[str],
        known_token: KnownToken,
    ):
        self.api_id = int(api_id)
        self.api_hash = api_hash
        self.session_name = session_name
        self.channels = tuple(dict.fromkeys(str(value) for value in channels if value))
        self.known_token = known_token

    async def run_events(self, emit: Emit, stop: asyncio.Event) -> None:
        try:
            from telethon import TelegramClient, events
        except ImportError as error:  # pragma: no cover - optional deployment extra
            raise RuntimeError("install the declared 'social' extra to enable Telegram") from error
        client = TelegramClient(self.session_name, self.api_id, self.api_hash)

        @client.on(events.NewMessage(chats=self.channels))
        async def receive(message: Any) -> None:
            source_at = _timestamp(message.message.date)
            for event in social_events_from_text(
                str(message.raw_text or ""),
                source=self.name,
                platform="telegram",
                source_event_id=str(message.id),
                source_event_at=source_at,
                author_id=str(message.sender_id or "") or None,
                channel_id=str(message.chat_id or "") or None,
                known_token=self.known_token,
            ):
                await emit(event)

        await client.start()
        try:
            await stop.wait()
        finally:
            await client.disconnect()


def _hash_identifier(value: str | None) -> str | None:
    return hashlib.sha256(value.encode()).hexdigest() if value else None


def _timestamp(value: datetime | str) -> str:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        raise ValueError("social source timestamps must include timezone")
    return parsed.astimezone(UTC).isoformat()

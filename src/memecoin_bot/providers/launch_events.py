from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from typing import Any

import aiohttp

from memecoin_bot.alpha_engine import LaunchEvent
from memecoin_bot.models import iso
from memecoin_bot.observability.logging import event as log_event
from memecoin_bot.providers.base import ProviderError, ResilientJsonClient

Emit = Callable[[LaunchEvent], Awaitable[None]]


def _ws_url(http_url: str) -> str:
    if http_url.startswith("https://"):
        return "wss://" + http_url.removeprefix("https://")
    if http_url.startswith("http://"):
        return "ws://" + http_url.removeprefix("http://")
    return http_url


def extract_new_mint(transaction: dict[str, Any]) -> str | None:
    """Return a mint present after, but not before, a successful launch transaction."""
    result = transaction.get("result") or transaction
    meta = result.get("meta") or {}
    if meta.get("err") is not None:
        return None
    pre = {str(row.get("mint")) for row in meta.get("preTokenBalances") or []}
    post = [str(row.get("mint")) for row in meta.get("postTokenBalances") or [] if row.get("mint")]
    return next((mint for mint in post if mint not in pre), None)


class SolanaProgramLaunchSource:
    """Read-only `logsSubscribe` launch source with bounded reconnect and HTTP enrichment."""

    name = "solana_direct_launch"

    def __init__(
        self,
        rpc_url: str,
        program_ids: Iterable[str],
        client: ResilientJsonClient,
        launchpad: str = "pumpfun",
        reconnect_seconds: float = 2,
    ):
        self.rpc_url = rpc_url
        self.websocket_url = _ws_url(rpc_url)
        self.program_ids = tuple(dict.fromkeys(value for value in program_ids if value))
        self.client = client
        self.launchpad = launchpad
        self.reconnect_seconds = reconnect_seconds
        self.log = logging.getLogger("memecoin_bot.launch.solana")

    async def transaction(self, signature: str) -> dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [
                signature,
                {
                    "encoding": "jsonParsed",
                    "commitment": "confirmed",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        }
        result = await self.client.request(self.rpc_url, "POST", payload)
        return result if isinstance(result, dict) else {}

    async def parse_notification(
        self, payload: dict[str, Any], program_id: str
    ) -> LaunchEvent | None:
        result = (payload.get("params") or {}).get("result") or {}
        value = result.get("value") or {}
        logs = value.get("logs") or []
        if value.get("err") is not None or not any(
            "instruction: create" in str(line).lower() for line in logs
        ):
            return None
        signature = value.get("signature")
        if not signature:
            return None
        transaction = await self.transaction(str(signature))
        token = extract_new_mint(transaction)
        if not token:
            return None
        received = iso()
        transaction_result = transaction.get("result") or {}
        block_time = transaction_result.get("blockTime")
        source_timestamp = (
            datetime.fromtimestamp(float(block_time), UTC).isoformat() if block_time else received
        )
        account_keys = ((transaction_result.get("transaction") or {}).get("message") or {}).get(
            "accountKeys"
        ) or []
        signer = next(
            (
                row.get("pubkey")
                for row in account_keys
                if isinstance(row, dict) and row.get("signer") is True
            ),
            None,
        )
        slot = str((result.get("context") or {}).get("slot") or "")
        return LaunchEvent.deterministic(
            self.name,
            "solana",
            token,
            source_timestamp,
            source_received_at=received,
            launchpad=self.launchpad,
            creator_address=str(signer) if signer else None,
            slot_or_block=slot,
            transaction_id=str(signature),
            metadata={
                "program_id": program_id,
                "logs": logs[:20],
                "timestamp_source": "block_time" if block_time else "received_at",
            },
        )

    async def run(self, emit: Emit, stop: asyncio.Event) -> None:
        if not self.program_ids:
            raise ProviderError("No Solana launch program ids configured")
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=self.client.timeout, sock_read=90)
        while not stop.is_set():
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:  # noqa: SIM117
                    async with session.ws_connect(self.websocket_url, heartbeat=30) as websocket:
                        subscriptions: dict[int, str] = {}
                        for request_id, program_id in enumerate(self.program_ids, 1):
                            await websocket.send_json(
                                {
                                    "jsonrpc": "2.0",
                                    "id": request_id,
                                    "method": "logsSubscribe",
                                    "params": [
                                        {"mentions": [program_id]},
                                        {"commitment": "confirmed"},
                                    ],
                                }
                            )
                            response = await websocket.receive_json()
                            if response.get("error"):
                                raise ProviderError(
                                    f"Solana logsSubscribe rejected {program_id}: {response['error']}"
                                )
                            if response.get("result") is not None:
                                subscriptions[int(response["result"])] = program_id
                        if not subscriptions:
                            raise ProviderError("Solana launch source created no subscriptions")
                        log_event(
                            self.log, logging.INFO, "launch_source_connected", source=self.name
                        )
                        async for message in websocket:
                            if stop.is_set():
                                return
                            if message.type != aiohttp.WSMsgType.TEXT:
                                continue
                            payload = json.loads(message.data)
                            subscription = (payload.get("params") or {}).get("subscription")
                            program_id = (
                                subscriptions.get(int(subscription))
                                if subscription is not None
                                else None
                            )
                            if program_id:
                                event = await self.parse_notification(payload, program_id)
                                if event:
                                    await emit(event)
            except (TimeoutError, aiohttp.ClientError, ProviderError, ValueError) as exc:
                log_event(
                    self.log,
                    logging.WARNING,
                    "launch_source_reconnect",
                    source=self.name,
                    error=str(exc),
                )
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.reconnect_seconds)
                except TimeoutError:
                    pass


def topic_address(value: str) -> str | None:
    raw = value.removeprefix("0x")
    return "0x" + raw[-40:].lower() if len(raw) >= 40 else None


def abi_word_address(data: str, word_index: int) -> str | None:
    """Extract an ABI-encoded address from a non-indexed event-data word."""
    raw = str(data or "").removeprefix("0x")
    start = word_index * 64
    word = raw[start : start + 64]
    if word_index < 0 or len(word) != 64:
        return None
    padding, address = word[:24], word[24:]
    try:
        numeric = int(address, 16)
    except ValueError:
        return None
    if padding != "0" * 24 or numeric == 0:
        return None
    return "0x" + address.lower()


class EvmFactoryLaunchSource:
    """Read-only BSC factory-log polling fallback; WebSocket-capable RPCs can replace it later."""

    name = "bsc_direct_launch"

    def __init__(
        self,
        rpc_url: str,
        factory_addresses: Iterable[str],
        event_topics: Iterable[str],
        client: ResilientJsonClient,
        launchpad: str = "fourmeme",
        token_topic_index: int = 1,
        token_data_word_index: int | None = None,
        creator_data_word_index: int | None = None,
        load_cursor: Callable[[str], str | None] | None = None,
        save_cursor: Callable[[str, str, dict[str, Any]], None] | None = None,
        poll_seconds: float = 2,
    ):
        self.rpc_url = rpc_url
        self.factories = tuple(address.lower() for address in factory_addresses if address)
        self.event_topics = tuple(topic.lower() for topic in event_topics if topic)
        self.client = client
        self.launchpad = launchpad
        self.token_topic_index = token_topic_index
        self.token_data_word_index = token_data_word_index
        self.creator_data_word_index = creator_data_word_index
        self.load_cursor = load_cursor
        self.save_cursor = save_cursor
        self.poll_seconds = poll_seconds
        self.next_block: int | None = None
        self.log = logging.getLogger("memecoin_bot.launch.bsc")

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        response = await self.client.request(
            self.rpc_url,
            "POST",
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        if response.get("error"):
            raise ProviderError(str(response["error"]))
        return response.get("result")

    async def poll_once(self) -> list[LaunchEvent]:
        if not self.factories or not self.event_topics:
            raise ProviderError("No BNB launch factory addresses/event topics configured")
        latest = int(await self._rpc("eth_blockNumber", []), 16)
        if self.next_block is None and self.load_cursor:
            persisted = self.load_cursor(self.name)
            self.next_block = int(persisted) if persisted is not None else None
        start = self.next_block if self.next_block is not None else latest
        end = min(latest, start + 250)
        logs = await self._rpc(
            "eth_getLogs",
            [
                {
                    "fromBlock": hex(start),
                    "toBlock": hex(end),
                    "address": list(self.factories),
                    "topics": [list(self.event_topics)],
                }
            ],
        )
        self.next_block = end + 1
        if self.save_cursor:
            self.save_cursor(
                self.name,
                str(self.next_block),
                {"last_completed_block": end, "backfill_window": end - start + 1},
            )
        received = iso()
        block_timestamps: dict[str, str] = {}
        for block_number in {
            str(row.get("blockNumber")) for row in logs or [] if row.get("blockNumber")
        }:
            try:
                header = await self._rpc("eth_getBlockByNumber", [block_number, False])
                timestamp = header.get("timestamp") if isinstance(header, dict) else None
                if timestamp:
                    block_timestamps[block_number] = datetime.fromtimestamp(
                        int(str(timestamp), 16), UTC
                    ).isoformat()
            except (ProviderError, TypeError, ValueError):
                # Public RPCs are allowed to omit/deny the enrichment. The event is
                # still useful, but its timestamp provenance stays explicit below.
                pass
        events = []
        for row in logs or []:
            topics = row.get("topics") or []
            token = (
                abi_word_address(str(row.get("data") or ""), self.token_data_word_index)
                if self.token_data_word_index is not None
                else (
                    topic_address(str(topics[self.token_topic_index]))
                    if len(topics) > self.token_topic_index
                    else None
                )
            )
            if not token:
                continue
            creator = (
                abi_word_address(str(row.get("data") or ""), self.creator_data_word_index)
                if self.creator_data_word_index is not None
                else None
            )
            block = str(row.get("blockNumber") or "")
            transaction = str(row.get("transactionHash") or "")
            log_index = str(row.get("logIndex") or "")
            source_timestamp = block_timestamps.get(block, received)
            events.append(
                LaunchEvent.deterministic(
                    self.name,
                    "bsc",
                    token,
                    source_timestamp,
                    source_received_at=received,
                    launchpad=self.launchpad,
                    creator_address=creator,
                    slot_or_block=block,
                    transaction_id=f"{transaction}:{log_index}" if log_index else transaction,
                    metadata={
                        "factory": row.get("address"),
                        "topic": topics[0],
                        "log_index": log_index or None,
                        "address_encoding": (
                            "abi_event_data"
                            if self.token_data_word_index is not None
                            else "indexed_topic"
                        ),
                        "timestamp_source": (
                            "block_timestamp" if block in block_timestamps else "received_at"
                        ),
                    },
                )
            )
        return events

    async def run(self, emit: Emit, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                for event in await self.poll_once():
                    await emit(event)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
                except TimeoutError:
                    pass
            except ProviderError as exc:
                log_event(
                    self.log,
                    logging.WARNING,
                    "launch_source_poll_failed",
                    source=self.name,
                    error=str(exc),
                )
                try:
                    await asyncio.wait_for(stop.wait(), timeout=max(self.poll_seconds, 5))
                except TimeoutError:
                    pass

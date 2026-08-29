from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote_plus

import aiohttp

from memecoin_bot.models import iso
from memecoin_bot.observability.logging import event as log_event
from memecoin_bot.providers.base import ProviderError, ResilientJsonClient
from memecoin_bot.providers.launch_events import (
    EvmFactoryLaunchSource,
    _ws_url,
    abi_word_address,
    extract_new_mint,
    topic_address,
)
from memecoin_bot.realtime.events import CanonicalEvent, CanonicalEventType, ProviderState
from memecoin_bot.realtime.pumpfun import (
    PUMP_PROGRAM_ID,
    anchor_events_from_logs,
    decode_account_data,
    decode_bonding_curve_account,
    jito_tip_evidence,
)

Emit = Callable[[CanonicalEvent], Awaitable[None]]

# Official Jito getTipAccounts result. The source refresh endpoint is documented
# in docs.jito.wtf; these public keys contain no credentials.
JITO_TIP_ACCOUNTS = {
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
}


def _epoch_timestamp(value: Any, fallback: str) -> str:
    if value in (None, ""):
        return fallback
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if number > 10_000_000_000:
        number /= 1_000
    try:
        return datetime.fromtimestamp(number, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return fallback


def _rate_limited(error: object) -> bool:
    message = str(error).lower()
    return "429" in message or "rate limit" in message


def _redacted_error(error: object) -> str:
    value = str(error)
    value = re.sub(r"(?i)(api-key=)[^&\s'\"]+", r"\1<redacted>", value)
    return re.sub(r"(?i)(x-dune-api-key[=: ]+)[^&\s'\"]+", r"\1<redacted>", value)


def _health_event(
    provider: str,
    state: ProviderState,
    *,
    sequence: int,
    error: str | None = None,
    counters: dict[str, Any] | None = None,
) -> CanonicalEvent:
    now = iso()
    values = dict(counters or {})
    values.update({"provider": provider, "state": str(state), "error": error})
    return CanonicalEvent.create(
        CanonicalEventType.PROVIDER_HEALTH,
        f"__provider__:{provider}",
        "provider",
        "provider",
        provider,
        now,
        received_timestamp=now,
        available_timestamp=now,
        source_event_id=f"{provider}:{sequence}",
        confidence=1,
        raw_provenance={"kind": "provider_lifecycle"},
        payload=values,
    )


class NativePumpFunSource:
    """Pump program logs with Anchor event decoding and bounded slot-gap recovery."""

    name = "solana_pumpfun_native"

    def __init__(
        self,
        rpc_url: str,
        client: ResilientJsonClient,
        program_id: str = PUMP_PROGRAM_ID,
        reconnect_seconds: float = 2,
        silence_seconds: float = 90,
        backfill_limit: int = 100,
        backfill_max_pages: int = 20,
        fallback_rpc_url: str | None = None,
        fallback_client: ResilientJsonClient | None = None,
        enrichment_queue_max: int = 2_048,
        enrichment_concurrency: int = 4,
        transaction_cache_size: int = 4_096,
        load_cursor: Callable[[str], str | None] | None = None,
        save_cursor: Callable[[str, str, dict[str, Any] | None], None] | None = None,
    ):
        self.rpc_url = rpc_url
        self.websocket_url = _ws_url(rpc_url)
        self.client = client
        self.program_id = program_id
        self.reconnect_seconds = reconnect_seconds
        self.silence_seconds = silence_seconds
        self.backfill_limit = backfill_limit
        self.backfill_max_pages = backfill_max_pages
        self.fallback_rpc_url = fallback_rpc_url
        self.fallback_client = fallback_client
        self.enrichment_queue_max = enrichment_queue_max
        self.enrichment_concurrency = enrichment_concurrency
        self.transaction_cache_size = transaction_cache_size
        self.load_cursor = load_cursor
        self.save_cursor = save_cursor
        self.last_slot: int | None = None
        self.last_signature: str | None = None
        if load_cursor:
            cursor = load_cursor(self.name)
            if cursor and ":" in cursor:
                slot, signature = cursor.split(":", 1)
                if slot.isdigit():
                    self.last_slot = int(slot)
                    self.last_signature = signature or None
        self.events_received = 0
        self.last_valid_event_at: str | None = None
        self.errors = 0
        self.rate_limits = 0
        self.reconnects = 0
        self.health_sequence = 0
        self.slot_density: dict[int, int] = {}
        self.fallback_requests = 0
        self.gap_incomplete = False
        self._rpc_semaphores: dict[str, asyncio.Semaphore] = {}
        self._transaction_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._transaction_inflight: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._enrichment_pending: set[str] = set()
        self.log = logging.getLogger("memecoin_bot.realtime.pumpfun_native")

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        semaphore = self._rpc_semaphores.setdefault(
            method,
            asyncio.Semaphore(2 if method == "getTransaction" else 1),
        )
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        async with semaphore:
            try:
                response = await self.client.request(self.rpc_url, "POST", body)
            except ProviderError:
                if not self.fallback_rpc_url or self.fallback_client is None:
                    raise
                self.fallback_requests += 1
                response = await self.fallback_client.request(self.fallback_rpc_url, "POST", body)
        if response.get("error"):
            raise ProviderError(f"Solana {method}: {response['error']}")
        return response.get("result")

    async def transaction(self, signature: str) -> dict[str, Any]:
        if signature in self._transaction_cache:
            cached = self._transaction_cache.pop(signature)
            self._transaction_cache[signature] = cached
            return cached
        task = self._transaction_inflight.get(signature)
        if task is None:
            task = asyncio.create_task(self._fetch_transaction(signature))
            self._transaction_inflight[signature] = task
        try:
            value = await task
        finally:
            self._transaction_inflight.pop(signature, None)
        self._transaction_cache[signature] = value
        while len(self._transaction_cache) > self.transaction_cache_size:
            self._transaction_cache.popitem(last=False)
        return value

    async def _fetch_transaction(self, signature: str) -> dict[str, Any]:
        for attempt in range(5):
            try:
                result = await self._rpc(
                    "getTransaction",
                    [
                        signature,
                        {
                            "encoding": "jsonParsed",
                            "commitment": "confirmed",
                            "maxSupportedTransactionVersion": 0,
                        },
                    ],
                )
                return {"result": result} if isinstance(result, dict) else {}
            except ProviderError as exc:
                if not _rate_limited(exc) or attempt == 4:
                    raise
                self.rate_limits += 1
                await asyncio.sleep(min(30.0, (2**attempt) + random.random()))
        return {}

    async def parse_notification(self, payload: dict[str, Any]) -> list[CanonicalEvent]:
        result = (payload.get("params") or {}).get("result") or {}
        value = result.get("value") or {}
        if value.get("err") is not None:
            return []
        signature = str(value.get("signature") or "")
        if not signature:
            return []
        slot = int((result.get("context") or {}).get("slot") or 0)
        logs = list(value.get("logs") or [])
        # FAST PATH: Anchor events carry the mint, actor, side, reserves and amounts.
        # They are published before any optional transaction enrichment.
        return self.events_from_transaction({}, signature, slot, logs)

    def events_from_transaction(
        self,
        transaction: dict[str, Any],
        signature: str,
        slot: int,
        notification_logs: list[Any] | None = None,
    ) -> list[CanonicalEvent]:
        result = transaction.get("result") or {}
        meta = result.get("meta") or {}
        if meta.get("err") is not None:
            return []
        logs = list(notification_logs or meta.get("logMessages") or [])
        received = iso()
        block_timestamp = _epoch_timestamp(result.get("blockTime"), received)
        decoded = anchor_events_from_logs(logs, self.program_id)
        density = self.slot_density.get(slot, 0) + 1
        self.slot_density[slot] = density
        for old_slot in list(self.slot_density):
            if old_slot < slot - 8:
                self.slot_density.pop(old_slot, None)
        jito = jito_tip_evidence(transaction, JITO_TIP_ACCOUNTS)
        provenance = {
            "transport": "solana_logsSubscribe",
            "fast_path": not bool(result),
            "transaction_enrichment_required": not bool(decoded),
            "program_id": self.program_id,
            "commitment": "confirmed",
            "timestamp_source": "anchor_event_or_block_time",
            "log_count": len(logs),
            "slot_transaction_density_seen": density,
            "jito_tip_account_source": "official_getTipAccounts",
        }
        events: list[CanonicalEvent] = []
        for index, item in enumerate(decoded):
            source_timestamp = _epoch_timestamp(item.get("timestamp"), block_timestamp)
            event_name = item.get("anchor_event")
            common = {
                "canonical_token": str(item.get("mint") or ""),
                "chain": "solana",
                "platform": "pumpfun",
                "source": self.name,
                "source_timestamp": source_timestamp,
                "received_timestamp": received,
                "available_timestamp": iso(),
                "slot_or_block": slot,
                "transaction_signature": signature,
                "raw_provenance": {**provenance, "anchor_event": event_name, "event_index": index},
            }
            if not common["canonical_token"]:
                continue
            if event_name == "CreateEvent":
                payload = {
                    **item,
                    "initial_real_token_reserves": item.get("real_token_reserves"),
                    **jito,
                }
                events.append(
                    CanonicalEvent.create(
                        CanonicalEventType.TOKEN_CREATED,
                        source_event_id=f"{signature}:{index}",
                        payload=payload,
                        **common,
                    )
                )
            elif event_name == "TradeEvent":
                payload = {
                    **item,
                    "actor": item.get("user"),
                    "side": "buy" if item.get("is_buy") else "sell",
                    "sol_amount_lamports": item.get("sol_amount"),
                    "sol_amount": float(item.get("sol_amount") or 0) / 1_000_000_000,
                    "real_quote_reserves": item.get("real_sol_reserves"),
                    "virtual_quote_reserves": item.get("virtual_sol_reserves"),
                    **jito,
                }
                events.append(
                    CanonicalEvent.create(
                        CanonicalEventType.TOKEN_TRADE,
                        source_event_id=f"{signature}:{index}",
                        payload=payload,
                        **common,
                    )
                )
                events.append(
                    CanonicalEvent.create(
                        CanonicalEventType.WALLET_BUY
                        if item.get("is_buy")
                        else CanonicalEventType.WALLET_SELL,
                        source_event_id=f"{signature}:{index}",
                        payload=payload,
                        **common,
                    )
                )
                curve_payload = {
                    key: payload.get(key)
                    for key in (
                        "virtual_token_reserves",
                        "virtual_sol_reserves",
                        "virtual_quote_reserves",
                        "real_token_reserves",
                        "real_sol_reserves",
                        "real_quote_reserves",
                    )
                }
                curve_payload["creator"] = item.get("creator")
                events.append(
                    CanonicalEvent.create(
                        CanonicalEventType.BONDING_CURVE_STATE,
                        source_event_id=f"{signature}:{index}",
                        payload=curve_payload,
                        **common,
                    )
                )
                if jito["jito_tip_present"]:
                    events.append(
                        CanonicalEvent.create(
                            CanonicalEventType.BUNDLE_EVIDENCE,
                            source_event_id=f"{signature}:{index}",
                            confidence=0.7,
                            payload={**payload, **jito, "exact_bundle_id": None},
                            **common,
                        )
                    )
            elif event_name == "CompleteEvent":
                events.append(
                    CanonicalEvent.create(
                        CanonicalEventType.MIGRATION_STARTED,
                        source_event_id=f"{signature}:{index}",
                        payload=item,
                        **common,
                    )
                )
            elif event_name == "CompletePumpAmmMigrationEvent":
                for kind in (
                    CanonicalEventType.MIGRATION_COMPLETED,
                    CanonicalEventType.POOL_CREATED,
                ):
                    events.append(
                        CanonicalEvent.create(
                            kind,
                            source_event_id=f"{signature}:{index}",
                            pool_identity=item.get("pool"),
                            payload=item,
                            **common,
                        )
                    )
        if not decoded and any("instruction: create" in str(line).lower() for line in logs):
            token = extract_new_mint(transaction)
            if token:
                events.append(
                    CanonicalEvent.create(
                        CanonicalEventType.TOKEN_CREATED,
                        token,
                        "solana",
                        "pumpfun",
                        self.name,
                        block_timestamp,
                        received_timestamp=received,
                        available_timestamp=iso(),
                        slot_or_block=slot,
                        transaction_signature=signature,
                        source_event_id=f"{signature}:fallback",
                        confidence=0.8,
                        raw_provenance={**provenance, "parser": "token_balance_fallback"},
                        payload={"creator": self._transaction_signer(transaction), **jito},
                    )
                )
        return events

    @staticmethod
    def _transaction_signer(transaction: dict[str, Any]) -> str | None:
        result = transaction.get("result") or {}
        keys = ((result.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
        for row in keys:
            if isinstance(row, dict) and row.get("signer") is True:
                return str(row.get("pubkey"))
        return None

    async def backfill(self, emit: Emit) -> int:
        if self.last_slot is None:
            return 0
        missing_by_signature: dict[str, dict[str, Any]] = {}
        before = None
        boundary_found = False
        pages = 0
        while pages < self.backfill_max_pages:
            options: dict[str, Any] = {
                "limit": self.backfill_limit,
                "commitment": "confirmed",
            }
            if before:
                options["before"] = before
            rows = await self._rpc("getSignaturesForAddress", [self.program_id, options])
            pages += 1
            if not rows:
                boundary_found = True
                break
            for row in rows:
                slot = int(row.get("slot") or 0)
                if slot <= self.last_slot:
                    boundary_found = True
                    continue
                signature = str(row.get("signature") or "")
                if signature and row.get("err") is None:
                    missing_by_signature[signature] = row
            if boundary_found or len(rows) < self.backfill_limit:
                boundary_found = True
                break
            before = str(rows[-1].get("signature") or "") or None
            if not before:
                break
        self.gap_incomplete = not boundary_found
        missing = sorted(
            missing_by_signature.values(),
            key=lambda row: (int(row.get("slot") or 0), str(row.get("signature") or "")),
        )
        emitted = 0
        for row in missing:
            signature = str(row.get("signature") or "")
            if not signature:
                continue
            tx = await self.transaction(signature)
            for event in self.events_from_transaction(tx, signature, int(row["slot"])):
                await emit(event)
                emitted += 1
            self.last_slot = max(self.last_slot or 0, int(row["slot"]))
            self.last_signature = signature
            self._persist_cursor()
        return emitted

    def _persist_cursor(self) -> None:
        if self.save_cursor and self.last_slot is not None and self.last_signature:
            self.save_cursor(
                self.name,
                f"{self.last_slot}:{self.last_signature}",
                {
                    "last_slot": self.last_slot,
                    "last_signature": self.last_signature,
                    "gap_incomplete": self.gap_incomplete,
                },
            )

    async def _enrichment_worker(
        self,
        queue: asyncio.Queue[tuple[str, int, list[Any]]],
        emit: Emit,
        stop: asyncio.Event,
    ) -> None:
        while not stop.is_set():
            try:
                signature, slot, logs = await asyncio.wait_for(queue.get(), timeout=0.5)
            except TimeoutError:
                continue
            try:
                transaction = await self.transaction(signature)
                for event in self.events_from_transaction(transaction, signature, slot, logs):
                    await emit(event)
            except (ProviderError, TypeError, ValueError) as exc:
                self.errors += 1
                log_event(
                    self.log,
                    logging.WARNING,
                    "pumpfun_async_enrichment_failed",
                    signature=signature,
                    error=_redacted_error(exc),
                )
            finally:
                self._enrichment_pending.discard(signature)
                queue.task_done()

    async def run_events(self, emit: Emit, stop: asyncio.Event) -> None:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=self.client.timeout)
        enrichment_queue: asyncio.Queue[tuple[str, int, list[Any]]] = asyncio.Queue(
            maxsize=self.enrichment_queue_max
        )
        enrichment_workers = [
            asyncio.create_task(self._enrichment_worker(enrichment_queue, emit, stop))
            for _ in range(self.enrichment_concurrency)
        ]
        while not stop.is_set():
            self.health_sequence += 1
            if self.last_slot is not None:
                await emit(
                    _health_event(
                        self.name,
                        ProviderState.RECOVERING,
                        sequence=self.health_sequence,
                        counters={
                            "reconnect_attempts": self.reconnects,
                            "gap_detected_at": iso(),
                            "events_received": self.events_received,
                        },
                    )
                )
                try:
                    recovered = await self.backfill(emit)
                    self.health_sequence += 1
                    await emit(
                        _health_event(
                            self.name,
                            ProviderState.RECOVERING,
                            sequence=self.health_sequence,
                            counters={
                                "reconnect_attempts": self.reconnects,
                                "gap_recovered_at": iso(),
                                "backfilled_events": recovered,
                                "gap_fully_recovered": not self.gap_incomplete,
                                "events_received": self.events_received,
                            },
                        )
                    )
                except (ProviderError, TypeError, ValueError) as exc:
                    self.errors += 1
                    self.rate_limits += int(_rate_limited(exc))
                    log_event(
                        self.log,
                        logging.WARNING,
                        "pumpfun_gap_backfill_failed",
                        error=_redacted_error(exc),
                    )
            try:
                async with (
                    aiohttp.ClientSession(timeout=timeout) as session,
                    session.ws_connect(
                        self.websocket_url, heartbeat=30, autoping=True
                    ) as websocket,
                ):
                    await websocket.send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "logsSubscribe",
                            "params": [
                                {"mentions": [self.program_id]},
                                {"commitment": "confirmed"},
                            ],
                        }
                    )
                    response = await asyncio.wait_for(websocket.receive_json(), timeout=15)
                    if response.get("error") or response.get("result") is None:
                        raise ProviderError(f"logsSubscribe rejected: {response}")
                    self.health_sequence += 1
                    await emit(
                        _health_event(
                            self.name,
                            ProviderState.CONNECTED,
                            sequence=self.health_sequence,
                            counters={
                                "reconnect_attempts": self.reconnects,
                                "events_received": self.events_received,
                            },
                        )
                    )
                    while not stop.is_set():
                        message = await asyncio.wait_for(
                            websocket.receive(), timeout=self.silence_seconds
                        )
                        if message.type == aiohttp.WSMsgType.TEXT:
                            payload = json.loads(message.data)
                            result = (payload.get("params") or {}).get("result") or {}
                            value = result.get("value") or {}
                            signature = str(value.get("signature") or "")
                            slot = int((result.get("context") or {}).get("slot") or 0)
                            logs = list(value.get("logs") or [])
                            parsed = await self.parse_notification(payload)
                            for event in parsed:
                                await emit(event)
                                self.events_received += 1
                                self.last_valid_event_at = iso()
                                if event.slot_or_block:
                                    self.last_slot = max(
                                        self.last_slot or 0, int(event.slot_or_block)
                                    )
                                if event.transaction_signature:
                                    self.last_signature = event.transaction_signature
                            if parsed:
                                self._persist_cursor()
                            elif signature and signature not in self._enrichment_pending:
                                try:
                                    enrichment_queue.put_nowait((signature, slot, logs))
                                    self._enrichment_pending.add(signature)
                                except asyncio.QueueFull:
                                    log_event(
                                        self.log,
                                        logging.WARNING,
                                        "pumpfun_enrichment_backpressure",
                                        queue_size=enrichment_queue.qsize(),
                                    )
                        elif message.type in {
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.ERROR,
                        }:
                            raise ProviderError(f"websocket closed: {message.type}")
            except TimeoutError:
                self.errors += 1
                state, error = ProviderState.STALE, "SILENCE_WATCHDOG_EXPIRED"
            except (aiohttp.ClientError, ProviderError, json.JSONDecodeError, ValueError) as exc:
                self.errors += 1
                self.rate_limits += int(_rate_limited(exc))
                state, error = ProviderState.DISCONNECTED, _redacted_error(exc)
            else:
                break
            self.reconnects += 1
            self.health_sequence += 1
            await emit(
                _health_event(
                    self.name,
                    state,
                    sequence=self.health_sequence,
                    error=error,
                    counters={
                        "error_count": self.errors,
                        "rate_limit_count": self.rate_limits,
                        "reconnect_attempts": self.reconnects,
                        "events_received": self.events_received,
                        "last_valid_event_at": self.last_valid_event_at,
                    },
                )
            )
            try:
                delay = min(60.0, self.reconnect_seconds * (2 ** min(self.reconnects, 5)))
                await asyncio.wait_for(stop.wait(), timeout=delay + random.random())
            except TimeoutError:
                pass
        for worker in enrichment_workers:
            worker.cancel()
        await asyncio.gather(*enrichment_workers, return_exceptions=True)


class PumpPortalSource:
    """One PumpPortal socket for the free creation and migration streams."""

    name = "pumpportal_redundancy"

    def __init__(
        self,
        api_key: str,
        websocket_base_url: str = "wss://pumpportal.fun/api/data",
        reconnect_seconds: float = 2,
        silence_seconds: float = 90,
    ):
        if not api_key:
            raise ValueError("PumpPortal API key is required by the current data API")
        self.websocket_url = f"{websocket_base_url}?api-key={quote_plus(api_key)}"
        self.reconnect_seconds = reconnect_seconds
        self.silence_seconds = silence_seconds
        self.health_sequence = 0
        self.errors = 0
        self.reconnects = 0
        self.events_received = 0

    def parse_message(
        self, payload: dict[str, Any], received: str | None = None
    ) -> list[CanonicalEvent]:
        received = received or iso()
        if payload.get("message") and not payload.get("mint"):
            return []
        mint = str(payload.get("mint") or payload.get("token") or "")
        if not mint:
            return []
        signature = payload.get("signature") or payload.get("txHash")
        event_timestamp = _epoch_timestamp(
            payload.get("timestamp") or payload.get("createdTimestamp"), received
        )
        tx_type = str(payload.get("txType") or payload.get("type") or "").lower()
        pool = payload.get("pool") or payload.get("poolAddress")
        common = {
            "canonical_token": mint,
            "chain": "solana",
            "platform": "pumpfun",
            "source": self.name,
            "source_timestamp": event_timestamp,
            "received_timestamp": received,
            "available_timestamp": received,
            "slot_or_block": payload.get("slot"),
            "transaction_signature": str(signature) if signature else None,
            "raw_provenance": {
                "transport": "pumpportal_websocket",
                "subscription": "subscribeMigration"
                if "migrat" in tx_type
                else "subscribeNewToken",
                "timestamp_source": "provider" if event_timestamp != received else "received_at",
                "documented_tier": "FREE",
            },
        }
        source_event_id = str(payload.get("eventId") or signature or f"{mint}:{event_timestamp}")
        if "migrat" in tx_type or pool:
            return [
                CanonicalEvent.create(
                    CanonicalEventType.MIGRATION_COMPLETED,
                    source_event_id=source_event_id,
                    pool_identity=str(pool) if pool else None,
                    confidence=0.8,
                    payload={
                        "pool": pool,
                        "bonding_curve": payload.get("bondingCurveKey"),
                        "provider_payload_fields": sorted(payload),
                    },
                    **common,
                )
            ]
        return [
            CanonicalEvent.create(
                CanonicalEventType.TOKEN_CREATED,
                source_event_id=source_event_id,
                confidence=0.8,
                payload={
                    "symbol": payload.get("symbol"),
                    "name": payload.get("name"),
                    "creator": payload.get("traderPublicKey") or payload.get("creator"),
                    "bonding_curve": payload.get("bondingCurveKey"),
                    "virtual_token_reserves": payload.get("vTokensInBondingCurve"),
                    "virtual_sol_reserves": payload.get("vSolInBondingCurve"),
                    "virtual_quote_reserves": payload.get("vSolInBondingCurve"),
                    "provider_payload_fields": sorted(payload),
                },
                **common,
            )
        ]

    async def run_events(self, emit: Emit, stop: asyncio.Event) -> None:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15)
        while not stop.is_set():
            try:
                async with (
                    aiohttp.ClientSession(timeout=timeout) as session,
                    session.ws_connect(
                        self.websocket_url, heartbeat=30, autoping=True
                    ) as websocket,
                ):
                    await websocket.send_json({"method": "subscribeNewToken"})
                    await websocket.send_json({"method": "subscribeMigration"})
                    self.health_sequence += 1
                    await emit(
                        _health_event(
                            self.name,
                            ProviderState.CONNECTED,
                            sequence=self.health_sequence,
                            counters={
                                "events_received": self.events_received,
                                "reconnect_attempts": self.reconnects,
                                "metadata": {"subscriptions": ["new_token", "migration"]},
                            },
                        )
                    )
                    while not stop.is_set():
                        message = await asyncio.wait_for(
                            websocket.receive(), timeout=self.silence_seconds
                        )
                        if message.type == aiohttp.WSMsgType.TEXT:
                            payload = json.loads(message.data)
                            for event in self.parse_message(payload):
                                await emit(event)
                                self.events_received += 1
                        elif message.type in {
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.ERROR,
                        }:
                            raise ProviderError(f"PumpPortal websocket closed: {message.type}")
            except TimeoutError:
                self.errors += 1
                state, error = ProviderState.STALE, "SILENCE_WATCHDOG_EXPIRED"
            except (aiohttp.ClientError, ProviderError, json.JSONDecodeError) as exc:
                self.errors += 1
                state, error = ProviderState.DISCONNECTED, _redacted_error(exc)
            else:
                return
            self.reconnects += 1
            self.health_sequence += 1
            await emit(
                _health_event(
                    self.name,
                    state,
                    sequence=self.health_sequence,
                    error=error,
                    counters={
                        "error_count": self.errors,
                        "reconnect_attempts": self.reconnects,
                        "events_received": self.events_received,
                        "gap_detected_at": iso(),
                    },
                )
            )
            try:
                delay = min(60.0, self.reconnect_seconds * (2 ** min(self.reconnects, 5)))
                await asyncio.wait_for(stop.wait(), timeout=delay + random.random())
            except TimeoutError:
                pass


class EvmFactoryRealtimeSource:
    """BNB factory websocket primary with the persisted block poller as gap fallback."""

    name = "bsc_factory_realtime"

    def __init__(
        self,
        poller: EvmFactoryLaunchSource,
        silence_seconds: float = 90,
        max_backfill_windows: int = 8,
    ):
        self.poller = poller
        self.websocket_url = _ws_url(poller.rpc_url)
        self.silence_seconds = silence_seconds
        self.max_backfill_windows = max_backfill_windows
        self.health_sequence = 0
        self.events_received = 0
        self.errors = 0
        self.reconnects = 0

    def _from_launch(self, event: Any, *, transport: str) -> CanonicalEvent:
        transaction = str(event.transaction_id or "")
        signature = transaction.split(":", 1)[0] or None
        return CanonicalEvent.create(
            CanonicalEventType.TOKEN_CREATED,
            event.token_address,
            event.chain,
            str(event.launchpad or "bnb_factory"),
            self.name,
            event.source_event_timestamp,
            received_timestamp=event.source_received_at,
            available_timestamp=iso(),
            slot_or_block=event.slot_or_block,
            transaction_signature=signature,
            source_event_id=transaction or event.event_key,
            confidence=0.9,
            raw_provenance={
                **event.metadata,
                "transport": transport,
                "cursor_persisted": True,
            },
            payload={
                "creator": event.creator_address,
                "factory": event.metadata.get("factory"),
                "event_topic": event.metadata.get("topic"),
            },
        )

    async def _seed_cursor(self) -> None:
        if self.poller.next_block is not None:
            return
        persisted = self.poller.load_cursor(self.poller.name) if self.poller.load_cursor else None
        if persisted is not None:
            self.poller.next_block = int(persisted)
            return
        latest = int(await self.poller._rpc("eth_blockNumber", []), 16)
        self.poller.next_block = latest
        if self.poller.save_cursor:
            self.poller.save_cursor(
                self.poller.name,
                str(latest),
                {"seed_block": latest, "transport": "eth_subscribe"},
            )

    async def _event_from_log(self, row: dict[str, Any]) -> CanonicalEvent | None:
        topics = list(row.get("topics") or [])
        token = (
            abi_word_address(str(row.get("data") or ""), self.poller.token_data_word_index)
            if self.poller.token_data_word_index is not None
            else (
                topic_address(str(topics[self.poller.token_topic_index]))
                if len(topics) > self.poller.token_topic_index
                else None
            )
        )
        if not token:
            return None
        creator = (
            abi_word_address(str(row.get("data") or ""), self.poller.creator_data_word_index)
            if self.poller.creator_data_word_index is not None
            else None
        )
        received = iso()
        block = str(row.get("blockNumber") or "")
        source_timestamp = received
        try:
            header = await self.poller._rpc("eth_getBlockByNumber", [block, False])
            if isinstance(header, dict) and header.get("timestamp"):
                source_timestamp = datetime.fromtimestamp(
                    int(str(header["timestamp"]), 16), UTC
                ).isoformat()
        except (ProviderError, TypeError, ValueError):
            pass
        transaction = str(row.get("transactionHash") or "")
        log_index = str(row.get("logIndex") or "")
        return CanonicalEvent.create(
            CanonicalEventType.TOKEN_CREATED,
            token,
            "bsc",
            self.poller.launchpad,
            self.name,
            source_timestamp,
            received_timestamp=received,
            available_timestamp=iso(),
            slot_or_block=block,
            transaction_signature=transaction or None,
            source_event_id=f"{transaction}:{log_index}" if log_index else transaction,
            confidence=0.95,
            raw_provenance={
                "transport": "eth_subscribe_logs",
                "factory": row.get("address"),
                "topic": topics[0] if topics else None,
                "timestamp_source": (
                    "block_timestamp" if source_timestamp != received else "received_at"
                ),
                "cursor_persisted": True,
            },
            payload={"creator": creator, "factory": row.get("address")},
        )

    async def _backfill(self, emit: Emit) -> int:
        emitted = 0
        for _ in range(self.max_backfill_windows):
            latest = int(await self.poller._rpc("eth_blockNumber", []), 16)
            if self.poller.next_block is not None and self.poller.next_block > latest:
                break
            for event in await self.poller.poll_once():
                await emit(self._from_launch(event, transport="eth_getLogs_gap_backfill"))
                emitted += 1
        return emitted

    async def run_events(self, emit: Emit, stop: asyncio.Event) -> None:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=self.poller.client.timeout)
        await self._seed_cursor()
        while not stop.is_set():
            try:
                if self.reconnects:
                    recovered = await self._backfill(emit)
                    self.health_sequence += 1
                    await emit(
                        _health_event(
                            self.name,
                            ProviderState.RECOVERING,
                            sequence=self.health_sequence,
                            counters={
                                "gap_recovered_at": iso(),
                                "backfilled_events": recovered,
                                "reconnect_attempts": self.reconnects,
                            },
                        )
                    )
                async with (
                    aiohttp.ClientSession(timeout=timeout) as session,
                    session.ws_connect(
                        self.websocket_url, heartbeat=30, autoping=True
                    ) as websocket,
                ):
                    await websocket.send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "eth_subscribe",
                            "params": [
                                "logs",
                                {
                                    "address": list(self.poller.factories),
                                    "topics": [list(self.poller.event_topics)],
                                },
                            ],
                        }
                    )
                    response = await asyncio.wait_for(websocket.receive_json(), timeout=15)
                    if response.get("error") or response.get("result") is None:
                        raise ProviderError(f"BNB eth_subscribe rejected: {response}")
                    self.health_sequence += 1
                    await emit(
                        _health_event(
                            self.name,
                            ProviderState.CONNECTED,
                            sequence=self.health_sequence,
                            counters={
                                "events_received": self.events_received,
                                "reconnect_attempts": self.reconnects,
                                "metadata": {"primary": "eth_subscribe", "fallback": "eth_getLogs"},
                            },
                        )
                    )
                    while not stop.is_set():
                        message = await asyncio.wait_for(
                            websocket.receive(), timeout=self.silence_seconds
                        )
                        if message.type == aiohttp.WSMsgType.TEXT:
                            payload = json.loads(message.data)
                            row = (payload.get("params") or {}).get("result") or {}
                            event = await self._event_from_log(row)
                            if event is None:
                                continue
                            await emit(event)
                            self.events_received += 1
                            if event.slot_or_block:
                                next_block = int(event.slot_or_block, 16) + 1
                                self.poller.next_block = max(
                                    self.poller.next_block or 0, next_block
                                )
                                if self.poller.save_cursor:
                                    self.poller.save_cursor(
                                        self.poller.name,
                                        str(self.poller.next_block),
                                        {"transport": "eth_subscribe", "event_id": event.event_id},
                                    )
                        elif message.type in {
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.ERROR,
                        }:
                            raise ProviderError(f"BNB websocket closed: {message.type}")
            except TimeoutError:
                self.errors += 1
                state, error = ProviderState.STALE, "SILENCE_WATCHDOG_EXPIRED"
            except (aiohttp.ClientError, ProviderError, json.JSONDecodeError, ValueError) as exc:
                self.errors += 1
                state, error = ProviderState.DISCONNECTED, _redacted_error(exc)
            else:
                return
            self.reconnects += 1
            self.health_sequence += 1
            await emit(
                _health_event(
                    self.name,
                    state,
                    sequence=self.health_sequence,
                    error=error,
                    counters={
                        "error_count": self.errors,
                        "reconnect_attempts": self.reconnects,
                        "events_received": self.events_received,
                        "gap_detected_at": iso(),
                    },
                )
            )
            try:
                await asyncio.wait_for(stop.wait(), timeout=min(60, 2 ** min(self.reconnects, 5)))
            except TimeoutError:
                pass


class PumpCurveAccountSource:
    """Dynamic accountSubscribe monitor for active Pump bonding curves."""

    name = "solana_pumpfun_curve_accounts"

    def __init__(
        self,
        rpc_url: str,
        client: ResilientJsonClient,
        targets: Callable[[], Iterable[dict[str, Any]]],
        refresh_seconds: float = 5,
        silence_seconds: float = 90,
    ):
        self.rpc_url = rpc_url
        self.websocket_url = _ws_url(rpc_url)
        self.client = client
        self.targets = targets
        self.refresh_seconds = refresh_seconds
        self.silence_seconds = silence_seconds
        self.health_sequence = 0
        self.events_received = 0
        self.errors = 0
        self.reconnects = 0

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        response = await self.client.request(
            self.rpc_url,
            "POST",
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        if response.get("error"):
            raise ProviderError(f"Solana {method}: {response['error']}")
        return response.get("result")

    async def initial_event(self, target: dict[str, Any]) -> CanonicalEvent | None:
        """Read the account once so a quiet curve still has a truthful T0 state."""
        result = await self._rpc(
            "getAccountInfo",
            [
                target["bonding_curve_address"],
                {"encoding": "base64", "commitment": "confirmed"},
            ],
        )
        if not isinstance(result, dict) or not result.get("value"):
            return None
        return self.parse_notification({"params": {"result": result}}, target)

    def parse_notification(
        self, payload: dict[str, Any], target: dict[str, Any], received: str | None = None
    ) -> CanonicalEvent:
        received = received or iso()
        result = (payload.get("params") or {}).get("result") or {}
        context = result.get("context") or {}
        value = result.get("value") or {}
        raw = decode_account_data(value.get("data"))
        decoded = decode_bonding_curve_account(raw)
        decoded["bonding_curve"] = target["bonding_curve_address"]
        return CanonicalEvent.create(
            CanonicalEventType.BONDING_CURVE_STATE,
            str(target["token_address"]),
            "solana",
            "pumpfun",
            self.name,
            received,
            received_timestamp=received,
            available_timestamp=iso(),
            slot_or_block=context.get("slot"),
            source_event_id=f"{target['bonding_curve_address']}:{context.get('slot')}",
            raw_provenance={
                "transport": "solana_accountSubscribe",
                "account": target["bonding_curve_address"],
                "encoding": "base64",
                "timestamp_source": "received_at_no_block_time",
                "account_owner": value.get("owner"),
                "account_space": value.get("space") or len(raw),
            },
            payload=decoded,
        )

    async def run_events(self, emit: Emit, stop: asyncio.Event) -> None:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=self.client.timeout)
        while not stop.is_set():
            try:
                async with (
                    aiohttp.ClientSession(timeout=timeout) as session,
                    session.ws_connect(
                        self.websocket_url, heartbeat=30, autoping=True
                    ) as websocket,
                ):
                    subscriptions: dict[int, dict[str, Any]] = {}
                    subscribed_accounts: set[str] = set()
                    request_id = 0
                    last_valid = datetime.now(UTC)
                    self.health_sequence += 1
                    await emit(
                        _health_event(
                            self.name,
                            ProviderState.CONNECTED,
                            sequence=self.health_sequence,
                            counters={"reconnect_attempts": self.reconnects},
                        )
                    )
                    while not stop.is_set():
                        for target in self.targets():
                            account = str(target.get("bonding_curve_address") or "")
                            if not account or account in subscribed_accounts:
                                continue
                            request_id += 1
                            await websocket.send_json(
                                {
                                    "jsonrpc": "2.0",
                                    "id": request_id,
                                    "method": "accountSubscribe",
                                    "params": [
                                        account,
                                        {"encoding": "base64", "commitment": "confirmed"},
                                    ],
                                }
                            )
                            response = await asyncio.wait_for(websocket.receive_json(), timeout=15)
                            if response.get("error"):
                                raise ProviderError(str(response["error"]))
                            subscriptions[int(response["result"])] = dict(target)
                            subscribed_accounts.add(account)
                            snapshot = await self.initial_event(target)
                            if snapshot is not None:
                                await emit(snapshot)
                                self.events_received += 1
                                last_valid = datetime.now(UTC)
                        try:
                            message = await asyncio.wait_for(
                                websocket.receive(), timeout=self.refresh_seconds
                            )
                        except TimeoutError:
                            if (
                                subscriptions
                                and (datetime.now(UTC) - last_valid).total_seconds()
                                > self.silence_seconds
                            ):
                                raise ProviderError("SILENCE_WATCHDOG_EXPIRED")
                            continue
                        if message.type == aiohttp.WSMsgType.TEXT:
                            payload = json.loads(message.data)
                            subscription = (payload.get("params") or {}).get("subscription")
                            target = subscriptions.get(int(subscription)) if subscription else None
                            if target:
                                await emit(self.parse_notification(payload, target))
                                self.events_received += 1
                                last_valid = datetime.now(UTC)
                        elif message.type in {
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.ERROR,
                        }:
                            raise ProviderError(f"curve websocket closed: {message.type}")
            except (aiohttp.ClientError, ProviderError, json.JSONDecodeError, ValueError) as exc:
                self.errors += 1
                self.reconnects += 1
                state = (
                    ProviderState.STALE
                    if "SILENCE_WATCHDOG" in str(exc)
                    else ProviderState.DISCONNECTED
                )
                self.health_sequence += 1
                await emit(
                    _health_event(
                        self.name,
                        state,
                        sequence=self.health_sequence,
                        error=_redacted_error(exc),
                        counters={
                            "error_count": self.errors,
                            "reconnect_attempts": self.reconnects,
                            "events_received": self.events_received,
                            "gap_detected_at": iso(),
                        },
                    )
                )
                try:
                    await asyncio.wait_for(
                        stop.wait(), timeout=min(60, 2 ** min(self.reconnects, 5))
                    )
                except TimeoutError:
                    pass


class HeliusCuratedSource:
    """Selective free-tier-compatible Helius standard RPC account feed.

    ``transactionSubscribe`` is deliberately not used. Current standard WebSocket
    traffic is free-plan available but credit-metered; ``logsSubscribe`` notifications
    trigger narrowly scoped ``getTransaction`` enrichment. When Helius is configured,
    native Pump also uses Helius as primary transport and public Solana RPC is fallback.
    """

    name = "helius_curated"

    def __init__(
        self,
        api_key: str,
        accounts: Iterable[str],
        silence_seconds: float = 540,
    ):
        self.accounts = tuple(dict.fromkeys(str(value) for value in accounts if value))
        if not api_key or not self.accounts:
            raise ValueError("Helius curated feed needs an API key and at least one account")
        self.rpc_url = f"https://mainnet.helius-rpc.com/?api-key={quote_plus(api_key)}"
        self.websocket_url = f"wss://mainnet.helius-rpc.com/?api-key={quote_plus(api_key)}"
        self.silence_seconds = silence_seconds
        self.health_sequence = 0
        self.events_received = 0
        self.errors = 0
        self.reconnects = 0
        self.bytes_received = 0
        self.rpc_requests = 0
        self.rate_limits = 0

    def parse_message(
        self, payload: dict[str, Any], received: str | None = None
    ) -> list[CanonicalEvent]:
        received = received or iso()
        result = (payload.get("params") or {}).get("result") or {}
        context = result.get("context") or {}
        value = result.get("value") or result
        transaction = value.get("transaction") or {}
        meta = value.get("meta") or transaction.get("meta") or {}
        if meta.get("err") is not None:
            return []
        signature = value.get("signature")
        if not signature:
            signatures = transaction.get("signatures") or []
            signature = signatures[0] if signatures else None
        pre = meta.get("preTokenBalances") or []
        post = meta.get("postTokenBalances") or []
        before = {
            (str(row.get("owner")), str(row.get("mint"))): float(
                (row.get("uiTokenAmount") or {}).get("uiAmount") or 0
            )
            for row in pre
            if row.get("owner") and row.get("mint")
        }
        after = {
            (str(row.get("owner")), str(row.get("mint"))): float(
                (row.get("uiTokenAmount") or {}).get("uiAmount") or 0
            )
            for row in post
            if row.get("owner") and row.get("mint")
        }
        source_timestamp = _epoch_timestamp(value.get("blockTime"), received)
        events: list[CanonicalEvent] = []
        for index, ((owner, mint), end) in enumerate(after.items()):
            if owner not in self.accounts:
                continue
            delta = end - before.get((owner, mint), 0.0)
            if delta == 0:
                continue
            common = {
                "canonical_token": mint,
                "chain": "solana",
                "platform": "solana",
                "source": self.name,
                "source_timestamp": source_timestamp,
                "received_timestamp": received,
                "available_timestamp": iso(),
                "slot_or_block": context.get("slot") or value.get("slot"),
                "transaction_signature": str(signature) if signature else None,
                "source_event_id": f"{signature or context.get('slot')}:{owner}:{index}",
                "confidence": 0.8,
                "raw_provenance": {
                    "transport": "helius_standard_logsSubscribe_plus_getTransaction",
                    "filter": "mentions_one_account_per_subscription",
                    "timestamp_source": (
                        "block_time" if value.get("blockTime") is not None else "received_at"
                    ),
                    "paid_enhanced_transaction_subscribe": False,
                },
                "payload": {
                    "actor": owner,
                    "side": "buy" if delta > 0 else "sell",
                    "token_amount": abs(delta),
                    "curated_account": True,
                    "sol_amount": None,
                },
            }
            events.append(
                CanonicalEvent.create(
                    CanonicalEventType.WALLET_BUY if delta > 0 else CanonicalEventType.WALLET_SELL,
                    **common,
                )
            )
        return events

    async def _transaction(
        self, session: aiohttp.ClientSession, signature: str, slot: int | None
    ) -> dict[str, Any] | None:
        body = {
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
        for attempt in range(3):
            self.rpc_requests += 1
            async with session.post(self.rpc_url, json=body) as response:
                if response.status == 429:
                    self.rate_limits += 1
                    if attempt < 2:
                        retry = min(
                            30.0,
                            float(response.headers.get("Retry-After", 2**attempt))
                            + random.random(),
                        )
                        await asyncio.sleep(retry)
                        continue
                    raise ProviderError("HELIUS_RATE_LIMITED_AFTER_BOUNDED_BACKOFF")
                if response.status >= 400:
                    raise ProviderError(f"Helius getTransaction HTTP {response.status}")
                payload = await response.json()
            if payload.get("error"):
                raise ProviderError(f"Helius getTransaction: {payload['error']}")
            transaction = payload.get("result")
            if transaction:
                return {
                    "params": {
                        "result": {
                            "context": {"slot": slot},
                            "value": transaction,
                        }
                    }
                }
            if attempt < 2:
                await asyncio.sleep(0.2 * (attempt + 1))
        return None

    async def run_events(self, emit: Emit, stop: asyncio.Event) -> None:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15)
        while not stop.is_set():
            try:
                async with (
                    aiohttp.ClientSession(timeout=timeout) as session,
                    session.ws_connect(
                        self.websocket_url, heartbeat=60, autoping=True
                    ) as websocket,
                ):
                    subscriptions: dict[int, str] = {}
                    for request_id, account in enumerate(self.accounts, start=1):
                        await websocket.send_json(
                            {
                                "jsonrpc": "2.0",
                                "id": request_id,
                                "method": "logsSubscribe",
                                "params": [
                                    {"mentions": [account]},
                                    {"commitment": "confirmed"},
                                ],
                            }
                        )
                        response = await asyncio.wait_for(websocket.receive_json(), timeout=15)
                        if response.get("error") or response.get("result") is None:
                            raise ProviderError(
                                f"Helius standard logsSubscribe rejected: {response}"
                            )
                        subscriptions[int(response["result"])] = account
                    self.health_sequence += 1
                    await emit(
                        _health_event(
                            self.name,
                            ProviderState.CONNECTED,
                            sequence=self.health_sequence,
                            counters={
                                "metadata": {
                                    "curated_accounts": len(self.accounts),
                                    "transport": "standard_rpc",
                                    "enhanced_paid_feed": False,
                                },
                            },
                        )
                    )
                    while not stop.is_set():
                        message = await asyncio.wait_for(
                            websocket.receive(), timeout=self.silence_seconds
                        )
                        if message.type == aiohttp.WSMsgType.TEXT:
                            self.bytes_received += len(message.data.encode())
                            payload = json.loads(message.data)
                            result = (payload.get("params") or {}).get("result") or {}
                            value = result.get("value") or {}
                            signature = str(value.get("signature") or "")
                            subscription = (payload.get("params") or {}).get("subscription")
                            if not signature or int(subscription or -1) not in subscriptions:
                                continue
                            transaction = await self._transaction(
                                session,
                                signature,
                                (result.get("context") or {}).get("slot"),
                            )
                            if transaction is None:
                                continue
                            for event in self.parse_message(transaction):
                                await emit(event)
                                self.events_received += 1
                        elif message.type in {
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.ERROR,
                        }:
                            raise ProviderError(f"Helius websocket closed: {message.type}")
            except TimeoutError:
                self.errors += 1
                state, error = ProviderState.STALE, "HELIUS_INACTIVITY_TIMEOUT"
            except (aiohttp.ClientError, ProviderError, json.JSONDecodeError) as exc:
                self.errors += 1
                state, error = ProviderState.DISCONNECTED, _redacted_error(exc)
            else:
                return
            self.reconnects += 1
            self.health_sequence += 1
            await emit(
                _health_event(
                    self.name,
                    state,
                    sequence=self.health_sequence,
                    error=error,
                    counters={
                        "error_count": self.errors,
                        "reconnect_attempts": self.reconnects,
                        "events_received": self.events_received,
                        "rate_limit_count": self.rate_limits,
                        "gap_detected_at": iso(),
                        "metadata": {
                            "standard_rpc_requests": self.rpc_requests,
                            "bytes_received": self.bytes_received,
                            "enhanced_paid_feed": False,
                        },
                    },
                )
            )
            try:
                await asyncio.wait_for(stop.wait(), timeout=min(60, 2 ** min(self.reconnects, 5)))
            except TimeoutError:
                pass

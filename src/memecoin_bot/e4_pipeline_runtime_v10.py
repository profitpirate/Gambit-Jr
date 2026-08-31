from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

import aiohttp

from .e4_pipelines_v10 import E4_WALLET, manager

LOGGER = logging.getLogger("gambit.e4.pipeline-runtime.v10")


def _csv_env(name: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(part.strip() for part in os.getenv(name, "").split(",") if part.strip()))


def _json_mapping(raw: bytes | str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _token_totals(rows: list[Mapping[str, Any]], owner: str) -> dict[str, float]:
    totals: dict[str, float] = {}
    for row in rows or []:
        if str(row.get("owner") or "") != owner:
            continue
        mint = str(row.get("mint") or "")
        amount = row.get("uiTokenAmount") or {}
        value = amount.get("uiAmountString", amount.get("uiAmount", 0))
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            number = 0.0
        if mint:
            totals[mint] = totals.get(mint, 0.0) + number
    return totals


@dataclass(slots=True)
class RuntimeMetrics:
    udp_messages: int = 0
    social_messages: int = 0
    social_reconnects: int = 0
    e4_notifications: int = 0
    e4_transactions: int = 0
    e4_transaction_misses: int = 0
    errors: int = 0
    last_error: str = ""
    started_ns: int = field(default_factory=time.time_ns)


class SignalDatagramProtocol(asyncio.DatagramProtocol):
    def __init__(self, runtime: "PipelineRuntime") -> None:
        self.runtime = runtime

    def datagram_received(self, data: bytes, _addr: tuple[str, int]) -> None:
        payload = _json_mapping(data)
        if payload:
            self.runtime.metrics.udp_messages += 1
            self.runtime.accept_signal(payload)

    def error_received(self, exc: Exception) -> None:
        self.runtime.record_error(exc)


class PipelineRuntime:
    """Background I/O for the three V10 pipelines.

    The trading hot path never performs this I/O. The supervisor updates the
    same-process immutable snapshots consumed by ``e4_hardening_v10``.
    """

    def __init__(self) -> None:
        self.pipelines = manager()
        self.metrics = RuntimeMetrics()
        self.stop_event = asyncio.Event()
        self.rpc_urls = _csv_env("E4_PIPELINE_SOLANA_RPC_URLS") or tuple(
            url
            for url in (
                os.getenv("HELIUS_RPC_URL", ""),
                os.getenv("SOLANA_RPC_URL", ""),
                "https://solana-rpc.publicnode.com",
            )
            if url
        )
        self.ws_urls = _csv_env("E4_PIPELINE_SOLANA_WS_URLS")
        # Enhanced transaction streams deliver the full transaction in the
        # notification and avoid the slow logsSubscribe -> getTransaction hop.
        self.transaction_ws_urls = _csv_env("E4_PIPELINE_TRANSACTION_WS_URLS")
        self.social_stream_url = os.getenv("E4_SOCIAL_STREAM_URL", "").strip()
        self.social_bearer = os.getenv("E4_SOCIAL_STREAM_BEARER_TOKEN", "").strip()
        self.udp_host = os.getenv("E4_PIPELINE_UDP_HOST", "127.0.0.1")
        self.udp_port = int(os.getenv("E4_PIPELINE_UDP_PORT", "19104"))
        self.rpc_cursor = 0
        self.request_id = 0
        self.session: aiohttp.ClientSession | None = None

    def record_error(self, exc: BaseException) -> None:
        self.metrics.errors += 1
        self.metrics.last_error = f"{type(exc).__name__}: {exc}"
        LOGGER.warning("E4 V10 pipeline runtime error: %s", self.metrics.last_error)

    def accept_signal(self, payload: Mapping[str, Any]) -> None:
        kind = str(payload.get("kind") or payload.get("type") or payload.get("event_type") or "").lower()
        if any(marker in kind for marker in ("social", "tweet", "x_post", "narrative")):
            self.pipelines.observe_social_post(payload)
            return
        if any(marker in kind for marker in ("intent", "prearmed", "authorized_launch")):
            self.pipelines.register_authorized_intent(payload)
            return
        if "e4" in kind and any(marker in kind for marker in ("buy", "entry", "trade")):
            self.pipelines.observe_e4_entry(payload)
            return
        if "e4" in kind and any(marker in kind for marker in ("sell", "exit")):
            self.pipelines.observe_e4_exit(str(payload.get("mint") or ""))
            return
        if kind in {"reload", "model_reload"}:
            self.pipelines.reload_models()

    async def rpc(self, method: str, params: list[Any], retries: int = 3) -> Any:
        if not self.rpc_urls or self.session is None:
            return None
        last: Exception | None = None
        for attempt in range(max(1, retries) * len(self.rpc_urls)):
            url = self.rpc_urls[(self.rpc_cursor + attempt) % len(self.rpc_urls)]
            self.request_id += 1
            try:
                async with self.session.post(
                    url,
                    json={"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params},
                ) as response:
                    text = await response.text()
                    if response.status == 429 or response.status >= 500:
                        raise RuntimeError(f"HTTP {response.status}")
                    if response.status >= 400:
                        raise RuntimeError(f"HTTP {response.status}: {text[:160]}")
                    payload = json.loads(text)
                    if payload.get("error"):
                        raise RuntimeError(str(payload["error"]))
                    self.rpc_cursor = (self.rpc_urls.index(url) + 1) % len(self.rpc_urls)
                    return payload.get("result")
            except Exception as exc:
                last = exc
                await asyncio.sleep(min(0.08, 0.008 * (attempt + 1)))
        if last:
            self.record_error(last)
        return None

    async def fetch_transaction(self, signature: str) -> Mapping[str, Any] | None:
        for delay in (0.0, 0.015, 0.030, 0.060):
            if delay:
                await asyncio.sleep(delay)
            value = await self.rpc(
                "getTransaction",
                [
                    signature,
                    {
                        "encoding": "jsonParsed",
                        "commitment": "processed",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
                retries=1,
            )
            if isinstance(value, Mapping):
                return value
        self.metrics.e4_transaction_misses += 1
        return None

    def process_e4_transaction(self, signature: str, tx: Mapping[str, Any]) -> None:
        meta = tx.get("meta") or {}
        if meta.get("err") is not None:
            return
        pre = _token_totals(list(meta.get("preTokenBalances") or []), E4_WALLET)
        post = _token_totals(list(meta.get("postTokenBalances") or []), E4_WALLET)
        changes = []
        for mint in set(pre) | set(post):
            delta = post.get(mint, 0.0) - pre.get(mint, 0.0)
            if abs(delta) > max(1e-8, abs(pre.get(mint, 0.0)) * 1e-10):
                changes.append((mint, delta))
        for mint, delta in changes:
            learning = self.pipelines._learning.get(mint)  # same-process hot cache
            creator = learning.creator if learning else ""
            price = learning.latest_price_sol if learning else 0.0
            if delta > 0:
                self.pipelines.observe_e4_entry(
                    {
                        "kind": "e4_buy",
                        "mint": mint,
                        "creator": creator,
                        "observed_ns": time.time_ns(),
                        "entry_price_sol": price,
                        "signature": signature,
                    }
                )
            else:
                self.pipelines.observe_e4_exit(mint)
        self.metrics.e4_transactions += 1

    async def e4_ws_worker(self, url: str) -> None:
        assert self.session is not None
        while not self.stop_event.is_set():
            try:
                async with self.session.ws_connect(
                    url,
                    heartbeat=10,
                    max_msg_size=4 * 1024 * 1024,
                ) as ws:
                    await ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "logsSubscribe",
                            "params": [{"mentions": [E4_WALLET]}, {"commitment": "processed"}],
                        }
                    )
                    async for message in ws:
                        if self.stop_event.is_set():
                            break
                        if message.type != aiohttp.WSMsgType.TEXT:
                            continue
                        payload = _json_mapping(message.data)
                        value = (((payload.get("params") or {}).get("result") or {}).get("value") or {})
                        signature = str(value.get("signature") or "")
                        if not signature or value.get("err") is not None:
                            continue
                        self.metrics.e4_notifications += 1
                        tx = await self.fetch_transaction(signature)
                        if tx:
                            self.process_e4_transaction(signature, tx)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.record_error(exc)
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=0.25)
                except asyncio.TimeoutError:
                    pass

    async def e4_transaction_ws_worker(self, url: str) -> None:
        """Consume provider transactionSubscribe notifications in one hop."""
        assert self.session is not None
        while not self.stop_event.is_set():
            try:
                async with self.session.ws_connect(
                    url,
                    heartbeat=10,
                    max_msg_size=16 * 1024 * 1024,
                ) as ws:
                    await ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "transactionSubscribe",
                            "params": [
                                {
                                    "accountInclude": [E4_WALLET],
                                    "failed": False,
                                    "vote": False,
                                },
                                {
                                    "commitment": "processed",
                                    "encoding": "jsonParsed",
                                    "transactionDetails": "full",
                                    "showRewards": False,
                                    "maxSupportedTransactionVersion": 0,
                                },
                            ],
                        }
                    )
                    async for message in ws:
                        if self.stop_event.is_set():
                            break
                        if message.type != aiohttp.WSMsgType.TEXT:
                            continue
                        envelope = _json_mapping(message.data)
                        result = ((envelope.get("params") or {}).get("result") or {})
                        value = result.get("value") if isinstance(result, Mapping) else None
                        if not isinstance(value, Mapping):
                            continue
                        signature = str(
                            value.get("signature")
                            or ((value.get("transaction") or {}).get("signatures") or [""])[0]
                            or ""
                        )
                        tx = value.get("transaction") if isinstance(value.get("transaction"), Mapping) else value
                        if signature and isinstance(tx, Mapping):
                            # Providers differ: some put meta alongside transaction,
                            # others wrap both under value. Normalize for one parser.
                            normalized = dict(tx)
                            if "meta" not in normalized and isinstance(value.get("meta"), Mapping):
                                normalized["meta"] = value["meta"]
                            self.metrics.e4_notifications += 1
                            self.process_e4_transaction(signature, normalized)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.record_error(exc)
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=0.20)
                except asyncio.TimeoutError:
                    pass

    async def social_stream_worker(self) -> None:
        assert self.session is not None
        headers = {"Accept": "application/json"}
        if self.social_bearer:
            headers["Authorization"] = f"Bearer {self.social_bearer}"
        while not self.stop_event.is_set():
            try:
                timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=90)
                async with self.session.get(
                    self.social_stream_url,
                    headers=headers,
                    timeout=timeout,
                ) as response:
                    if response.status >= 400:
                        raise RuntimeError(f"social stream HTTP {response.status}: {(await response.text())[:200]}")
                    async for raw in response.content:
                        if self.stop_event.is_set():
                            break
                        payload = _json_mapping(raw)
                        if not payload:
                            continue
                        # Accept direct canonical payloads and X API v2 envelopes.
                        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
                        includes = payload.get("includes") if isinstance(payload.get("includes"), Mapping) else {}
                        users = includes.get("users") if isinstance(includes.get("users"), list) else []
                        author = users[0] if users and isinstance(users[0], Mapping) else {}
                        merged = {
                            "kind": "social_post",
                            "id": data.get("id"),
                            "text": data.get("text") or payload.get("text"),
                            "created_ns": data.get("created_ns") or payload.get("created_ns") or time.time_ns(),
                            "handle": author.get("username") or payload.get("handle"),
                            "followers": ((author.get("public_metrics") or {}).get("followers_count") if isinstance(author.get("public_metrics"), Mapping) else None) or payload.get("followers"),
                            "authority": payload.get("authority"),
                            "novelty": payload.get("novelty"),
                            "engagement_velocity": payload.get("engagement_velocity"),
                            "source": payload.get("source") or "social_stream",
                        }
                        if self.pipelines.observe_social_post(merged):
                            self.metrics.social_messages += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.metrics.social_reconnects += 1
                self.record_error(exc)
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass

    async def model_reload_worker(self) -> None:
        interval = max(5.0, float(os.getenv("E4_PIPELINE_MODEL_RELOAD_SECONDS", "30")))
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                self.pipelines.reload_models()

    async def run(self) -> None:
        timeout = aiohttp.ClientTimeout(total=8, sock_connect=4, sock_read=8)
        connector = aiohttp.TCPConnector(
            limit=64,
            ttl_dns_cache=600,
            keepalive_timeout=60,
            enable_cleanup_closed=True,
        )
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            self.session = session
            loop = asyncio.get_running_loop()
            transport, _protocol = await loop.create_datagram_endpoint(
                lambda: SignalDatagramProtocol(self),
                local_addr=(self.udp_host, self.udp_port),
            )
            tasks: list[asyncio.Task[Any]] = [
                asyncio.create_task(self.model_reload_worker(), name="e4-v10-model-reload")
            ]
            tasks.extend(
                asyncio.create_task(self.e4_ws_worker(url), name=f"e4-v10-wallet-{index}")
                for index, url in enumerate(self.ws_urls)
            )
            tasks.extend(
                asyncio.create_task(
                    self.e4_transaction_ws_worker(url),
                    name=f"e4-v10-transaction-stream-{index}",
                )
                for index, url in enumerate(self.transaction_ws_urls)
            )
            if self.social_stream_url:
                tasks.append(asyncio.create_task(self.social_stream_worker(), name="e4-v10-social"))
            try:
                await self.stop_event.wait()
            finally:
                transport.close()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self.session = None

    def stop(self) -> None:
        self.stop_event.set()


_RUNTIME: PipelineRuntime | None = None
_THREAD: threading.Thread | None = None
_START_LOCK = threading.Lock()


def start_background_supervisor() -> PipelineRuntime | None:
    """Start once in the funded process; disabled with E4_PIPELINES_BACKGROUND=false."""
    global _RUNTIME, _THREAD
    if os.getenv("E4_PIPELINES_BACKGROUND", "true").strip().lower() in {"0", "false", "no", "off"}:
        return None
    with _START_LOCK:
        if _RUNTIME is not None:
            return _RUNTIME
        runtime = PipelineRuntime()

        def target() -> None:
            try:
                asyncio.run(runtime.run())
            except Exception as exc:
                runtime.record_error(exc)

        thread = threading.Thread(target=target, name="e4-v10-pipeline-supervisor", daemon=True)
        thread.start()
        _RUNTIME = runtime
        _THREAD = thread
        return runtime


def runtime_snapshot() -> dict[str, Any]:
    runtime = _RUNTIME
    return {
        "running": bool(runtime and _THREAD and _THREAD.is_alive()),
        "runtime": {
            "udp_messages": runtime.metrics.udp_messages,
            "social_messages": runtime.metrics.social_messages,
            "social_reconnects": runtime.metrics.social_reconnects,
            "e4_notifications": runtime.metrics.e4_notifications,
            "e4_transactions": runtime.metrics.e4_transactions,
            "e4_transaction_misses": runtime.metrics.e4_transaction_misses,
            "errors": runtime.metrics.errors,
            "last_error": runtime.metrics.last_error,
            "started_ns": runtime.metrics.started_ns,
        } if runtime else {},
        "pipelines": manager().snapshot(),
    }

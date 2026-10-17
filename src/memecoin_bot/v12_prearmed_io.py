"""Read-only feed, unsigned builder, and journaled execution for the frozen runner.

Constructing LiveExecution requires explicit live settings. Shadow mode never
constructs it and never opens the signing key. Network errors are redacted.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import math
import socket
import time
import uuid
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from .realtime.pumpfun import PUMP_PROGRAM_ID, anchor_events_from_logs
from .v12_prearmed_diagnostics import DiagnosticError, failure
from .v12_prearmed_discovery_budget import STREAM_LEASE_BYTES, provider_name, public_first
from .v12_prearmed_ipfs import ipfs_metadata, root_cid


from .v12_prearmed_timing import BUILDER_TIMINGS


class Rpc:
    def __init__(self, session, urls, *, discovery_budget=None):
        self.session, self.urls = session, tuple(dict.fromkeys(urls))
        self.trace = lambda *args, **kwargs: None
        self.provider_state = {}
        self.discovery_budget = discovery_budget
        if not self.urls or any(urlsplit(u).scheme != "https" for u in self.urls):
            raise ValueError("HTTPS RPC endpoints required")

    async def call(self, method, params, *, url=None):
        endpoints = [url] if url else self.urls
        if self.discovery_budget is not None and method == "getBalance" and url is None:
            endpoints = public_first(endpoints)
        for endpoint in endpoints:
            index = self.urls.index(endpoint)
            health = self.provider_state.setdefault(
                index, {"provider_index": index, "health": "UNTESTED", "failures": 0}
            )
            if time.monotonic() < health.get("_retry_after", 0):
                continue
            if self.discovery_budget is not None and method == "getBalance":
                if not self.discovery_budget.reserve_balance(endpoint):
                    if not health.get("balance_budget_exhausted"):
                        self.trace("DISCOVERY_BUDGET_EXHAUSTED", stage="rpc", category="blocked",
                                   reason="PAID_BALANCE_FALLBACK_LIMIT", provider_index=index)
                    health["balance_budget_exhausted"] = True
                    continue
            try:
                async with self.session.post(
                    endpoint,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                    timeout=aiohttp.ClientTimeout(total=2 if self.discovery_budget is not None and method == "getBalance" else 8),
                ) as response:
                    if response.status != 200:
                        if response.status in (413, 429):
                            health["_retry_after"] = time.monotonic() + retry_after_seconds(
                                getattr(response, 'headers', {}).get('Retry-After'), default=60
                            )
                        raise DiagnosticError("RPC_HTTP_REJECTED", http_status=response.status)
                    result = await response.json()
                    if "error" not in result and "result" in result:
                        health.update(health="AVAILABLE", last_success_ns=time.time_ns())
                        if method == "getBalance":
                            health.pop("balance_budget_exhausted", None)
                        return result["result"]
                    health.update(
                        health="DEGRADED",
                        last_failure_ns=time.time_ns(),
                        failures=health["failures"] + 1,
                    )
                    code = (result.get("error") or {}).get("code")
                    data = (result.get("error") or {}).get("data")
                    instruction = (data.get("err") or {}).get("InstructionError") if isinstance(data, dict) and isinstance(data.get("err"), dict) else None
                    rejection = {}
                    if isinstance(instruction, list) and len(instruction) == 2 and type(instruction[0]) is int:
                        rejection["rpc_instruction_index"] = instruction[0]
                        if isinstance(instruction[1], dict) and type(instruction[1].get("Custom")) is int:
                            rejection["rpc_custom_error"] = instruction[1]["Custom"]
                    if code == -32003:
                        health["_retry_after"] = time.monotonic() + 60
                    self.trace(
                        "RPC_REQUEST_FAILED",
                        stage="rpc",
                        reason="RPC_SERVER_REJECTED",
                        category="fault",
                        provider_index=self.urls.index(endpoint),
                        rpc_method=method,
                        rpc_code=code if type(code) is int else None,
                        **rejection,
                    )
            except (aiohttp.ClientError, TimeoutError, ValueError) as error:
                health.update(
                    health="DEGRADED",
                    last_failure_ns=time.time_ns(),
                    failures=health["failures"] + 1,
                    http_status=getattr(error, "http_status", None),
                )
                self.trace(
                    "RPC_REQUEST_FAILED",
                    stage="rpc",
                    category="fault",
                    provider_index=self.urls.index(endpoint),
                    rpc_method=method,
                    **failure("rpc", error),
                )
                continue
        raise RuntimeError("RPC_UNAVAILABLE:" + method.upper())

    def snapshot(self):
        return [
            {k: v for k, v in value.items() if not k.startswith("_")}
            for value in self.provider_state.values()
        ]

    async def balance_lamports(self, wallet):
        result = await self.call("getBalance", [wallet, {"commitment": "confirmed"}])
        value = result["value"]
        if type(value) is not int or value < 0:
            raise RuntimeError("INVALID_WALLET_BALANCE")
        self.last_balance_slot = (result.get("context") or {}).get("slot")
        return value


class Builder:
    def __init__(self, command):
        self.command = tuple(command)
        self.process = None
        self.lock = asyncio.Lock()

    async def request(self, payload):
        async with self.lock:
            if self.process is None or self.process.returncode is not None:
                self.process = await asyncio.create_subprocess_exec(
                    *self.command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            request_id = str(uuid.uuid4())
            self.process.stdin.write(
                (json.dumps({**payload, "request_id": request_id}) + "\n").encode()
            )
            try:
                await self.process.stdin.drain()
                line = await asyncio.wait_for(self.process.stdout.readline(), 20)
                result = json.loads(line)
                if result.get("request_id") != request_id:
                    raise RuntimeError("BUILDER_RESPONSE_MISMATCH")
                if result.get("error"):
                    error = RuntimeError("BUILDER:" + str(result["error"]))
                    error.source_location = result.get("source_location")
                    error.remote_error_type = result.get("error_type")
                    error.builder_timings = result.get("builder_timings") or {}
                    error.quote_facts = result.get("quote_facts") or {}
                    error.provider_index = result.get("provider_index")
                    raise error
                return result["result"]
            except (TimeoutError, ValueError, BrokenPipeError):
                await self.close()
                raise RuntimeError("BUILDER_UNAVAILABLE") from None
            except asyncio.CancelledError:
                # A cancelled request must not leave a reply for the next request.
                await self.close()
                raise

    async def close(self):
        process, self.process = self.process, None
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()


class BuilderLanes:
    """Exit construction never queues behind metadata quotes or entry building."""

    def __init__(self, command):
        self.quote, self.buy, self.sell = (Builder(command) for _ in range(3))

    async def request(self, payload):
        lane = (
            self.sell
            if payload.get("side") == "SELL"
            else (self.buy if payload.get("action") == "BUILD" else self.quote)
        )
        return await lane.request(payload)

    async def warmup(self):
        # Load the SDK in every isolated lane before admitting entries. This
        # local handshake performs no RPC reads, signing or transaction sends.
        for lane in (self.quote, self.buy, self.sell):
            result = await lane.request({"action": "WARMUP"})
            if result.get("ok") is not True:
                raise RuntimeError("BUILDER_WARMUP_FAILED")

    async def close(self):
        await asyncio.gather(self.quote.close(), self.buy.close(), self.sell.close())


def receipt(transaction, *, wallet, mint, side, submitted_ns):
    """Use one confirmed transaction, never differences between unrelated RPC reads."""
    meta = transaction.get("meta") or {}
    if meta.get("err") is not None:
        return {"status": "FAILED", "reason": "CHAIN_REJECTED", "fee_lamports": meta.get("fee", 0)}
    keys = transaction["transaction"]["message"]["accountKeys"]
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in keys]
    if keys[0] != wallet:
        raise RuntimeError("UNEXPECTED_TRANSACTION_PAYER")

    def tokens(rows):
        total = 0
        for row in rows:
            if row.get("owner") == wallet and row.get("mint") == mint:
                amount = row["uiTokenAmount"]
                if amount["decimals"] != 6:
                    raise RuntimeError("UNEXPECTED_TOKEN_DECIMALS")
                total += int(amount["amount"])
        return total

    before = tokens(meta.get("preTokenBalances", []))
    after = tokens(meta.get("postTokenBalances", []))
    delta = after - before
    if (side == "BUY" and delta <= 0) or (side == "SELL" and delta >= 0):
        raise RuntimeError("CONFIRMED_TRANSACTION_WITHOUT_EXPECTED_TOKEN_DELTA")
    reference = None
    if side == "BUY":
        matches = [
            e
            for e in anchor_events_from_logs(meta.get("logMessages", []), PUMP_PROGRAM_ID)
            if e.get("anchor_event") == "TradeEvent"
            and e.get("mint") == mint
            and e.get("user") == wallet
            and e.get("is_buy") is True
        ]
        if len(matches) == 1:
            event = matches[0]
            pre_sol = event["virtual_sol_reserves"] - event["sol_amount"]
            pre_tokens = event["virtual_token_reserves"] + event["token_amount"]
            if pre_sol > 0 and pre_tokens > 0:
                reference = pre_sol / pre_tokens / 1000

    def wrapped(rows):
        return sum(
            int(r["uiTokenAmount"]["amount"])
            for r in rows
            if r.get("owner") == wallet
            and r.get("mint") == "So11111111111111111111111111111111111111112"
        )

    sol_delta = (
        int(meta["postBalances"][0])
        - int(meta["preBalances"][0])
        + wrapped(meta.get("postTokenBalances", []))
        - wrapped(meta.get("preTokenBalances", []))
    )
    return {
        "status": "CONFIRMED",
        "tokens_raw": abs(delta),
        "sol_delta_lamports": sol_delta,
        "reference_price": reference,
        "submitted_ns": submitted_ns,
        "confirmed_ns": time.time_ns(),
        "slot": transaction["slot"],
    }


class LiveExecution:
    trace = staticmethod(lambda *args, **kwargs: None)

    def __init__(self, *, settings, rpc, builder, journal):
        if settings.live is not True:
            raise PermissionError("LIVE_EXECUTION_DISABLED")
        from .e4_live import Signer

        self.signer = Signer(settings)
        self.rpc, self.builder, self.journal = rpc, builder, journal
        self.wallet = settings.wallet
        self.trace = lambda *args, **kwargs: None
        self.allow_buy = lambda: (
            False
        )  # The runner must attach its current stop/feed/safety checks.

    async def execute(self, request_id, payload):
        execution_started_ns = time.time_ns()
        entry = self.journal.prepare(request_id, payload)
        if entry.state == "FAILED_TERMINAL":
            return {"status": "FAILED", "reason": "PREVIOUS_FAILURE"}
        if entry.signature:
            return await self.resolve(entry, wait_seconds=1)
        stage = "build"
        self.trace(
            "EXECUTION_STARTED",
            stage=stage,
            reason="ORDER_PREPARED",
            request_id=request_id,
            mint=payload["mint"],
            side=payload["side"],
        )
        try:
            if payload["side"] == "BUY" and not self.allow_buy():
                raise RuntimeError("ENTRY_NO_LONGER_ALLOWED")
            if (
                payload["side"] == "BUY"
                and payload.get("expires_ns") is not None
                and time.time_ns() > payload["expires_ns"]
            ):
                raise RuntimeError("ENTRY_EXPIRED")
            built = await self.builder.request(
                {**payload, "action": "BUILD", "public_key": self.wallet}
            )
            if built.get("simulation_passed") is not True:
                raise RuntimeError("SIMULATION_REQUIRED")
            if payload["side"] == "BUY" and not self.allow_buy():
                raise RuntimeError("ENTRY_NO_LONGER_ALLOWED")
            if (
                payload["side"] == "BUY"
                and payload.get("expires_ns") is not None
                and time.time_ns() > payload["expires_ns"]
            ):
                raise RuntimeError("ENTRY_EXPIRED")
            # Key bytes stay inside Signer on the VPS; this path is never used by shadow.
            stage = "sign"
            self.trace(
                "EXECUTION_PROGRESS",
                stage=stage,
                reason="BUILD_AND_SIMULATION_PASSED",
                request_id=request_id,
                mint=payload["mint"],
                started_ns=execution_started_ns,
                **{"builder_" + key: value for key, value in built.get("builder_timings", {}).items()
                   if key in BUILDER_TIMINGS
                   and type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 120000},
                **{key: built[key] for key in ("creator_verification", "curve_creator",
                                                "fee_sharing_admin", "fee_sharing_admin_revoked")
                   if key in built},
            )
            signed, signature = await self.signer.sign(built["transaction_base64"])
            if payload["side"] == "BUY" and not self.allow_buy():
                raise RuntimeError("ENTRY_NO_LONGER_ALLOWED")
            self.journal.mark_signed(
                entry.idempotency_key, signed_tx_b64=signed, signature=signature
            )
        except Exception as error:  # noqa: BLE001 - Boundary records failure and preserves recovery state.
            allowed = {
                "ENTRY_NO_LONGER_ALLOWED",
                "ENTRY_EXPIRED",
                "SIMULATION_REQUIRED",
                "BUILDER:CURVE_MIGRATED",
                "BUILDER:ENTRY_CHASE_GUARD_REJECTED",
                "BUILDER:ENTRY_OUTPUT_GUARD_REJECTED",
                "BUILDER:SIMULATION_FAILED",
                "BUILDER:SIMULATION_BUDGET_EXCEEDED",
            }
            reason = str(error) if str(error) in allowed else failure(stage, error)["reason"]
            self.trace(
                "EXECUTION_FAILED",
                stage=stage,
                category="fault",
                reason=reason,
                request_id=request_id,
                mint=payload["mint"],
                error_type=type(error).__name__,
            )
            self.journal.mark_failed(entry.idempotency_key, reason, terminal=True)
            return {"status": "FAILED", "reason": reason}
        self.journal.mark_submitted(entry.idempotency_key)
        self.trace(
            "EXECUTION_PROGRESS",
            stage="broadcast",
            reason="SIGNED_TRANSACTION_JOURNALED",
            started_ns=execution_started_ns,
            request_id=request_id,
            mint=payload["mint"],
            signature=signature,
        )

        # Every route receives identical bytes; a transport timeout never creates a new transaction.
        async def send(index, url):
            try:
                result = await self.rpc.call(
                    "sendTransaction",
                    [
                        signed,
                        {
                            "encoding": "base64",
                            "skipPreflight": False,
                            "preflightCommitment": "confirmed",
                            "maxRetries": 2,
                        },
                    ],
                    url=url,
                )
                if result != signature:
                    raise RuntimeError("SIGNATURE_MISMATCH")
                self.trace(
                    "ROUTE_ACCEPTED",
                    stage="broadcast",
                    reason="PROVIDER_ACCEPTED_SIGNATURE",
                    started_ns=execution_started_ns,
                    request_id=request_id,
                    provider_index=index,
                    signature=signature,
                )
            except RuntimeError as error:
                self.trace(
                    "ROUTE_FAILED",
                    stage="broadcast",
                    category="fault",
                    request_id=request_id,
                    provider_index=index,
                    **failure("broadcast", error),
                )
                return

        routes = [asyncio.create_task(send(index, url)) for index, url in enumerate(self.rpc.urls)]
        try:
            # A slow secondary provider must not delay observing a landed fill.
            return await self.resolve(self.journal.get(entry.idempotency_key), wait_seconds=15)
        finally:
            for route in routes:
                if not route.done():
                    route.cancel()
            await asyncio.gather(*routes, return_exceptions=True)

    async def resolve(self, entry, *, wait_seconds=0):
        if entry.state == "FAILED_TERMINAL":
            return {"status": "FAILED", "reason": "PREVIOUS_FAILURE"}
        if not entry.signature:
            self.journal.mark_failed(entry.idempotency_key, "NO_SIGNED_TRANSACTION", terminal=True)
            return {"status": "FAILED", "reason": "NO_SIGNED_TRANSACTION"}
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                tx = await self.confirmed_transaction(
                    entry.signature, wait_seconds=max(0, deadline-time.monotonic())
                )
                if tx:
                    result = receipt(
                        tx,
                        wallet=self.wallet,
                        mint=entry.mint,
                        side=entry.side,
                        submitted_ns=entry.payload["submitted_ns"],
                    )
                    if result["status"] == "FAILED":
                        self.journal.mark_failed(
                            entry.idempotency_key, result["reason"], terminal=True
                        )
                    else:
                        self.journal.mark_confirmed(
                            entry.idempotency_key, route="confirmed_rpc", slot=result["slot"]
                        )
                    return {**result, "signature": entry.signature}
            except (RuntimeError, KeyError, TypeError, ValueError) as error:
                self.trace(
                    "RECEIPT_CHECK_FAILED",
                    stage="confirmation",
                    category="fault",
                    request_id=entry.request_id,
                    signature=entry.signature,
                    **failure("confirmation", error),
                )
            if time.monotonic() >= deadline:
                submitted = entry.payload.get("submitted_ns")
                now_ns = time.time_ns()
                checked = getattr(self, "_expiry_checked", {})
                if (type(submitted) is int and now_ns - submitted >= 180_000_000_000
                        and now_ns - checked.get(entry.request_id, 0) >= 30_000_000_000):
                    from .v12_prearmed_expiry import expired_unseen
                    checked[entry.request_id] = now_ns
                    self._expiry_checked = checked
                    evidence = await expired_unseen(entry, self.rpc, now_ns=now_ns)
                    if evidence is not None:
                        reason = "TRANSACTION_EXPIRED_UNSEEN"
                        self.journal.mark_failed(entry.idempotency_key, reason, terminal=True)
                        self.trace("EXECUTION_EXPIRED", stage="confirmation", category="fault",
                                   reason=reason, request_id=entry.request_id, signature=entry.signature)
                        return {"status": "FAILED", "reason": reason, "fee_lamports": 0,
                                "signature": entry.signature}
                return {"status": "UNKNOWN", "signature": entry.signature}
            await asyncio.sleep(0.35)

    async def confirmed_transaction(self, signature, *, wait_seconds=0):
        deadline = time.monotonic() + wait_seconds
        async def query(url):
            while True:
                try:
                    value = await self.rpc.call(
                        "getTransaction",
                        [signature, {"encoding": "jsonParsed", "commitment": "confirmed",
                                     "maxSupportedTransactionVersion": 1}], url=url,
                    )
                    if value:
                        return value
                except RuntimeError:
                    pass
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                # Each provider progresses independently: one null response
                # must not wait for a stalled sibling before checking again.
                await asyncio.sleep(min(0.35, remaining))

        tasks = [asyncio.create_task(query(url)) for url in self.rpc.urls]
        try:
            for ready in asyncio.as_completed(tasks, timeout=wait_seconds if wait_seconds > 0 else None):
                try:
                    value = await ready
                    if value:
                        return value
                except RuntimeError:
                    continue
            return None
        except TimeoutError:
            return None
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


class PublicResolver(aiohttp.abc.AbstractResolver):
    """Metadata never gets RPC credentials or access to private/link-local networks."""

    async def resolve(self, host, port=0, family=socket.AF_INET):
        rows = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=family, type=socket.SOCK_STREAM
        )
        result = []
        for af, _, proto, _, address in rows:
            if not ipaddress.ip_address(address[0]).is_global:
                raise DiagnosticError("METADATA_ADDRESS_BLOCKED")
            result.append(
                {
                    "hostname": host,
                    "host": address[0],
                    "port": port,
                    "family": af,
                    "proto": proto,
                    "flags": socket.AI_NUMERICHOST,
                }
            )
        return result

    async def close(self):
        pass


def metadata_trace_config():
    """Measure connection phases without retaining URLs, headers or content."""
    config = aiohttp.TraceConfig()
    def callback(label):
        async def record(_session, context, _params):
            if isinstance(context.trace_request_ctx, dict):
                context.trace_request_ctx[label] = asyncio.get_running_loop().time()
        return record
    for name, label in (
        ("on_dns_resolvehost_start", "dns_start"), ("on_dns_resolvehost_end", "dns_end"),
        ("on_connection_create_start", "connect_start"), ("on_connection_create_end", "connect_end"),
        ("on_connection_reuseconn", "reused"), ("on_request_headers_sent", "sent"),
        ("on_request_end", "headers"),
    ):
        getattr(config, name).append(callback(label))
    return config


def retry_after_seconds(value, *, default=0):
    """Respect provider backoff without allowing an unbounded local wait."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return default
    return min(3600, max(0, seconds)) if math.isfinite(seconds) else default


async def metadata(session, uri, *, trace=None, timeout_seconds=3):
    """Fetch within one bounded budget; a new launch may precede its metadata.

    Retry only transient transport/HTTP failures. Never follow redirects, relax
    public-address checks, or accept partial/oversized/non-object JSON.
    """
    if not isinstance(uri, str) or not uri or len(uri) > 4096:
        raise DiagnosticError("METADATA_URI_UNSUPPORTED")
    cid = root_cid(uri)
    if cid is not None:
        return await ipfs_metadata(session, uri, cid, trace=trace, timeout_seconds=timeout_seconds)
    uri_sha256 = hashlib.sha256(uri.encode()).hexdigest()
    if uri.startswith("ipfs://"):
        uri = "https://ipfs.io/ipfs/" + uri[7:].removeprefix("ipfs/")
    parsed = urlsplit(uri)
    if parsed.scheme != "https" or parsed.username or parsed.password or not parsed.hostname:
        raise DiagnosticError("METADATA_URI_UNSUPPORTED")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise DiagnosticError("METADATA_ADDRESS_BLOCKED")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0, min(3, timeout_seconds))
    report = trace or (lambda **details: None)
    # Host is bounded and excludes path, query, userinfo and fragments.
    host = parsed.hostname.encode("idna").decode("ascii")[:253]
    common = {"metadata_host": host, "metadata_uri_sha256": uri_sha256}
    delays = (0.15, 0.35, 0.7)
    for attempt in range(1, 5):
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise DiagnosticError("METADATA_TIMEOUT")
        started = loop.time()
        transport = {}
        report(reason="METADATA_REQUEST_STARTED", metadata_attempt=attempt, **common)
        try:
            async with asyncio.timeout(remaining):
                async with session.get(
                    uri, allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=remaining),
                    trace_request_ctx=transport,
                ) as response:
                    if response.status != 200:
                        error = DiagnosticError("METADATA_HTTP_REJECTED", http_status=response.status)
                        error.retry_after_seconds = retry_after_seconds(getattr(response, 'headers', {}).get('Retry-After'))
                        raise error
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(16384):
                        body.extend(chunk)
                        if len(body) > 262_144:
                            raise DiagnosticError("METADATA_TOO_LARGE")
                    try:
                        document = json.loads(body)
                    except (ValueError, UnicodeError):
                        raise DiagnosticError("METADATA_JSON_INVALID") from None
                    if not isinstance(document, dict):
                        raise DiagnosticError("METADATA_NOT_OBJECT")
            timings = {"metadata_reused_connection": "reused" in transport}
            for name, first, last in (("dns", "dns_start", "dns_end"),
                                      ("connect", "connect_start", "connect_end"),
                                      ("ttfb", "sent", "headers")):
                if first in transport and last in transport:
                    timings["metadata_" + name + "_ms"] = max(0, (transport[last]-transport[first])*1000)
            if "headers" in transport:
                timings["metadata_body_ms"] = max(0, (loop.time()-transport["headers"])*1000)
            report(reason="METADATA_REQUEST_SUCCEEDED", metadata_attempt=attempt, **timings,
                   metadata_attempt_ms=(loop.time()-started)*1000, http_status=200, **common)
            return document, time.time_ns()
        except (DiagnosticError, aiohttp.ClientError, TimeoutError) as error:
            if isinstance(error, DiagnosticError):
                retryable = error.reason == "METADATA_HTTP_REJECTED" and (
                    error.http_status in (404, 408, 425, 429) or error.http_status >= 500
                )
            else:
                retryable = True
                error = DiagnosticError("METADATA_TIMEOUT" if isinstance(error, TimeoutError)
                                        else "METADATA_NETWORK_FAILED")
            delay = max(delays[min(attempt-1, 2)], getattr(error, 'retry_after_seconds', 0))
            retrying = retryable and attempt < 4 and deadline-loop.time() > delay
            error.metadata_attempt = attempt
            report(reason=error.reason, metadata_attempt=attempt, retrying=retrying,
                   metadata_attempt_ms=(loop.time()-started)*1000,
                   http_status=error.http_status, retry_after_seconds=getattr(error, 'retry_after_seconds', 0), **common)
            if not retrying:
                raise error from None
            await asyncio.sleep(delay)


async def native_feed(session, urls, emit, health, stop, diagnose=None, *, discovery_budget=None):
    configured_urls = tuple(urls)
    if discovery_budget is not None:
        urls = public_first(urls)
    index = 0
    budget_notices = set()
    while not stop.is_set():
        selected_url = urls[index % len(urls)]
        endpoint = urlsplit(selected_url)
        websocket = urlunsplit(("wss", endpoint.netloc, endpoint.path, endpoint.query, ""))
        index += 1
        paid = discovery_budget is not None and provider_name(selected_url) != "SOLANA_PUBLIC"
        lease = 0
        planned_switch = False
        if paid:
            if not discovery_budget.reserve_stream(selected_url):
                notice_key = (discovery_budget.data["utc_day"], selected_url)
                if diagnose and notice_key not in budget_notices:
                    diagnose("DISCOVERY_BUDGET_EXHAUSTED", stage="feed", category="blocked",
                             reason="PAID_STREAM_FALLBACK_LIMIT",
                             provider_index=configured_urls.index(selected_url))
                    budget_notices.add(notice_key)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1)
                except TimeoutError:
                    pass
                continue
            lease = STREAM_LEASE_BYTES
        try:
            async with session.ws_connect(
                websocket, heartbeat=15, receive_timeout=30, max_msg_size=4_194_304
            ) as ws:
                await ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "logsSubscribe",
                        "params": [{"mentions": [PUMP_PROGRAM_ID]}, {"commitment": "confirmed"}],
                    }
                )
                reply = await ws.receive_json(timeout=10)
                if "error" in reply or type(reply.get("result")) is not int:
                    raise RuntimeError("FEED_SUBSCRIPTION_REJECTED")
                if discovery_budget is not None:
                    discovery_budget.stream_provider = provider_name(selected_url)
                    discovery_budget.stream_provider_index = configured_urls.index(selected_url)
                    if diagnose:
                        diagnose("FEED_SOURCE_CONNECTED", stage="feed", category="progress",
                                 reason="BOUNDED_PAID_FALLBACK" if paid else "PUBLIC_RPC_DISCOVERY",
                                 provider_index=configured_urls.index(selected_url))
                health(True)
                opened = time.monotonic()
                async for message in ws:
                    if stop.is_set():
                        break
                    if message.type != aiohttp.WSMsgType.TEXT:
                        continue
                    received_ns = time.time_ns()
                    size = len(message.data.encode())
                    if discovery_budget is not None:
                        discovery_budget.stream_bytes += size
                        discovery_budget.stream_messages += 1
                    if paid:
                        lease -= size
                    payload = json.loads(message.data)
                    value = payload.get("params", {}).get("result", {}).get("value") or {}
                    if value.get("err") is None and value.get("signature"):
                        observed = time.time_ns()
                        events = anchor_events_from_logs(value.get("logs", []), PUMP_PROGRAM_ID)
                        slot = payload.get('params', {}).get('result', {}).get('context', {}).get('slot')
                        if type(slot) is int and 0 < slot < 2**53:
                            for event in events:
                                event['_feed_slot'] = slot
                        decoded_ns = time.time_ns()
                        for event in events:
                            event['_feed_received_ns'] = received_ns
                            event['_feed_decoded_ns'] = decoded_ns
                        if discovery_budget is not None:
                            discovery_budget.creates_decoded += sum(e.get("anchor_event") == "CreateEvent" for e in events)
                        await emit(value["signature"], events, observed)
                    # A buffered websocket receive and on_batch can both complete
                    # without suspending. Yield between batches so a busy feed
                    # cannot starve newly admitted entries, exits or RPC replies.
                    await asyncio.sleep(0)
                    retry_public = paid and time.monotonic() - opened >= 30
                    quota_reached = paid and not retry_public and lease < STREAM_LEASE_BYTES and not discovery_budget.reserve_stream(selected_url)
                    if retry_public or quota_reached:
                        # Retry the free source periodically, and close before the
                        # next maximum-size message could exceed reserved capacity.
                        index = 0
                        planned_switch = True
                        if diagnose:
                            diagnose("FEED_SOURCE_ROTATION", stage="feed", category="progress",
                                     reason="PAID_STREAM_FALLBACK_LIMIT" if quota_reached else "RETRY_PUBLIC_SOURCE",
                                     provider_index=configured_urls.index(selected_url))
                        break
                    if paid and lease < STREAM_LEASE_BYTES:
                        lease += STREAM_LEASE_BYTES
                if diagnose and not stop.is_set() and not planned_switch:
                    diagnose(
                        "FEED_STREAM_ENDED",
                        stage="feed",
                        category="fault",
                        reason="WEBSOCKET_STREAM_ENDED",
                        provider_index=configured_urls.index(selected_url),
                        websocket_close_code=ws.close_code,
                    )
        except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError) as error:
            if diagnose:
                diagnose(
                    "FEED_RECONNECT",
                    stage="feed",
                    category="fault",
                    provider_index=configured_urls.index(selected_url),
                    **failure("feed", error),
                )
        finally:
            health(False)
            if discovery_budget is not None:
                discovery_budget.stream_provider = "DISCONNECTED"
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except TimeoutError:
            pass

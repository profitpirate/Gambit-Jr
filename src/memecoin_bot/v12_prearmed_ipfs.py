"""Bounded, CID-verified IPFS metadata retrieval through public gateways.

Only immutable root-file CIDs are eligible. Arbitrary HTTPS/IPNS/path resolution
is never guessed, and unverified gateway JSON cannot enter the trading policy.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import re
import time
import weakref
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import aiohttp

from .v12_prearmed_diagnostics import DiagnosticError

MAX_DOCUMENT = 262_144
MAX_BLOCK = MAX_DOCUMENT + 1024
GATEWAYS = {"ipfs.io", "dweb.link", "gateway.pinata.cloud", "ipfs.filebase.io",
            "trustless-gateway.link"}
_COOLDOWNS = weakref.WeakKeyDictionary()


def cid_parts(cid):
    """Accept canonical CIDv0 or base32 CIDv1, raw/dag-pb, SHA-256 only."""
    if re.fullmatch(r"Qm[1-9A-HJ-NP-Za-km-z]{44}", cid):
        number = 0
        for char in cid:
            number = number * 58 + "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz".index(char)
        value = number.to_bytes((number.bit_length() + 7) // 8, "big")
        if len(value) == 34 and value[:2] == b"\x12\x20":
            return 0x70, value[2:]
    elif re.fullmatch(r"b[a-z2-7]{58}", cid):
        value = base64.b32decode(cid[1:].upper() + "=" * ((-len(cid[1:])) % 8))
        if (len(value) == 36 and value[0] == 1 and value[1] in (0x55, 0x70)
                and value[2:4] == b"\x12\x20"
                and "b" + base64.b32encode(value).decode().lower().rstrip("=") == cid):
            return value[1], value[4:]
    raise DiagnosticError("METADATA_IPFS_CID_UNSUPPORTED")


def root_cid(uri):
    parsed = urlsplit(uri)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    if parsed.scheme == "ipfs":
        path = (parsed.netloc + parsed.path).removeprefix("ipfs/")
    elif (parsed.scheme == "https" and parsed.hostname in GATEWAYS
          and parsed.port in (None, 443) and parsed.path.startswith("/ipfs/")):
        path = parsed.path.removeprefix("/ipfs/")
    else:
        return None
    if "/" in path or not path:
        return None
    try:
        cid_parts(path)
    except (ValueError, OverflowError):
        return None
    return path


def _varint(value):
    data = bytearray()
    while value >= 128:
        data.append((value & 127) | 128)
        value >>= 7
    data.append(value)
    return bytes(data)


def _read_varint(data, at):
    start, value = at, 0
    for shift in range(0, 64, 7):
        if at >= len(data):
            break
        byte = data[at]
        at += 1
        value |= (byte & 127) << shift
        if byte < 128:
            if data[start:at] != _varint(value) or value >= 1 << 64:
                break
            return value, at
    raise DiagnosticError("METADATA_IPFS_LAYOUT_UNSUPPORTED")


def _fields(data):
    at, fields = 0, []
    while at < len(data):
        tag, at = _read_varint(data, at)
        field, wire = tag >> 3, tag & 7
        if not field or wire not in (0, 2) or len(fields) >= 32:
            raise DiagnosticError("METADATA_IPFS_LAYOUT_UNSUPPORTED")
        value, at = _read_varint(data, at)
        if wire == 2:
            end = at + value
            if end > len(data):
                raise DiagnosticError("METADATA_IPFS_LAYOUT_UNSUPPORTED")
            value, at = data[at:end], end
        fields.append((field, wire, value))
    return fields


def _unixfs_file(body):
    node = _fields(body)
    if len(node) != 1 or node[0][:2] != (1, 2):
        raise DiagnosticError("METADATA_IPFS_LAYOUT_UNSUPPORTED")
    fields = _fields(node[0][2])
    if len({field for field, _, _ in fields}) != len(fields):
        raise DiagnosticError("METADATA_IPFS_LAYOUT_UNSUPPORTED")
    values = {field: (wire, value) for field, wire, value in fields}
    if (values.get(1) not in ((0, 0), (0, 2)) or values.get(2, (None,))[0] != 2
            or any(field not in (1, 2, 3, 7, 8) for field in values)):
        raise DiagnosticError("METADATA_IPFS_LAYOUT_UNSUPPORTED")
    content = values[2][1]
    if 3 in values and values[3] != (0, len(content)):
        raise DiagnosticError("METADATA_IPFS_LAYOUT_UNSUPPORTED")
    return content


def verified_document(cid, body):
    codec, expected = cid_parts(cid)
    if len(body) > MAX_BLOCK:
        raise DiagnosticError("METADATA_TOO_LARGE")
    if hashlib.sha256(body).digest() == expected:
        content = body if codec == 0x55 else _unixfs_file(body)
    elif codec == 0x70:
        # Some gateways return a decoded single-block UnixFS file. Reconstruct
        # its canonical root, and accept only if its exact hash matches the CID.
        inner = b"\x08\x02\x12" + _varint(len(body)) + body + b"\x18" + _varint(len(body))
        block = b"\x0a" + _varint(len(inner)) + inner
        if hashlib.sha256(block).digest() != expected:
            raise DiagnosticError("METADATA_IPFS_INTEGRITY_FAILED")
        content = body
    else:
        raise DiagnosticError("METADATA_IPFS_INTEGRITY_FAILED")
    if len(content) > MAX_DOCUMENT:
        raise DiagnosticError("METADATA_TOO_LARGE")
    try:
        document = json.loads(content)
    except (ValueError, UnicodeError, RecursionError):
        raise DiagnosticError("METADATA_JSON_INVALID") from None
    if not isinstance(document, dict):
        raise DiagnosticError("METADATA_NOT_OBJECT")
    return document


def retry_after_seconds(value, *, now=None):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - (time.time() if now is None else now)
        except (TypeError, ValueError, OverflowError):
            return 60.0
    return max(1.0, seconds) if math.isfinite(seconds) else 60.0


async def ipfs_metadata(session, uri, cid, *, trace=None, timeout_seconds=3):
    """At most three GETs in one deadline; cancel losing requests and respect 429s."""
    report = trace or (lambda **_: None)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0, min(3, timeout_seconds))
    cooldowns = _COOLDOWNS.setdefault(session, {})
    uri_hash = hashlib.sha256(uri.encode()).hexdigest()
    original = "https://ipfs.io/ipfs/" + cid if urlsplit(uri).scheme == "ipfs" else uri
    routes = [(original, False), ("https://ipfs.filebase.io/ipfs/" + cid, False),
              ("https://trustless-gateway.link/ipfs/" + cid + "?format=raw", True)]
    hosts, unique_routes = set(), []
    for url, raw in routes:
        host = urlsplit(url).hostname
        if host not in hosts:
            unique_routes.append((url, raw))
            hosts.add(host)
    routes = unique_routes
    failed = asyncio.Event()

    async def fetch(index, url, raw):
        if index:
            try:
                await asyncio.wait_for(failed.wait(), timeout=0.15 * index)
            except TimeoutError:
                pass
        host = urlsplit(url).hostname
        common = dict(metadata_host=host, metadata_uri_sha256=uri_hash,
                      metadata_attempt=index + 1, metadata_gateway_fallback=index > 0)
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise DiagnosticError("METADATA_TIMEOUT")
        cooldown = cooldowns.get(host, 0) - loop.time()
        if cooldown > 0:
            report(reason="METADATA_GATEWAY_COOLDOWN", retry_after_seconds=round(cooldown, 3), **common)
            failed.set()
            raise DiagnosticError("METADATA_GATEWAY_COOLDOWN", http_status=429)
        started = loop.time()
        transport = {}
        report(reason="METADATA_REQUEST_STARTED", **common)
        try:
            async with asyncio.timeout(remaining):
                async with session.get(url, allow_redirects=False,
                    headers={"Accept": "application/vnd.ipld.raw" if raw else "application/json"},
                    timeout=aiohttp.ClientTimeout(total=remaining), trace_request_ctx=transport) as response:
                    if response.status != 200:
                        if response.status == 429:
                            seconds = retry_after_seconds(getattr(response, "headers", {}).get("Retry-After"))
                            cooldowns[host] = loop.time() + seconds
                            report(reason="METADATA_GATEWAY_RATE_LIMITED", http_status=429,
                                   retry_after_seconds=seconds, **common)
                        raise DiagnosticError("METADATA_HTTP_REJECTED", http_status=response.status)
                    body_started = loop.time()
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(16384):
                        body.extend(chunk)
                        if len(body) > MAX_BLOCK:
                            raise DiagnosticError("METADATA_TOO_LARGE")
                    document = verified_document(cid, bytes(body))
            timings = {"metadata_reused_connection": "reused" in transport,
                       "metadata_body_ms": max(0, (loop.time() - body_started) * 1000)}
            for name, first, last in (("dns", "dns_start", "dns_end"),
                                      ("connect", "connect_start", "connect_end"),
                                      ("ttfb", "sent", "headers")):
                if first in transport and last in transport:
                    timings["metadata_" + name + "_ms"] = max(0, (transport[last] - transport[first]) * 1000)
            return document, time.time_ns(), dict(reason="METADATA_REQUEST_SUCCEEDED", metadata_cid_verified=True,
                   metadata_attempt_ms=(loop.time() - started) * 1000, http_status=200, **timings, **common)
        except asyncio.CancelledError:
            report(reason="METADATA_REQUEST_CANCELLED", metadata_attempt_ms=(loop.time() - started) * 1000, **common)
            raise
        except (DiagnosticError, aiohttp.ClientError, TimeoutError) as error:
            failed.set()
            if not isinstance(error, DiagnosticError):
                error = DiagnosticError("METADATA_TIMEOUT" if isinstance(error, TimeoutError)
                                        else "METADATA_NETWORK_FAILED")
            error.metadata_attempt = index + 1
            report(reason=error.reason, http_status=error.http_status, retrying=False,
                   metadata_attempt_ms=(loop.time() - started) * 1000, **common)
            raise error from None

    tasks = [asyncio.create_task(fetch(i, *route)) for i, route in enumerate(routes)]
    errors = []
    winner = None
    try:
        async with asyncio.timeout(max(0, deadline - loop.time())):
            for ready in asyncio.as_completed(tasks):
                try:
                    winner = await ready
                    break
                except DiagnosticError as error:
                    errors.append(error)
        if winner is None:
            raise errors[-1] if errors else DiagnosticError("METADATA_TIMEOUT")
    except TimeoutError:
        raise DiagnosticError("METADATA_TIMEOUT") from None
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    # Publish the accepted route last, after losing requests have closed, so
    # subsequent candidate/policy logs identify the document actually used.
    document, observed_ns, details = winner
    report(**details)
    return document, observed_ns

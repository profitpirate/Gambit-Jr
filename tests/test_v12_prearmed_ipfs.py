"""CID integrity, bounded failover, and policy tests for real IPFS failures."""
import asyncio
import base64
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from memecoin_bot.v12_prearmed_diagnostics import DiagnosticError, failure
from memecoin_bot.v12_prearmed_io import metadata
from memecoin_bot.v12_prearmed_ipfs import (
    MAX_DOCUMENT, _COOLDOWNS, _varint, cid_parts, retry_after_seconds,
    root_cid, verified_document,
)
from memecoin_bot.v12_prearmed_runtime import Runtime, State

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = json.loads((ROOT / "tests/fixtures/v12-prearmed-ipfs-metadata.json").read_text())
MODEL = json.loads((ROOT / "research/v12-pre-armed-100-trade-frozen-model.json").read_text())


def cid_for(body, codec=0x55):
    return "b" + base64.b32encode(bytes([1, codec, 0x12, 0x20]) + hashlib.sha256(body).digest()).decode().lower().rstrip("=")


class Response:
    def __init__(self, body=b"{}", *, status=200, delay=0, headers=None):
        self.body, self.status, self.delay = body, status, delay
        self.headers, self.closed, self.content = headers or {}, False, self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def iter_chunked(self, size):
        await asyncio.sleep(self.delay)
        for at in range(0, len(self.body), size):
            yield self.body[at:at + size]


class Session:
    def __init__(self, routes):
        self.routes, self.requests, self.responses = routes, [], []

    def get(self, uri, **kwargs):
        self.requests.append((uri, kwargs))
        response = self.routes[urlsplit(uri).hostname]()
        self.responses.append(response)
        return response


@pytest.mark.parametrize("fixture", FIXTURES)
def test_actual_failed_launch_documents_match_chain_cid(fixture):
    body = base64.b64decode(fixture["body_b64"])
    document = verified_document(fixture["cid"], body)
    assert isinstance(document, dict) and document["twitter"]
    assert fixture["source_signature"]
    with pytest.raises(DiagnosticError, match="INTEGRITY_FAILED"):
        verified_document(fixture["cid"], body + b" ")
    peer = next(row for row in FIXTURES if row["cid"] == fixture["cid"] and row["route"] != fixture["route"])
    assert document == verified_document(peer["cid"], base64.b64decode(peer["body_b64"]))


@pytest.mark.parametrize("prefix", ["ipfs://", "ipfs://ipfs/", "https://ipfs.io/ipfs/", "https://ipfs.filebase.io/ipfs/"])
def test_only_immutable_root_urls_are_remapped(prefix):
    cid = FIXTURES[0]["cid"]
    assert root_cid(prefix + cid) == cid
    for suffix in ("/metadata.json", "?key=SECRET", "#fragment", "/"):
        assert root_cid(prefix + cid + suffix) is None


@pytest.mark.parametrize("uri", ["https://evil.example/ipfs/", "https://user:SECRET@ipfs.io/ipfs/",
                                  "https://127.0.0.1/ipfs/", "http://ipfs.io/ipfs/", "https://ipfs.io:444/ipfs/",
                                  "ipns://", "https://ipfs.io/ipns/"])
def test_arbitrary_or_unsafe_origins_never_gain_trusted_gateway_routes(uri):
    assert root_cid(uri + FIXTURES[0]["cid"]) is None


@pytest.mark.parametrize("cid", ["Qm" + "0" * 44, "b" + "a" * 58, "b" + "A" * 58, "x" * 1000])
def test_invalid_cid_rejected(cid):
    with pytest.raises(DiagnosticError, match="CID_UNSUPPORTED"):
        cid_parts(cid)


@pytest.mark.parametrize("body,codec,reason", [
    (b"[]", 0x55, "NOT_OBJECT"), (b"{", 0x55, "JSON_INVALID"),
    (b"x" * (MAX_DOCUMENT + 1), 0x55, "TOO_LARGE"),
    (b"\x0a\x04\x08\x02\x12\xff", 0x70, "LAYOUT_UNSUPPORTED"),
    (b"\x0a\x06\x08\x02\x12\x02{}\x12\x00", 0x70, "LAYOUT_UNSUPPORTED"),
    (b"\x0a\x08\x08\x02\x12\x02{}\x18\x03", 0x70, "LAYOUT_UNSUPPORTED"),
    (b"\x0a\x08\x08\x02\x08\x02\x12\x02{}", 0x70, "LAYOUT_UNSUPPORTED"),
    (b"\x8a\x00\x00", 0x70, "LAYOUT_UNSUPPORTED"),
], ids=["array", "bad-json", "oversize", "truncated-protobuf", "linked", "wrong-size", "duplicate", "overlong-varint"])
def test_hashed_but_unsafe_documents_and_linked_layouts_fail_closed(body, codec, reason):
    with pytest.raises(DiagnosticError, match=reason):
        verified_document(cid_for(body, codec), body)


def test_unixfs_single_file_reconstruction_and_raw_root_are_equivalent():
    body = b'{"twitter":"https://x.com/test/status/1"}'
    inner = b"\x08\x02\x12" + _varint(len(body)) + body + b"\x18" + _varint(len(body))
    root = b"\x0a" + _varint(len(inner)) + inner
    cid = cid_for(root, 0x70)
    assert verified_document(cid, body) == verified_document(cid, root) == json.loads(body)
    with pytest.raises(DiagnosticError, match="INTEGRITY_FAILED"):
        verified_document(cid_for(body), root)


def test_retry_after_honours_real_fifteen_minute_header_and_http_date():
    assert retry_after_seconds("900") == 900
    assert retry_after_seconds("Mon, 28 Sep 2026 17:15:00 GMT", now=1790614800) == 900
    for value in (None, "bad", "NaN", "Infinity"):
        assert retry_after_seconds(value) == 60


def test_rate_limited_origin_is_not_retried_on_next_candidate():
    async def scenario():
        body = b'{"twitter":"fresh"}'
        cid = cid_for(body)
        routes = {"ipfs.io": lambda: Response(status=429, headers={"Retry-After": "900"}),
                  "ipfs.filebase.io": lambda: Response(body),
                  "trustless-gateway.link": lambda: Response(body, delay=.1)}
        session, events = Session(routes), []
        for _ in range(2):
            doc, _ = await metadata(session, "ipfs://" + cid, trace=lambda **r: events.append(r))
            assert doc == json.loads(body)
        hosts = [urlsplit(url).hostname for url, _ in session.requests]
        assert hosts.count("ipfs.io") == 1 and len(hosts) <= 5
        assert 899 < _COOLDOWNS[session]["ipfs.io"] - asyncio.get_running_loop().time() <= 900
        assert any(r["reason"] == "METADATA_GATEWAY_COOLDOWN" for r in events)
        assert any(r.get("metadata_cid_verified") and r["metadata_gateway_fallback"] for r in events)
        assert all(r.closed for r in session.responses)
        assert all(not opts["allow_redirects"] for _, opts in session.requests)
        assert Session(routes) not in _COOLDOWNS
    asyncio.run(scenario())


@pytest.mark.parametrize("host", ["ipfs.io", "ipfs.filebase.io", "trustless-gateway.link"])
def test_fast_original_wins_without_extra_gets(host):
    async def scenario():
        body = b"{}"
        session = Session({host: lambda: Response(body)})
        await metadata(session, "https://" + host + "/ipfs/" + cid_for(body))
        assert len(session.requests) == 1 and session.responses[0].closed
    asyncio.run(scenario())


@pytest.mark.parametrize("bad_original", ["slow", "tampered", "redirect"])
def test_verified_backup_wins_and_cancels_bad_original(bad_original):
    async def scenario():
        body = b"{}"
        original = Response(body, delay=10) if bad_original == "slow" else Response(b"tampered", status=302 if bad_original == "redirect" else 200)
        session = Session({"ipfs.io": lambda: original, "ipfs.filebase.io": lambda: Response(body),
                           "trustless-gateway.link": lambda: Response(body, delay=10)})
        start, events = time.monotonic(), []
        document, _ = await metadata(session, "ipfs://" + cid_for(body), trace=lambda **r: events.append(r))
        assert document == {} and time.monotonic() - start < .8
        assert len(session.requests) <= 3 and all(r.closed for r in session.responses)
        assert events[-1]["reason"] == "METADATA_REQUEST_SUCCEEDED"
        assert events[-1]["metadata_host"] == "ipfs.filebase.io"
        if bad_original == "slow":
            assert any(e["reason"] == "METADATA_REQUEST_CANCELLED" for e in events)
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["deadline", "cancel", "all_tampered"])
def test_single_deadline_cancellation_and_all_invalid_close_every_response(mode):
    async def scenario():
        session = Session({host: lambda: Response(b"bad", delay=0 if mode == "all_tampered" else 10)
                           for host in ("ipfs.io", "ipfs.filebase.io", "trustless-gateway.link")})
        start = time.monotonic()
        task = asyncio.create_task(metadata(session, "ipfs://" + cid_for(b"{}"), timeout_seconds=.4))
        if mode == "cancel":
            await asyncio.sleep(.2)
            task.cancel()
        with pytest.raises(asyncio.CancelledError if mode == "cancel" else DiagnosticError):
            await task
        assert time.monotonic() - start < .8 and len(session.requests) <= 3
        assert all(r.closed for r in session.responses)
    asyncio.run(scenario())


@pytest.mark.parametrize("age,accepted", [(1, True), (60, False)])
def test_recovered_metadata_still_must_pass_frozen_social_freshness(tmp_path, age, accepted):
    async def scenario():
        now = time.time_ns()
        creator = "95ZCf3jKMHeFYvPXVZW3Ek6AEPDyjebosqnc7eNioVMo"
        handle = MODEL["creator_handles"][creator][0].lstrip("@")
        snowflake = (((now - age * 1_000_000_000) // 1_000_000) - 1288834974657) << 22
        body = json.dumps({"twitter": f"https://x.com/{handle}/status/{snowflake}"}).encode()
        session = Session({"ipfs.io": lambda: Response(status=429, headers={"Retry-After":"900"}),
                           "ipfs.filebase.io": lambda: Response(body), "trustless-gateway.link": lambda: Response(body, delay=.2)})
        state = State(tmp_path / "state.sqlite", wallet="wallet", mode="live")
        events, orders = [], []
        async def quote(payload):
            return {"quoted_tokens_raw": "100", "curve_price": 1.0}
        runner = Runtime(model=MODEL, state=state, rpc=None, builder=SimpleNamespace(request=quote), execution=SimpleNamespace(),
                         wallet="wallet", metadata_session=session, emit=lambda *a: events.append(a), kill_path=tmp_path / "STOP")
        runner.connected = True
        runner.last_feed_ns = runner.balance_ns = now
        runner.balance = 1_000_000_000
        runner.order = lambda *a, **kw: orders.append((a, kw))
        event = {"anchor_event": "CreateEvent", "mint": "mint", "creator": creator, "is_mayhem_mode": False,
                 "timestamp": now // 1_000_000_000, "uri": "ipfs://" + cid_for(body),
                 "virtual_sol_reserves":100_000_000_000, "virtual_token_reserves":100_000_000}
        trade = {"anchor_event":"TradeEvent", "mint":"mint", "user":creator, "is_buy":True, "sol_amount":2_000_000_000}
        await runner.on_batch("creation", [event, trade], now)
        await asyncio.gather(*list(runner.tasks))
        assert bool(orders) is accepted and len(orders) <= 1
        if not accepted:
            assert any(e == "CANDIDATE_REJECTED" and p["reason"] == "SOCIAL_AGE_OUTSIDE_WINDOW" for e, p in events)
        assert any(p.get("metadata_cid_verified") for _, p in events)
        state.conn.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["METADATA_IPFS_CID_UNSUPPORTED", "METADATA_IPFS_LAYOUT_UNSUPPORTED",
                                   "METADATA_IPFS_INTEGRITY_FAILED", "METADATA_GATEWAY_COOLDOWN"])
def test_integrity_and_cooldown_reasons_remain_visible_in_candidate_failures(reason):
    assert failure("metadata", DiagnosticError(reason))["reason"] == reason


def test_nested_hashed_json_cannot_bypass_social_post_requirement():
    from memecoin_bot.v12_prearmed_runtime import social_status
    body = b'{"x":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}"
    try:
        document = verified_document(cid_for(body), body)
    except DiagnosticError as error:
        assert error.reason == "METADATA_JSON_INVALID"
    else:
        with pytest.raises(DiagnosticError, match="SOCIAL_STATUS_URL_REQUIRED"):
            social_status(document)


def test_integrity_notice_explains_the_skipped_token_in_plain_language():
    from memecoin_bot.v12_prearmed_notices import format_notice
    text, priority = format_notice("CANDIDATE_UNAVAILABLE", {"stage":"metadata", "reason":"METADATA_IPFS_INTEGRITY_FAILED"})
    assert "published content hash" in text and "No trade was placed" in text
    assert "IPFS_INTEGRITY" not in text and priority == 0

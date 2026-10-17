"""Regression cases for the 28 September launch metadata 404 incident."""
import asyncio
import hashlib
import importlib.util
import json
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest

from memecoin_bot.v12_prearmed_diagnostics import DiagnosticError
from memecoin_bot.v12_prearmed_io import PublicResolver, metadata
from memecoin_bot.v12_prearmed_notices import format_notice
from memecoin_bot.v12_prearmed_runtime import Runtime, State

ROOT = Path(__file__).resolve().parents[1]
MODEL = json.loads((ROOT / "research/v12-pre-armed-100-trade-frozen-model.json").read_text())


class Response:
    def __init__(self, status=200, chunks=(b'{}',), delay=0):
        self.status, self.chunks, self.delay = status, chunks, delay
        self.content = self
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk


class Session:
    def __init__(self, responses):
        self.responses, self.requests = iter(responses), []

    def get(self, uri, **kwargs):
        self.requests.append((uri, kwargs))
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value


@pytest.mark.parametrize("status", [404, 408, 425, 429, 500, 502, 503, 504])
def test_transient_http_failure_recovers_without_becoming_a_candidate_fault(status):
    session = Session([Response(status), Response(chunks=(b'{"twitter":', b'"fresh"}'))])
    events = []
    uri = "https://metadata.example/token?api-key=SECRET#PRIVATE"
    document, observed = asyncio.run(metadata(session, uri, trace=lambda **d: events.append(d)))
    assert document == {"twitter": "fresh"} and observed > 0
    assert len(session.requests) == 2
    assert all(request[1]["allow_redirects"] is False for request in session.requests)
    assert events[1]["http_status"] == status and events[1]["retrying"] is True
    assert events[-1]["reason"] == "METADATA_REQUEST_SUCCEEDED"
    assert events[-1]["metadata_attempt"] == 2
    assert events[-1]["metadata_uri_sha256"] == hashlib.sha256(uri.encode()).hexdigest()
    assert "SECRET" not in json.dumps(events) and "PRIVATE" not in json.dumps(events)


def test_persistent_404_has_a_bounded_retry_count_and_releases_every_response():
    responses = [Response(404) for _ in range(4)]
    session = Session(responses)
    start = time.monotonic()
    with pytest.raises(DiagnosticError) as caught:
        asyncio.run(metadata(session, "https://metadata.example/token"))
    assert caught.value.reason == "METADATA_HTTP_REJECTED"
    assert caught.value.http_status == 404 and caught.value.metadata_attempt == 4
    assert len(session.requests) == 4 and all(row.closed for row in responses)
    assert time.monotonic() - start < 3


@pytest.mark.parametrize("response,reason", [
    (Response(301), "METADATA_HTTP_REJECTED"),
    (Response(302), "METADATA_HTTP_REJECTED"),
    (Response(400), "METADATA_HTTP_REJECTED"),
    (Response(403), "METADATA_HTTP_REJECTED"),
    (Response(chunks=(b'{broken}',)), "METADATA_JSON_INVALID"),
    (Response(chunks=(b'[]',)), "METADATA_NOT_OBJECT"),
    (Response(chunks=(b'x' * 262144, b'x')), "METADATA_TOO_LARGE"),
])
def test_permanent_or_unsafe_response_is_not_retried(response, reason):
    session = Session([response])
    with pytest.raises(DiagnosticError, match=reason):
        asyncio.run(metadata(session, "https://metadata.example/token"))
    assert len(session.requests) == 1 and response.closed


def test_maximum_size_chunked_json_is_read_to_eof():
    raw = json.dumps({"text": "a" * (262144 - 12)}).encode()
    assert len(raw) == 262144
    session = Session([Response(chunks=[raw[i:i+1024] for i in range(0,len(raw),1024)])])
    document, _ = asyncio.run(metadata(session, "https://metadata.example/token"))
    assert len(document["text"]) == 262144-12


@pytest.mark.parametrize("error", [aiohttp.ClientConnectionError("SECRET"),
                                  aiohttp.ClientPayloadError("SECRET"), TimeoutError("SECRET")])
def test_transport_errors_are_retried_and_redacted(error):
    session = Session([error, Response()])
    events = []
    assert asyncio.run(metadata(session, "https://metadata.example/token", trace=lambda **d: events.append(d)))[0] == {}
    assert events[1]["retrying"] is True and "SECRET" not in json.dumps(events)


def test_slow_stream_obeys_total_deadline_and_closes_connection():
    response = Response(chunks=(b'{', b'}'), delay=.1)
    session = Session([response])
    started = time.monotonic()
    with pytest.raises(DiagnosticError, match="METADATA_TIMEOUT"):
        asyncio.run(metadata(session, "https://metadata.example/token", timeout_seconds=.03))
    assert time.monotonic()-started < .2 and response.closed
    assert len(session.requests) == 1


@pytest.mark.parametrize("uri", ["http://metadata.example/token", "https://user:SECRET@metadata.example/token",
                                "https://127.0.0.1/token", "https://[::1]/token", "https://169.254.169.254/token",
                                None, "", "x"*4097])
def test_invalid_and_private_uris_never_open_a_connection(uri):
    with pytest.raises(DiagnosticError):
        asyncio.run(metadata(None, uri))


def test_dns_rebinding_to_private_address_remains_blocked(monkeypatch):
    async def scenario():
        async def lookup(*args, **kwargs):
            return [(2, 1, 6, "", ("8.8.8.8",443)), (2,1,6,"",("127.0.0.1",443))]
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
        with pytest.raises(DiagnosticError, match="METADATA_ADDRESS_BLOCKED"):
            await PublicResolver().resolve("metadata.example",443)
    asyncio.run(scenario())


def test_cancellation_during_retry_stops_requests():
    async def scenario():
        retry_started = asyncio.Event()
        session = Session([Response(404), Response()])
        def trace(**details):
            if details.get("retrying"):
                retry_started.set()
        task = asyncio.create_task(metadata(session, "https://metadata.example/token", trace=trace))
        await retry_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(session.requests)==1
    asyncio.run(scenario())


def test_metadata_budget_is_capped_by_remaining_launch_window():
    session = Session([Response(404)])
    with pytest.raises(DiagnosticError, match="METADATA_HTTP_REJECTED"):
        asyncio.run(metadata(session, "https://metadata.example/token", timeout_seconds=.01))
    assert len(session.requests)==1 and session.requests[0][1]["timeout"].total <= .010001


@pytest.mark.parametrize("recover", [True, False])
def test_real_metadata_path_to_order_and_candidate_failure_isolation(tmp_path, recover):
    async def scenario():
        now = time.time_ns()
        creator = "95ZCf3jKMHeFYvPXVZW3Ek6AEPDyjebosqnc7eNioVMo"
        handle = MODEL["creator_handles"][creator][0].lstrip("@")
        snowflake = (((now-1_000_000_000)//1_000_000)-1288834974657)<<22
        body = json.dumps({"twitter": f"https://x.com/{handle}/status/{snowflake}"}).encode()
        session = Session([Response(404), Response(chunks=[body[:15],body[15:]])] if recover
                          else [aiohttp.ClientPayloadError("SECRET") for _ in range(4)])
        state = State(tmp_path/"state.sqlite", wallet="wallet", mode="live")
        events, orders = [], []
        async def quote(payload):
            return {"quoted_tokens_raw":"100", "curve_price":1.0}
        runner = Runtime(model=MODEL, state=state, rpc=None, builder=SimpleNamespace(request=quote),
                         execution=SimpleNamespace(), wallet="wallet", metadata_session=session,
                         emit=lambda *args:events.append(args), kill_path=tmp_path/"STOP")
        runner.connected = True
        runner.last_feed_ns = runner.balance_ns = now
        runner.balance = 1_000_000_000
        runner.order = lambda *args, **kwargs:orders.append((args,kwargs))
        event = {"anchor_event":"CreateEvent", "mint":"mint", "creator":creator, "is_mayhem_mode":False,
                 "timestamp":now//1_000_000_000, "uri":"https://metadata.example/token?key=SECRET",
                 "virtual_sol_reserves":100_000_000_000, "virtual_token_reserves":100_000_000}
        trade = {"anchor_event":"TradeEvent", "mint":"mint", "user":creator,"is_buy":True,"sol_amount":2_000_000_000}
        await runner.on_batch("creation-signature",[event,trade],now)
        await runner.on_batch("duplicate-signature",[event,trade],now)
        await asyncio.gather(*list(runner.tasks))
        if recover:
            assert len(orders)==1
            assert not any(e=="CANDIDATE_REJECTED" for e,p in events)
            assert runner.diagnostics.snapshot()["recent_fault_count"]==0
        else:
            rejection = next(p for e,p in events if e=="CANDIDATE_REJECTED")
            assert rejection["reason"]=="METADATA_NETWORK_FAILED"
            assert rejection["metadata_attempt"]==4 and rejection["metadata_host"]=="metadata.example"
            assert rejection["signature"]=="creation-signature" and not orders
            assert runner.entries_allowed() and not any(e=="RUNTIME_TASK_FAILED" for e,p in events)
        assert not runner.busy_mints and "SECRET" not in json.dumps(events)
        state.conn.close()
    asyncio.run(scenario())


def test_candidate_alert_is_specific_and_does_not_claim_whole_bot_failure():
    text, priority = format_notice("CANDIDATE_UNAVAILABLE", {"stage":"metadata", "reason":"METADATA_HTTP_REJECTED",
                                                          "http_status":404, "metadata_attempt":4})
    assert "Token skipped" in text and "Service response: 404" in text and "Checks: 4" in text
    assert "No trade was placed" in text and priority==0


def test_discord_and_monitor_exports_keep_attempt_trace_but_remove_raw_url(tmp_path):
    from memecoin_bot.v12_prearmed_operator_backend import OperatorBackend
    source = ROOT/"deployment/gambit-monitor-collector.py"
    shutil.copyfile(source, tmp_path/"gambit-monitor-collector.py")
    data = tmp_path/"prearmed-data"
    data.mkdir()
    row = {"ts_ns":time.time_ns(),"event":"CANDIDATE_PROGRESS", "payload":{
        "mint":"mint", "signature":"creation-signature", "metadata_host":"metadata.example",
        "metadata_uri_sha256":"a"*64, "metadata_attempt":2, "metadata_attempt_ms":103.5,
        "metadata_dns_ms":5.1,"metadata_connect_ms":10.2,"metadata_ttfb_ms":102.8,
        "metadata_body_ms":.7,"metadata_reused_connection":False,
        "metadata_gateway_fallback":True,"metadata_cid_verified":True,"retry_after_seconds":900,
        "builder_state_read_ms":12.3,"builder_total_ms":14.5,"builder_simulation_ms":2.2,
        "http_status":404,"retrying":True,"reason":"METADATA_HTTP_REJECTED",
        "uri":"https://metadata.example/token?key=SECRET"}}
    path = data/"live-audit.jsonl"
    path.write_text(json.dumps(row)+"\n")
    spec = importlib.util.spec_from_file_location("metadata_collector",source)
    collector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(collector)
    exports = [OperatorBackend(tmp_path).logs(),collector.audit_tail(path,time.time_ns())]
    for value in exports:
        payload = value["events"][0]["payload"]
        assert payload["metadata_attempt"]==2 and payload["http_status"]==404
        assert payload["signature"]=="creation-signature" and payload["retrying"] is True
        assert payload["metadata_host"]=="metadata.example" and payload["metadata_uri_sha256"]=="a"*64
        for key in ("metadata_dns_ms","metadata_connect_ms","metadata_ttfb_ms","metadata_body_ms",
                    "metadata_reused_connection","metadata_gateway_fallback","metadata_cid_verified","retry_after_seconds",
                    "builder_state_read_ms","builder_total_ms","builder_simulation_ms"):
            assert payload[key] == row["payload"][key]
        assert "SECRET" not in json.dumps(value)

"""Concurrency, freshness and fairness tests without financial operations."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest

from memecoin_bot import v12_prearmed_io as io
from memecoin_bot import v12_prearmed_runtime as module

MODEL = json.loads((Path(__file__).resolve().parents[1] / 'research/v12-pre-armed-100-trade-frozen-model.json').read_text())


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    now = [MODEL['maximum_evidence_ns'] + 100_000_000_000]
    creator = next(iter(MODEL['creator_handles']))
    handle = MODEL['creator_handles'][creator][0].lstrip('@')
    snowflake = (((now[0] - 1_000_000_000) // 1_000_000) - 1288834974657) << 22
    document = {'twitter': f'https://x.com/{handle}/status/{snowflake}'}
    state = module.State(tmp_path/'state.sqlite', wallet='wallet', mode='shadow')
    events, calls = [], []
    async def quote(request):
        calls.append(request)
        return dict(quoted_tokens_raw='100', curve_price=.00000003)
    runner = module.Runtime(model=MODEL, state=state, rpc=None, builder=SimpleNamespace(request=quote),
        execution=None, wallet='wallet', metadata_session=None, emit=lambda *a: events.append(a),
        kill_path=tmp_path/'kill', clock=lambda: now[0])
    runner.connected = True
    runner.last_feed_ns = runner.balance_ns = now[0]
    runner.balance = 1_000_000_000
    event = dict(mint='mint', creator=creator, timestamp=now[0]//1_000_000_000, uri='https://example.test',
        virtual_sol_reserves=30_000_000_000, virtual_token_reserves=1_000_000_000_000_000)
    async def metadata(*a, **kw): return document, now[0]
    monkeypatch.setattr(module, 'metadata', metadata)
    yield SimpleNamespace(runner=runner, now=now, document=document, event=event, events=events, calls=calls)
    assert not state.data['pending'] and not state.data['positions']
    state.conn.close()


def test_metadata_and_quote_overlap_and_only_quote_is_speculative(candidate, monkeypatch):
    c = candidate
    async def scenario():
        quote_started, metadata_started = asyncio.Event(), asyncio.Event()
        async def quote(request):
            c.calls.append(request); quote_started.set()
            await metadata_started.wait()
            return dict(quoted_tokens_raw='100', curve_price=.00000003)
        async def metadata(*a, **kw):
            metadata_started.set(); await quote_started.wait()
            return c.document, c.now[0]
        c.runner.builder.request = quote
        monkeypatch.setattr(module, 'metadata', metadata)
        await asyncio.wait_for(c.runner.enter(c.event, 2_000_000_000, c.now[0]), 1)
    asyncio.run(scenario())
    assert [r['action'] for r in c.calls] == ['QUOTE']
    signal = next(p for e, p in c.events if e == 'FROZEN_SIGNAL')
    assert signal['quote_prefetch_reused'] is True


@pytest.mark.parametrize('delay_ns,reused', [(20_000_000, True), (20_000_001, False), (-1, False)])
def test_aged_or_future_quote_is_read_again(candidate, monkeypatch, delay_ns, reused):
    c = candidate
    async def metadata(*a, **kw):
        # The quote task is created first and completes before this getter.
        assert c.calls
        c.now[0] += delay_ns
        return c.document, c.now[0]
    monkeypatch.setattr(module, 'metadata', metadata)
    asyncio.run(c.runner.enter(c.event, 2_000_000_000, c.now[0]))
    if delay_ns < 0:
        # Existing freshness gates reject a clock moving behind the feed/balance.
        assert len(c.calls) == 1 and c.runner.candidates == 0
    else:
        assert len(c.calls) == (1 if reused else 2)


def test_changed_budget_never_reuses_earlier_amount(candidate, monkeypatch):
    c = candidate
    async def metadata(*a, **kw):
        assert c.calls
        c.runner.balance -= 100_000_000
        return c.document, c.now[0]
    monkeypatch.setattr(module, 'metadata', metadata)
    asyncio.run(c.runner.enter(c.event, 2_000_000_000, c.now[0]))
    assert [r['budget_lamports'] for r in c.calls] == ['97000000', '87000000']


@pytest.mark.parametrize('reason', ['metadata_error', 'social_invalid', 'entry_blocked', 'expired'])
def test_failed_other_gate_never_uses_speculative_quote_to_enter(candidate, monkeypatch, reason):
    c = candidate
    async def metadata(*a, **kw):
        if reason == 'metadata_error': raise RuntimeError('private-url-must-not-be-logged')
        if reason == 'entry_blocked': c.runner.stopping = True
        if reason == 'expired': c.now[0] += 16_000_000_000
        return ({} if reason == 'social_invalid' else c.document), c.now[0]
    monkeypatch.setattr(module, 'metadata', metadata)
    asyncio.run(c.runner.enter(c.event, 2_000_000_000, c.now[0]))
    assert len(c.calls) == 1 and c.runner.candidates == 0
    assert all(e != 'FROZEN_SIGNAL' for e, _ in c.events)
    assert 'private-url' not in json.dumps(c.events)


def test_speculative_failure_does_not_override_invalid_social(candidate):
    c = candidate; c.document.clear()
    async def quote(request): raise RuntimeError('BUILDER:QUOTE_STATE_UNAVAILABLE')
    c.runner.builder.request = quote
    asyncio.run(c.runner.enter(c.event, 2_000_000_000, c.now[0]))
    reasons = [p['reason'] for e, p in c.events if e == 'CANDIDATE_REJECTED']
    assert reasons == ['SOCIAL_STATUS_URL_REQUIRED']


def test_shutdown_cancels_inflight_speculative_read(candidate, monkeypatch):
    c = candidate
    async def scenario():
        started, ended = asyncio.Event(), asyncio.Event()
        async def quote(request):
            started.set()
            try: await asyncio.sleep(10)
            finally: ended.set()
        async def metadata(*a, **kw): await asyncio.sleep(10)
        c.runner.builder.request = quote; monkeypatch.setattr(module, 'metadata', metadata)
        task = asyncio.create_task(c.runner.enter(c.event, 2_000_000_000, c.now[0]))
        await started.wait(); task.cancel()
        with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(task, .5)
        assert ended.is_set()
    asyncio.run(scenario())


def test_buffered_feed_yields_before_draining_rest_of_burst(monkeypatch):
    async def scenario():
        stop = asyncio.Event(); processed = []; scheduled_at = []; ready = asyncio.Event()
        class Socket:
            close_code = 1000
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def send_json(self, row): pass
            async def receive_json(self, **kw): return {'result': 1}
            def __aiter__(self): return self
            async def __anext__(self):
                if len(processed) == 100: stop.set(); raise StopAsyncIteration
                value = {'err': None, 'signature': str(len(processed)), 'logs': []}
                return SimpleNamespace(type=aiohttp.WSMsgType.TEXT,
                    data=json.dumps({'params': {'result': {'value': value}}}))
        class Session:
            def ws_connect(self, *a, **kw): return Socket()
        async def decision(): scheduled_at.append(len(processed)); ready.set()
        async def emit(*a):
            processed.append(1)
            if len(processed) == 1: asyncio.create_task(decision())
        monkeypatch.setattr(io, 'anchor_events_from_logs', lambda *a: [])
        await io.native_feed(Session(), ['https://api.mainnet.solana.com'], emit, lambda *a: None, stop)
        await ready.wait()
        assert len(processed) == 100 and scheduled_at == [1]
    asyncio.run(scenario())

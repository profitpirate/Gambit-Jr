import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from memecoin_bot import v12_prearmed as launcher
from memecoin_bot.v12_prearmed_preflight import live_database_check
from memecoin_bot import v12_prearmed_preflight as preflight
import aiohttp


@pytest.mark.parametrize('failed', ['discord', 'pushover'])
def test_alert_failure_does_not_suppress_other_channel(failed):
    calls = []
    notices = launcher.Notices(None)
    async def discord(message):
        calls.append('discord')
        if failed == 'discord':
            raise RuntimeError('discord failed')
    async def pushover(message):
        calls.append('pushover')
        if failed == 'pushover':
            raise RuntimeError('pushover failed')
    notices.send_discord, notices.send_pushover = discord, pushover
    with pytest.raises(RuntimeError, match='NOTIFICATION_DELIVERY_FAILED'):
        asyncio.run(notices.send('test'))
    assert sorted(calls) == ['discord', 'pushover']


def test_all_alert_channels_must_succeed():
    notices = launcher.Notices(None)
    async def succeed(message):
        return None
    notices.send_discord = notices.send_pushover = succeed
    asyncio.run(notices.send('test'))


@pytest.mark.parametrize('live', [False, True])
def test_restart_network_failure_preserves_recovery_but_not_entry_clearance(monkeypatch, live):
    async def failure(*a, **kw):
        raise RuntimeError('RPC_UNAVAILABLE')
    monkeypatch.setattr(launcher, 'network_probe', failure)
    if live:
        assert asyncio.run(launcher.startup_network(None, None, None, None, live=True)) == ({}, False)
    else:
        with pytest.raises(RuntimeError):
            asyncio.run(launcher.startup_network(None, None, None, None, live=False))


@pytest.mark.parametrize('live', [False, True])
def test_missing_startup_balance_can_only_enter_recovery_mode(live):
    reasons = []
    async def failure():
        raise RuntimeError('RPC_UNAVAILABLE')
    runtime = SimpleNamespace(refresh_balance=failure, balance_ns=1,
        breaker=SimpleNamespace(exit_only=reasons.append))
    if live:
        asyncio.run(launcher.startup_balance(runtime, live=True))
        assert runtime.balance_ns == 0 and reasons == ['startup_balance_unavailable']
    else:
        with pytest.raises(RuntimeError):
            asyncio.run(launcher.startup_balance(runtime, live=False))


@pytest.mark.parametrize('case', ['flat', 'position', 'pending', 'signed', 'wallet_mismatch'])
def test_preflight_rejects_unresolved_or_mismatched_wallet_state(tmp_path, case):
    path = tmp_path / 'live.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE frozen_state (id INTEGER PRIMARY KEY, data TEXT)')
        state = dict(wallet='wallet' if case != 'wallet_mismatch' else 'other', positions={}, pending={})
        if case == 'position':
            state['positions']['mint'] = {}
        if case == 'pending':
            state['pending']['order'] = {}
        db.execute('INSERT INTO frozen_state VALUES (1, ?)', (json.dumps(state),))
        db.execute('CREATE TABLE v12_execution_journal (state TEXT)')
        if case == 'signed':
            db.execute("INSERT INTO v12_execution_journal VALUES ('SIGNED')")
    assert live_database_check(path, 'wallet') == (case == 'flat')


def test_no_existing_live_database_does_not_create_one(tmp_path):
    path = tmp_path / 'live.sqlite'
    assert live_database_check(path, 'wallet')
    assert not path.exists()


def test_second_provider_stream_must_deliver_decodable_events(monkeypatch):
    clock = [0.0]
    def monotonic():
        clock[0] += .01
        return clock[0]
    monkeypatch.setattr(preflight.time, 'monotonic', monotonic)
    monkeypatch.setattr(preflight, 'anchor_events_from_logs', lambda *a: [{'anchor_event': 'TradeEvent'}])
    class Socket:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def send_json(self, message):
            assert message['method'] == 'logsSubscribe'
        async def receive_json(self, **kwargs):
            return {'result': 1}
        async def receive(self, **kwargs):
            return SimpleNamespace(type=aiohttp.WSMsgType.TEXT,
                data=json.dumps({'params': {'result': {'value': {'err': None, 'logs': []}}}}))
    session = SimpleNamespace(ws_connect=lambda *a, **kw: Socket())
    report = asyncio.run(preflight.observe_endpoint(session, 'https://mainnet.helius-rpc.com/?api-key=SECRET', seconds=.1))
    assert report['passed'] and report['decoded_events'] > 0
    assert report['bytes_received'] > 0
    assert 'SECRET' not in json.dumps(report)


def test_provider_exception_never_exposes_credentials():
    class Socket:
        async def __aenter__(self):
            raise RuntimeError('https://provider.test/?api-key=SECRET')
        async def __aexit__(self, *a):
            pass
    session = SimpleNamespace(ws_connect=lambda *a, **kw: Socket())
    report = asyncio.run(preflight.observe_endpoint(session, 'https://provider.test/?api-key=SECRET', seconds=.1))
    assert not report['passed'] and 'SECRET' not in json.dumps(report)


def test_alerts_require_explicit_operator_option():
    with pytest.raises(RuntimeError, match='EXPLICIT_TEST_ALERT_OPTION_REQUIRED'):
        asyncio.run(preflight.network_checks(None, None, send_test_alert=False))
import asyncio
from types import SimpleNamespace
import pytest

from memecoin_bot import v12_prearmed_preflight as preflight

@pytest.mark.parametrize('states,expected', [([True,False,True],True),([True,False,False],False),([True,True],True)])
def test_readiness_requires_two_healthy_feeds_and_reports_extra_failures(monkeypatch,states,expected):
    urls=tuple('https://provider'+str(i) for i in range(len(states)))
    monkeypatch.setattr(preflight,'settings',lambda *a:SimpleNamespace(builder_command=(),rpc_url=urls[0],fallback_rpcs=urls[1:]))
    class Builder:
        def __init__(self,*a):pass
        async def close(self):pass
    monkeypatch.setattr(preflight,'Builder',Builder)
    async def probe(*a):return {'mainnet_rpc_endpoints':2}
    async def observe(session,url):return {'passed':states[urls.index(url)],'reason':'provider test'}
    async def send(*a):pass
    monkeypatch.setattr(preflight,'network_probe',probe)
    monkeypatch.setattr(preflight,'observe_endpoint',observe)
    monkeypatch.setattr(preflight.Notices,'send_discord',send)
    monkeypatch.setattr(preflight.Notices,'send_pushover',send)
    result=asyncio.run(preflight.network_checks(None,None,send_test_alert=True))
    assert result['passed'] is expected
    assert result['degraded_provider_indices']==[i for i,state in enumerate(states) if not state]
    assert result['healthy_feed_count']==sum(states)

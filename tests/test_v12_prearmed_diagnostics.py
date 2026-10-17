import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from memecoin_bot.v12_prearmed_diagnostics import DiagnosticError, Diagnostics, failure
from memecoin_bot.v12_prearmed_runtime import Runtime, State

ROOT = Path(__file__).resolve().parents[1]
MODEL = json.loads((ROOT / 'research/v12-pre-armed-100-trade-frozen-model.json').read_text())


def test_error_messages_never_leak_credentials():
    secret = 'https://rpc.test/?api-key=SECRET token=SECRET'
    assert 'SECRET' not in json.dumps(failure('quote', RuntimeError(secret)))
    assert failure('quote', RuntimeError('BUILDER:QUOTE_STATE_UNAVAILABLE'))['reason'] == 'QUOTE_STATE_UNAVAILABLE'
    assert failure('metadata', DiagnosticError('METADATA_HTTP_REJECTED', http_status=429))['http_status'] == 429


def test_diagnostics_degrade_and_expire_without_claiming_execution_proof():
    now, events = [10**18], []
    diagnostics = Diagnostics(lambda *args: events.append(args), lambda: now[0], 'run1')
    diagnostics.record('CANDIDATE_REJECTED', stage='metadata', reason='METADATA_HTTP_REJECTED', category='fault', mint='mint')
    assert diagnostics.snapshot()['pipeline_health'] == 'DEGRADED'
    assert events[0][1]['candidate_id'] == 'run1:mint'
    now[0] += 901_000_000_000
    assert diagnostics.snapshot()['pipeline_health'] == 'NO_RECENT_REPORTED_FAULTS'
    assert diagnostics.snapshot()['execution_verification'] == 'NOT_ATTESTED'


@pytest.mark.parametrize('scenario,expected_stage,expected_reason,category', [
    ('metadata', 'metadata', 'METADATA_HTTP_REJECTED', 'fault'),
    ('social', 'social_validation', 'SOCIAL_STATUS_URL_REQUIRED', 'policy'),
    ('quote', 'quote', 'QUOTE_STATE_UNAVAILABLE', 'fault'),
    ('creator_changed', 'quote', 'QUOTE_CREATOR_CHANGED', 'policy'),
    ('policy', 'social_validation', 'SOCIAL_AGE_OUTSIDE_WINDOW', 'policy'),
])
def test_each_candidate_failure_has_its_actual_stage(tmp_path, monkeypatch, scenario, expected_stage, expected_reason, category):
    import memecoin_bot.v12_prearmed_runtime as module
    now = MODEL['maximum_evidence_ns'] + 100_000_000_000
    creator = next(iter(MODEL['creator_handles']))
    handle = MODEL['creator_handles'][creator][0].lstrip('@')
    status_time = now - (20_000_000_000 if scenario == 'policy' else 1_000_000_000)
    snowflake = ((status_time // 1_000_000) - 1288834974657) << 22
    async def metadata(*_, **kwargs):
        if scenario == 'metadata':
            raise DiagnosticError('METADATA_HTTP_REJECTED', http_status=429)
        return ({} if scenario == 'social' else {'twitter': f'https://x.com/{handle}/status/{snowflake}'}), now
    monkeypatch.setattr(module, 'metadata', metadata)
    class Builder:
        async def request(self, payload):
            if scenario == 'quote':
                raise RuntimeError('BUILDER:QUOTE_STATE_UNAVAILABLE')
            if scenario == 'creator_changed':
                raise RuntimeError('BUILDER:QUOTE_CREATOR_CHANGED')
            return dict(quoted_tokens_raw='100', curve_price=0.00000003)
    state = State(tmp_path / 'state.sqlite', wallet='wallet', mode='shadow')
    events = []
    runtime = Runtime(model=MODEL, state=state, rpc=None, builder=Builder(), execution=None,
        wallet='wallet', metadata_session=None, emit=lambda *args: events.append(args),
        kill_path=tmp_path / 'kill', clock=lambda: now)
    runtime.connected = True
    runtime.last_feed_ns = runtime.balance_ns = now
    runtime.balance = 1_000_000_000
    event = dict(mint='mint', creator=creator, timestamp=now // 1_000_000_000, uri='https://example.test',
                 virtual_sol_reserves=30_000_000_000, virtual_token_reserves=1_000_000_000_000_000)
    asyncio.run(runtime.enter(event, 2_000_000_000, now))
    rejection = [p for e, p in events if e == 'CANDIDATE_REJECTED'][-1]
    assert (rejection['stage'], rejection['reason'], rejection['category']) == (expected_stage, expected_reason, category)
    assert rejection['observed_ns'] == now and rejection['elapsed_ms'] >= 0
    assert not state.data['pending']
    assert not state.data['positions']
    state.conn.close()


def test_blockers_describe_state_without_changing_gates(tmp_path):
    state = State(tmp_path / 'state.sqlite', wallet='wallet', mode='shadow')
    runtime = Runtime(model=MODEL, state=state, rpc=None, builder=None, execution=None,
        wallet='wallet', metadata_session=None, emit=lambda *args: None,
        kill_path=tmp_path / 'kill', clock=lambda: 10**18)
    assert {'FEED_DISCONNECTED', 'FEED_STALE', 'BALANCE_STALE', 'BALANCE_BELOW_RESERVE'} <= set(runtime.status()['entry_blockers'])
    assert not runtime.entries_allowed()
    state.conn.close()

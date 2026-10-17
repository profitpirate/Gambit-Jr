from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from memecoin_bot.v12_prearmed_runtime import State, Runtime, social_status
from memecoin_bot.v12_prearmed_policy import ExitState
from memecoin_bot.v12_prearmed_io import LiveExecution, receipt, metadata
from memecoin_bot.v12_execution_journal import ExecutionJournal

ROOT = Path(__file__).resolve().parents[1]
MODEL = json.loads((ROOT / 'research/v12-pre-armed-100-trade-frozen-model.json').read_text())


@pytest.fixture
def runtime(tmp_path):
    state = State(tmp_path / 'state.sqlite', wallet='wallet', mode='live')
    runner = Runtime(model=MODEL, state=state, rpc=None, builder=None, execution=SimpleNamespace(),
        wallet='wallet', metadata_session=None, emit=lambda *a: None, kill_path=tmp_path / 'kill',
        clock=lambda: 10_000_000_000)
    yield runner
    state.conn.close()


def position(runner):
    runner.state.data['positions']['mint'] = dict(original=101, remaining=101, pnl_lamports=-100,
        exit=asdict(ExitState(8_500_000_000, 1, 1)), latest_price=1, creator='creator',
        forced_exit=False, last_attempt_ns=0)


def test_wallet_mode_binding(tmp_path):
    path = tmp_path / 'state.sqlite'
    state = State(path, wallet='a', mode='shadow')
    state.conn.close()
    with pytest.raises(RuntimeError):
        State(path, wallet='a', mode='live')
    with pytest.raises(RuntimeError):
        State(path, wallet='b', mode='shadow')


def test_intent_survives_restart_and_dedupes_mint(tmp_path):
    path = tmp_path / 'state.sqlite'
    state = State(path, wallet='a', mode='live')
    assert state.reserve_mint('mint')
    state.data['pending']['id'] = {'payload': {'mint': 'mint'}}
    state.save()
    state.conn.close()
    state = State(path, wallet='a', mode='live')
    assert not state.reserve_mint('mint')
    assert 'id' in state.data['pending']
    state.conn.close()


def test_pending_blocks_entries(runtime):
    runtime.connected = True
    runtime.last_feed_ns = runtime.balance_ns = runtime.clock()
    runtime.balance = 1_000_000_000
    assert runtime.entries_allowed()
    runtime.state.data['pending']['id'] = {}
    assert not runtime.entries_allowed()
    runtime.state.data['pending'].clear()
    runtime.last_feed_ns = 0
    assert not runtime.entries_allowed()


def test_balance_timing_separates_slow_rpc_from_delayed_polling(runtime):
    now=[10_000_000_000];runtime.clock=lambda:now[0]
    runtime.balance_ns=4_000_000_000
    seen=[];runtime.diagnostics.emit=lambda event,payload:seen.append((event,payload))
    class Rpc:
        async def balance_lamports(self,wallet):
            assert runtime.status()['balance_refresh_inflight'] is True
            now[0]+=20_000_000
            return 1_000_000_000
    runtime.rpc=Rpc()
    asyncio.run(runtime.refresh_balance())
    status=runtime.status()
    assert status['balance_age_ms']==0 and status['balance_refresh_duration_ms']==20
    assert status['balance_refresh_inflight'] is False
    timing=next(p for event,p in seen if event=='BALANCE_REFRESH_DELAYED')
    assert timing['elapsed_ms']==20 and timing['balance_refresh_gap_ms']==6020


def test_failed_balance_refresh_never_updates_freshness(runtime):
    previous=runtime.balance_ns=runtime.clock()
    class Rpc:
        async def balance_lamports(self,wallet):raise RuntimeError('RPC_UNAVAILABLE')
    runtime.rpc=Rpc()
    with pytest.raises(RuntimeError):asyncio.run(runtime.refresh_balance())
    assert runtime.balance_ns==previous
    assert runtime.status()['balance_refresh_inflight'] is False


def test_prefill_balance_reply_cannot_restore_postfill_freshness(runtime):
    async def exercise():
        now=[10_000_000_000]
        runtime.clock=lambda:now[0]
        runtime.balance=1_000_000_000
        runtime.balance_ns=now[0]
        started=asyncio.Event();release=asyncio.Event()
        class Rpc:
            calls=0
            async def balance_lamports(self,wallet):
                self.calls+=1
                if self.calls==1:
                    started.set()
                    await release.wait()
                    return 900_000_000  # Snapshot began before the fill.
                return 800_000_000
        runtime.rpc=Rpc()
        stale_read=asyncio.create_task(runtime.refresh_balance())
        await started.wait()
        runtime.invalidate_balance()
        now[0]+=20_000_000
        release.set()
        await stale_read
        assert runtime.balance==1_000_000_000
        assert runtime.balance_ns==0
        assert await runtime.wait_for_balance_refresh(timeout=0.1)
        await runtime.refresh_balance()
        assert runtime.balance==800_000_000
        assert runtime.balance_ns==now[0]
    asyncio.run(exercise())


def test_balance_refresh_wait_wakes_immediately_after_fill(runtime):
    async def exercise():
        runtime.invalidate_balance()
        assert await runtime.wait_for_balance_refresh(timeout=0.1)
        assert not runtime.balance_refresh_requested.is_set()
    asyncio.run(exercise())


def test_partial_acknowledges_actual_original_fraction(runtime):
    position(runtime)
    emitted = []
    runtime.order = lambda payload, context, reason: emitted.append((payload, reason))
    runtime.consider_exit('mint', 9_000_000_000, price=1.2)
    payload, reason = emitted[0]
    assert payload['tokens_raw'] == '30' and reason == 'FIRST_TAKE'
    assert runtime.state.data['positions']['mint']['exit']['first_partial_done'] is False
    runtime.state.data['pending']['id'] = dict(payload=payload, context={}, reason=reason)
    runtime.apply_result('id', dict(status='UNKNOWN'))
    assert runtime.state.data['positions']['mint']['remaining'] == 101
    runtime.apply_result('id', dict(status='CONFIRMED', tokens_raw=30, sol_delta_lamports=50))
    assert runtime.state.data['positions']['mint']['remaining'] == 71
    assert runtime.balance_ns == 0 and runtime.balance_refresh_requested.is_set()
    assert runtime.state.data['positions']['mint']['exit']['first_partial_done'] is True
    runtime.apply_result('id', dict(status='CONFIRMED', tokens_raw=30, sol_delta_lamports=50))
    assert runtime.state.data['positions']['mint']['remaining'] == 71
    runtime.consider_exit('mint', 11_000_000_000, timer_only=True)
    assert emitted[-1][0]['tokens_raw'] == '71'


def test_confirmed_partial_does_not_delay_protective_exit(runtime):
    position(runtime)
    emitted = []
    runtime.order = lambda payload, context, reason: emitted.append((payload, reason))
    runtime.consider_exit('mint', 9_000_000_000, price=1.2)
    payload, reason = emitted[0]
    runtime.state.data['pending']['id'] = dict(payload=payload, context={}, reason=reason)
    runtime.apply_result('id', dict(status='UNKNOWN'))
    runtime.consider_exit('mint', 9_100_000_000, price=1.0)
    assert len(emitted) == 1  # Never sell an unresolved partial twice.
    runtime.apply_result('id', dict(status='CONFIRMED', tokens_raw=30, sol_delta_lamports=50))
    runtime.consider_exit('mint', 9_200_000_000, price=1.0)
    assert len(emitted) == 2
    assert emitted[-1][0]['tokens_raw'] == '71'
    assert emitted[-1][1] == 'TRAILING_OR_STOP'


def test_failed_partial_keeps_retry_backoff(runtime):
    position(runtime)
    emitted = []
    runtime.order = lambda payload, context, reason: emitted.append((payload, reason))
    runtime.consider_exit('mint', 9_000_000_000, price=1.2)
    payload, reason = emitted[0]
    runtime.state.data['pending']['id'] = dict(payload=payload, context={}, reason=reason)
    runtime.apply_result('id', dict(status='FAILED', reason='SIMULATION_FAILED'))
    runtime.consider_exit('mint', 9_200_000_000, price=1.2)
    assert len(emitted) == 1
    runtime.consider_exit('mint', 10_000_000_000, price=1.2)
    assert len(emitted) == 2
    assert emitted[-1][0]['tokens_raw'] == '30'


def test_failed_partial_can_retry_without_committing(runtime):
    position(runtime)
    payload = dict(side='SELL', mint='mint', tokens_raw='30')
    runtime.state.data['pending']['id'] = dict(payload=payload, context={}, reason='FIRST_TAKE')
    runtime.apply_result('id', dict(status='FAILED', reason='SIMULATION_FAILED'))
    assert not runtime.state.data['positions']['mint']['exit']['first_partial_done']
    assert not runtime.state.data['pending']


def test_quiet_market_uses_original_verified_receipt_not_order_start(runtime):
    runtime.state.data['pending']['id'] = dict(
        payload=dict(side='BUY', mint='mint', submitted_ns=9_000_000_000),
        context=dict(creator='creator', reference_price=1, launch_price=1), reason='ENTRY')
    runtime.apply_result('id', dict(status='CONFIRMED', tokens_raw=100, sol_delta_lamports=-100,
        reference_price=1, confirmed_ns=9_999_999_999))
    assert runtime.state.data['positions']['mint']['exit']['fill_ns'] == 9_999_999_999
    emitted = []
    runtime.order = lambda payload, context, reason: emitted.append((payload, reason))
    runtime.consider_exit('mint', runtime.clock(), timer_only=True)
    assert not emitted
    runtime.consider_exit('mint', 11_999_999_999, timer_only=True)
    assert emitted[0][1] == 'MAXIMUM_HOLD'


def test_no_second_sell_while_first_unresolved(runtime):
    position(runtime)
    runtime.state.data['pending']['id'] = dict(payload=dict(mint='mint', side='SELL'))
    runtime.order = lambda *a, **kw: pytest.fail('duplicate order')
    runtime.consider_exit('mint', 11_000_000_000, timer_only=True)


def pending_buy(runner, *, runtime_id=None):
    runner.state.data['pending']['buy-id'] = dict(
        payload=dict(side='BUY', mint='mint', submitted_ns=8_000_000_000),
        context=dict(creator='creator', reference_price=1, launch_price=1), reason='ENTRY',
        runtime_id=runtime_id or runner.diagnostics.run_id)


def market_event(price=1, *, own=False, complete=False):
    return dict(anchor_event='CompleteEvent' if complete else 'TradeEvent', mint='mint',
                user='wallet' if own else 'other', is_buy=True,
                virtual_sol_reserves=int(price * 1_000_000_000),
                virtual_token_reserves=1_000_000)


def buffer_market(runner, signature, ns, price=1, **kwargs):
    asyncio.run(runner.on_batch(signature, [market_event(price, **kwargs)], ns))


def confirm_buy(runner, **extra):
    runner.apply_result('buy-id', dict(status='CONFIRMED', tokens_raw=100,
        sol_delta_lamports=-100, reference_price=1, signature='our-buy',
        confirmed_ns=runner.clock(), **extra))


def test_verified_fill_observation_gets_full_hold_and_logs_clock(runtime):
    pending_buy(runtime)
    events = []
    runtime.emit = lambda name, payload: events.append((name, payload))
    buffer_market(runtime, 'our-buy', 9_000_000_000, own=True)
    confirm_buy(runtime)
    pos = runtime.state.data['positions']['mint']
    assert pos['exit']['fill_ns'] == 9_000_000_000
    assert pos['fill_clock_source'] == 'CONFIRMED_FEED_MATCH'
    assert events[-1][1]['hold_deadline_ns'] == 11_000_000_000
    orders = []
    runtime.order = lambda payload, context, reason: orders.append((payload, reason))
    runtime.consider_exit('mint', 10_999_999_999, timer_only=True)
    assert not orders
    runtime.consider_exit('mint', 11_000_000_000, timer_only=True)
    assert orders[0][1] == 'MAXIMUM_HOLD'


@pytest.mark.parametrize('path,expected,quantity', [
    ([(1.2, False)], 'FIRST_TAKE', '30'),
    ([(.65, False), (1.1, False)], 'TRAILING_OR_STOP', '100'),
    ([(1.2, False), (.85, False), (1.2, False)], 'TRAILING_OR_STOP', '100'),
    ([(1.0, True), (1.2, False)], 'LIQUIDITY_EMERGENCY', '100'),
])
def test_pending_buy_replays_peak_and_retains_triggered_exit(runtime, path, expected, quantity):
    pending_buy(runtime)
    buffer_market(runtime, 'before-buy', 8_500_000_000, 5)  # Must not create a fake peak/take.
    buffer_market(runtime, 'our-buy', 9_000_000_000, own=True)
    for index, (price, complete) in enumerate(path):
        buffer_market(runtime, 'market-' + str(index), 9_100_000_000 + index * 100_000_000,
                      price, complete=complete)
    assert not runtime.state.data['positions']
    confirm_buy(runtime)
    pos = runtime.state.data['positions']['mint']
    assert pos['replayed_market_events'] == len(path)
    assert pos['exit']['peak_price'] < 5
    assert pos['remaining'] == 100 and pos['exit']['first_partial_done'] is False
    orders = []
    runtime.order = lambda payload, context, reason: orders.append((payload, reason))
    runtime.consider_exit('mint', runtime.clock(), timer_only=True)
    assert orders[0][1] == expected and orders[0][0]['tokens_raw'] == quantity


def test_pending_buffer_persists_across_restart_without_new_deadline(runtime, tmp_path):
    pending_buy(runtime)
    buffer_market(runtime, 'our-buy', 9_000_000_000, own=True)
    buffer_market(runtime, 'stop', 9_100_000_000, .6)
    restored = State(tmp_path / 'state.sqlite', wallet='wallet', mode='live')
    try:
        runner = Runtime(model=MODEL, state=restored, rpc=None, builder=None,
            execution=SimpleNamespace(), wallet='wallet', metadata_session=None,
            emit=lambda *a: None, kill_path=tmp_path / 'kill', clock=lambda: 12_000_000_000)
        confirm_buy(runner)
        pos = runner.state.data['positions']['mint']
        assert pos['exit']['fill_ns'] == 9_000_000_000
        assert pos['exit_latched_reason'] == 'TRAILING_OR_STOP'
        orders = []
        runner.order = lambda payload, context, reason: orders.append((payload, reason))
        runner.consider_exit('mint', runner.clock(), timer_only=True)
        assert orders[0][1] == 'MAXIMUM_HOLD' and orders[0][0]['tokens_raw'] == '100'
    finally:
        restored.conn.close()


def test_recovered_unknown_buy_cannot_gain_fresh_window(runtime):
    pending_buy(runtime, runtime_id='previous-process')
    runtime.started_ns = 9_000_000_000
    confirm_buy(runtime)
    pos = runtime.state.data['positions']['mint']
    assert pos['exit']['fill_ns'] == 8_000_000_000
    assert pos['forced_exit'] and pos['recovery_exit_reason'] == 'FILL_CLOCK_UNCERTAIN'


def test_delayed_unanchored_receipt_cannot_gain_fresh_window(runtime):
    pending_buy(runtime)
    runtime.clock = lambda: 12_000_000_000
    confirm_buy(runtime)
    pos = runtime.state.data['positions']['mint']
    assert pos['exit']['fill_ns'] == 8_000_000_000
    assert pos['forced_exit'] and pos['recovery_exit_reason'] == 'FILL_CLOCK_UNCERTAIN'


def test_receipt_fallback_does_not_treat_someone_elses_buy_as_our_fill(runtime):
    pending_buy(runtime)
    buffer_market(runtime, 'somebody-else', 9_000_000_000, own=True)
    buffer_market(runtime, 'not-our-peak', 9_100_000_000, 3)
    confirm_buy(runtime)
    pos = runtime.state.data['positions']['mint']
    assert pos['exit']['fill_ns'] == runtime.clock()
    assert pos['exit']['peak_price'] == 1 and pos['replayed_market_events'] == 0
    assert pos['fill_clock_source'] == 'VERIFIED_RECEIPT_OBSERVED'


def test_buffer_overflow_cannot_silently_lose_protective_events(runtime):
    pending_buy(runtime)
    buffer_market(runtime, 'our-buy', 9_000_000_000, own=True)
    for index in range(513):
        runtime.sequence += 1
        runtime.buffer_pending_market(str(index), market_event(), 9_100_000_000 + index)
    assert len(runtime.state.data['pending']['buy-id']['market']) == 512
    confirm_buy(runtime)
    pos = runtime.state.data['positions']['mint']
    assert pos['forced_exit'] and pos['recovery_exit_reason'] == 'PENDING_MARKET_INCOMPLETE'


def test_late_own_buy_notification_does_not_trigger_take_on_own_price_impact(runtime):
    pending_buy(runtime)
    confirm_buy(runtime)
    runtime.order = lambda *a, **kw: pytest.fail('own entry caused a false take')
    buffer_market(runtime, 'our-buy', 10_100_000_000, 1.3, own=True)
    assert runtime.state.data['positions']['mint']['exit']['peak_price'] == 1


def test_stop_crossed_during_sell_remains_due_even_if_market_recovers(runtime):
    position(runtime)
    runtime.state.data['pending']['sell-id'] = dict(
        payload=dict(mint='mint', side='SELL', tokens_raw='30'), context={}, reason='FIRST_TAKE')
    runtime.consider_exit('mint', 9_000_000_000, price=.65)
    runtime.consider_exit('mint', 9_100_000_000, price=1.2)
    runtime.apply_result('sell-id', dict(status='CONFIRMED', tokens_raw=30, sol_delta_lamports=50))
    orders = []
    runtime.order = lambda payload, context, reason: orders.append((payload, reason))
    runtime.consider_exit('mint', runtime.clock(), timer_only=True)
    assert orders[0][1] == 'TRAILING_OR_STOP' and orders[0][0]['tokens_raw'] == '71'


def test_unknown_and_duplicate_receipts_do_not_apply_buffer_twice(runtime):
    pending_buy(runtime)
    buffer_market(runtime, 'our-buy', 9_000_000_000, own=True)
    buffer_market(runtime, 'take', 9_100_000_000, 1.2)
    buffer_market(runtime, 'take', 9_100_000_000, 1.2)
    runtime.apply_result('buy-id', dict(status='UNKNOWN'))
    assert not runtime.state.data['positions']
    assert len(runtime.state.data['pending']['buy-id']['market']) == 2
    confirm_buy(runtime)
    confirm_buy(runtime)
    assert runtime.state.data['positions']['mint']['replayed_market_events'] == 1
    assert runtime.state.data['positions']['mint']['remaining'] == 100


def test_deadline_supersedes_buffered_take(runtime):
    pending_buy(runtime)
    buffer_market(runtime, 'our-buy', 8_500_000_000, own=True)
    buffer_market(runtime, 'take', 9_000_000_000, 1.2)
    confirm_buy(runtime)
    orders = []
    runtime.order = lambda payload, context, reason: orders.append((payload, reason))
    runtime.consider_exit('mint', 10_500_000_000, timer_only=True)
    assert orders[0][1] == 'MAXIMUM_HOLD' and orders[0][0]['tokens_raw'] == '100'


def test_delayed_feed_observation_cannot_disguise_unknown_fill_time(runtime):
    pending_buy(runtime)
    runtime.clock = lambda: 12_000_000_000
    buffer_market(runtime, 'our-buy', 11_000_000_000, own=True)
    confirm_buy(runtime)
    pos = runtime.state.data['positions']['mint']
    assert pos['forced_exit'] and pos['exit']['fill_ns'] == 8_000_000_000


def test_existing_position_keeps_its_deadline_after_runtime_restart(runtime, tmp_path):
    position(runtime)
    runtime.state.save()
    state = State(tmp_path / 'state.sqlite', wallet='wallet', mode='live')
    try:
        runner = Runtime(model=MODEL, state=state, rpc=None, builder=None,
            execution=SimpleNamespace(), wallet='wallet', metadata_session=None,
            emit=lambda *a: None, kill_path=tmp_path / 'kill', clock=lambda: 10_000_000_000)
        assert runner.state.data['positions']['mint']['exit']['fill_ns'] == 8_500_000_000
    finally:
        state.conn.close()


def test_missing_receipt_reference_forces_exit_only(runtime):
    runtime.state.data['pending']['id'] = dict(payload=dict(side='BUY', mint='mint', submitted_ns=1),
        context=dict(reference_price=1, launch_price=1, creator='c'), reason='ENTRY')
    runtime.apply_result('id', dict(status='CONFIRMED', tokens_raw=100, sol_delta_lamports=-100,
                                   reference_price=None))
    assert runtime.state.data['positions']['mint']['forced_exit']
    assert not runtime.safety.snapshot().entries_allowed


def test_reconcile_does_not_touch_currently_building_order(runtime):
    runtime.state.data['pending']['id'] = dict(payload=dict(mint='mint'))
    runtime.inflight_orders.add('id')
    asyncio.run(runtime.reconcile())
    assert 'id' in runtime.state.data['pending']


def test_restart_cancels_unsigned_intent_without_replaying(runtime):
    journal = ExecutionJournal(runtime.state.conn)
    payload = dict(side='BUY', mint='mint', submitted_ns=1)
    journal.prepare('id', payload)
    runtime.state.data['pending']['id'] = dict(payload=payload, context={}, reason='ENTRY')
    runtime.execution = SimpleNamespace(journal=journal)
    asyncio.run(runtime.reconcile())
    assert not runtime.state.data['pending']


@pytest.mark.parametrize('host', ['127.0.0.1', '[::1]', '169.254.169.254', '10.0.0.1'])
def test_metadata_blocks_literal_private_addresses(host):
    with pytest.raises(ValueError):
        asyncio.run(metadata(None, 'https://' + host + '/key'))


def test_social_status_only_trusted_hosts_and_status_paths():
    assert social_status({'twitter': 'https://x.com/test/status/2000000000000000000'})[0] == 'test'
    for url in ['https://x.com.evil.test/test/status/2000000000000000000', 'https://x.com/test',
                'http://x.com/test/status/2000000000000000000']:
        with pytest.raises(ValueError):
            social_status({'twitter': url})


def test_disabled_execution_never_constructs_signer():
    with pytest.raises(PermissionError):
        LiveExecution(settings=SimpleNamespace(live=False), rpc=None, builder=None, journal=None)


def transaction(delta=100):
    def token(raw):
        return dict(owner='wallet', mint='mint', uiTokenAmount=dict(amount=str(raw), decimals=6))
    return dict(slot=12, transaction={'message': {'accountKeys': [{'pubkey': 'wallet'}]}},
        meta=dict(err=None, preTokenBalances=[token(200)], postTokenBalances=[token(200 + delta)],
                  preBalances=[1000], postBalances=[900], logMessages=[]))


def test_receipt_uses_transaction_metadata_only():
    result = receipt(transaction(), wallet='wallet', mint='mint', side='BUY', submitted_ns=1)
    assert result['tokens_raw'] == 100 and result['sol_delta_lamports'] == -100
    assert result['reference_price'] is None
    with pytest.raises(RuntimeError):
        receipt(transaction(0), wallet='wallet', mint='mint', side='BUY', submitted_ns=1)


def test_chain_rejection_is_terminal():
    tx = transaction()
    tx['meta']['err'] = {'InstructionError': [1, 'Custom']}
    assert receipt(tx, wallet='wallet', mint='mint', side='BUY', submitted_ns=1)['status'] == 'FAILED'


def test_identical_bytes_on_both_routes_and_no_rebuild_on_retry(tmp_path):
    class FakeBuilder:
        calls = 0
        async def request(self, payload):
            self.calls += 1
            return dict(simulation_passed=True, transaction_base64='unsigned')
    class FakeSigner:
        async def sign(self, value):
            assert value == 'unsigned'
            return 'SAME_SIGNED_BYTES', 'signature'
    class FakeRpc:
        urls = ('a', 'b')
        sent = []
        async def call(self, method, params, url=None):
            if method == 'sendTransaction':
                self.sent.append((url, params[0]))
                return 'signature'
            await asyncio.sleep(0)
            return transaction()
    ex = LiveExecution.__new__(LiveExecution)
    ex.wallet, ex.rpc, ex.builder, ex.signer = 'wallet', FakeRpc(), FakeBuilder(), FakeSigner()
    ex.allow_buy = lambda: True
    ex.journal = ExecutionJournal(tmp_path / 'journal.sqlite')
    payload = dict(side='BUY', mint='mint', submitted_ns=1)
    assert asyncio.run(ex.execute('id', payload))['status'] == 'CONFIRMED'
    assert asyncio.run(ex.execute('id', payload))['status'] == 'CONFIRMED'
    assert ex.builder.calls == 1
    assert ex.rpc.sent == [('a', 'SAME_SIGNED_BYTES'), ('b', 'SAME_SIGNED_BYTES')]
    ex.journal.close()


def test_stop_during_build_prevents_signing(tmp_path):
    ex = LiveExecution.__new__(LiveExecution)
    permitted = [True]
    ex.allow_buy = lambda: permitted[0]
    ex.wallet = 'wallet'
    ex.journal = ExecutionJournal(tmp_path / 'journal.sqlite')
    class Builder:
        async def request(self, payload):
            permitted[0] = False
            return dict(simulation_passed=True, transaction_base64='unsigned')
    ex.builder = Builder()
    # No signer/RPC attributes are supplied: any attempt to use either must fail the test.
    result = asyncio.run(ex.execute('id', dict(side='BUY', mint='mint', submitted_ns=1)))
    assert result['status'] == 'FAILED'
    assert ex.journal.by_request('id').signature is None
    ex.journal.close()


def test_audit_failure_cannot_apply_a_fill_twice(runtime):
    position(runtime)
    runtime.state.data['pending']['id'] = dict(payload=dict(side='SELL', mint='mint', tokens_raw='30'),
        context={}, reason='FIRST_TAKE')
    def broken_audit(*a):
        raise OSError('disk full')
    runtime.emit = broken_audit
    result = dict(status='CONFIRMED', tokens_raw=30, sol_delta_lamports=50)
    with pytest.raises(OSError):
        runtime.apply_result('id', result)
    assert runtime.state.data['positions']['mint']['remaining'] == 71
    assert not runtime.state.data['pending']
    runtime.apply_result('id', result)
    assert runtime.state.data['positions']['mint']['remaining'] == 71


def test_terminal_failure_never_rebuilds(tmp_path):
    ex = LiveExecution.__new__(LiveExecution)
    ex.journal = ExecutionJournal(tmp_path / 'journal.sqlite')
    payload = dict(side='BUY', mint='mint', submitted_ns=1)
    entry = ex.journal.prepare('id', payload)
    ex.journal.mark_failed(entry.idempotency_key, 'simulation_failed', terminal=True)
    assert asyncio.run(ex.execute('id', payload))['status'] == 'FAILED'
    ex.journal.close()


def test_slow_or_empty_primary_does_not_hide_secondary_confirmation():
    async def scenario():
        class Rpc:
            urls = ('slow', 'fast')
            async def call(self, method, params, url=None):
                if url == 'slow':
                    await asyncio.sleep(60)
                return transaction()
        ex = LiveExecution.__new__(LiveExecution)
        ex.rpc = Rpc()
        assert (await asyncio.wait_for(ex.confirmed_transaction('signature'), .5))['slot'] == 12
    asyncio.run(scenario())


@pytest.mark.parametrize('live', [False, True])
@pytest.mark.parametrize('promoted', [False, True, 'original_review'])
def test_native_feed_to_frozen_entry_partial_and_deadline(tmp_path, monkeypatch, live, promoted):
    import memecoin_bot.v12_prearmed_runtime as module
    async def scenario():
        now = [1_790_000_000_000_000_000]
        creator = next(k for k, v in MODEL['creator_handles'].items() if v)
        handle = MODEL['creator_handles'][creator][0].lower().lstrip('@')
        creator_library = None
        if promoted:
            from memecoin_bot.v12_prearmed_library import PrearmedLibrary, LIBRARY_PATH, ACTIVATION_PATH
            library_path = 'tests/fixtures/v12-prearmed-library-promotions-before-review.json' if promoted is True else LIBRARY_PATH
            payload = json.loads((ROOT/library_path).read_text())
            auth = json.loads((ROOT/ACTIVATION_PATH).read_text())
            if promoted is True:
                auth['library_sha256'] = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            creator_library = PrearmedLibrary(payload, auth, MODEL)
            if promoted is True:
                creator = next(iter(set(creator_library.promoted) - creator_library.frozen))
                handle = 'fresh_handle'
            else:
                assert not creator_library.promoted
        status = ((now[0] // 1_000_000 - 1000 - 1288834974657) << 22)
        async def mock_metadata(*a, **kwargs):
            return {'twitter': f'https://x.com/{handle}/status/{status}'}, now[0]
        monkeypatch.setattr(module, 'metadata', mock_metadata)
        class Build:
            async def request(self, request):
                assert request['action'] == 'QUOTE'
                return dict(quoted_tokens_raw='100', curve_price=0.00000003)
        class Execute:
            calls = []
            async def execute(self, request_id, payload):
                self.calls.append(payload)
                amount = 100 if payload['side'] == 'BUY' else int(payload['tokens_raw'])
                return dict(status='CONFIRMED', tokens_raw=amount,
                    sol_delta_lamports=-100 if payload['side'] == 'BUY' else amount * 2,
                    reference_price=0.00000003, confirmed_ns=now[0])
        state = State(tmp_path / 'e2e.sqlite', wallet='wallet', mode='live' if live else 'shadow')
        executor = Execute() if live else None
        runner = Runtime(model=MODEL, state=state, rpc=None, builder=Build(), execution=executor,
            wallet='wallet', metadata_session=None, emit=lambda *a: None, kill_path=tmp_path / 'kill', clock=lambda: now[0],
            creator_library=creator_library)
        runner.balance, runner.balance_ns, runner.connected = 1_000_000_000, now[0], True
        create = dict(anchor_event='CreateEvent', creator=creator, mint='mint', is_mayhem_mode=False,
            timestamp=now[0] // 1_000_000_000, uri='https://metadata.test',
            virtual_sol_reserves=30_000_000_000, virtual_token_reserves=1_000_000_000_000_000)
        seed = dict(anchor_event='TradeEvent', mint='mint', user=creator, is_buy=True, sol_amount=2_000_000_000,
            virtual_sol_reserves=30_000_000_000, virtual_token_reserves=1_000_000_000_000_000)
        await runner.on_batch('creation-signature', [create, seed], now[0])
        while runner.tasks:
            await asyncio.gather(*runner.tasks)
        assert runner.candidates == 1
        await runner.on_batch('creation-signature', [create, seed], now[0])
        if not live:
            assert not state.data['positions'] and not state.data['pending']
        else:
            assert len(executor.calls) == 1 and state.data['positions']['mint']['remaining'] == 100
            now[0] += 100_000_000
            await runner.on_batch('take-signature', [{**seed, 'virtual_sol_reserves': 36_000_000_000}], now[0])
            while runner.tasks:
                await asyncio.gather(*runner.tasks)
            assert state.data['positions']['mint']['remaining'] == 70
            now[0] += 2_000_000_000
            runner.consider_exit('mint', now[0], timer_only=True)
            while runner.tasks:
                await asyncio.gather(*runner.tasks)
            assert not state.data['positions'] and not state.data['pending']
            assert [p['tokens_raw'] for p in executor.calls if p['side'] == 'SELL'] == ['30', '70']
        state.conn.close()
    asyncio.run(scenario())

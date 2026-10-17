import asyncio
import json
from types import SimpleNamespace
import pytest

from memecoin_bot.v12_prearmed_io import BuilderLanes, LiveExecution
from memecoin_bot.v12_prearmed_notices import format_notice
from memecoin_bot.v12_prearmed_ops import OperationLedger
from memecoin_bot.v12_execution_journal import ExecutionJournal
from memecoin_bot.v12_prearmed_discord import BotPanel


def test_exit_lane_can_finish_while_entry_quote_is_stuck():
    async def scenario():
        lanes=BuilderLanes(('unused',));started=asyncio.Event();release=asyncio.Event()
        async def quote(payload):
            started.set();await release.wait();return 'quote'
        async def sell(payload): return 'sell'
        lanes.quote.request=quote;lanes.sell.request=sell
        task=asyncio.create_task(lanes.request({'action':'QUOTE','side':'BUY'}))
        await started.wait()
        assert await asyncio.wait_for(lanes.request({'action':'BUILD','side':'SELL'}),.1)=='sell'
        assert not task.done()
        release.set();assert await task=='quote'
    asyncio.run(scenario())


def test_entry_expiring_during_build_cannot_reach_signer(tmp_path):
    async def scenario():
        execution=LiveExecution.__new__(LiveExecution)
        execution.journal=ExecutionJournal(tmp_path/'journal.sqlite')
        execution.allow_buy=lambda:True
        async def build(payload):return {'simulation_passed':True,'transaction_base64':'not signed'}
        async def sign(*args):pytest.fail('expired buy reached signer')
        execution.builder=SimpleNamespace(request=build);execution.signer=SimpleNamespace(sign=sign)
        result=await execution.execute('expired',{'side':'BUY','mint':'token','submitted_ns':1,'expires_ns':1})
        assert result=={'status':'FAILED','reason':'ENTRY_EXPIRED'}
        assert execution.journal.by_request('expired').signature is None
        execution.journal.close()
    asyncio.run(scenario())


def test_critical_notices_are_distinct_and_timestamped():
    message,priority=format_notice('EXECUTION_OUTCOME_UNKNOWN',{},ts_ns=1790540000000000000)
    assert message.startswith('🚨 Gambit') and priority==1 and '21:13 BST' in message
    assert 'Do not retry' in message and 'send a replacement' in message
    message,priority=format_notice('FILL_CONFIRMED',{'side':'SELL','signature':'publicreceipt'})
    assert message.startswith('💱 Gambit') and 'Sell confirmed' in message and priority==0


def test_every_discord_command_is_under_the_owner_checked_group(tmp_path):
    async def scenario():
        ledger=OperationLedger(tmp_path/'ops.sqlite',owner_id='1533142248596836619')
        panel=BotPanel(ledger,None)
        assert {c.name for c in panel.commands} == {'status','library','logs','operations','start','stop','restart','check','transfer','configure','releases','update','live_test','recover','reset_safety','acknowledge'}
        sent=[]
        async def message(*args,**kwargs):sent.append(args)
        attacker=SimpleNamespace(user=SimpleNamespace(id=1),response=SimpleNamespace(send_message=message))
        assert await panel.interaction_check(attacker) is False and sent
        owner=SimpleNamespace(user=SimpleNamespace(id=1533142248596836619))
        assert await panel.interaction_check(owner) is True
        ledger.db.close()
    asyncio.run(scenario())


def test_stopped_state_does_not_hide_orphaned_positions_or_provider_limits():
    from memecoin_bot.v12_prearmed_health import assessment
    report={'service':{'ActiveState':'inactive'},'database':{'positions':[{'mint':'held'}]},'execution_worker_running':False}
    problems=assessment(report,[{'provider_index':1,'health':'UNAVAILABLE','http_status':429}],intentionally_stopped=True)
    assert any('Open positions' in item for item in problems)
    assert any('request limit' in item for item in problems)
    assert 'Trading service is not running' not in problems


def test_command_failure_keeps_safe_reason_without_exporting_provider_urls(tmp_path):
    from memecoin_bot.v12_prearmed_operator_backend import OperatorBackend,ControlBlocked
    backend=OperatorBackend(tmp_path,runner=lambda *args,**kwargs:SimpleNamespace(returncode=1,stdout='BLOCKED: Stop marker is set.'))
    with pytest.raises(ControlBlocked,match='Stop marker is set'):
        backend.command('start-live')
    backend.runner=lambda *args,**kwargs:SimpleNamespace(returncode=1,stdout='BLOCKED: https://provider/?token=SECRET')
    with pytest.raises(ControlBlocked) as caught:backend.command('start-live')
    assert 'SECRET' not in str(caught.value)


def test_stop_cancels_start_waiting_for_readiness(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from memecoin_bot.v12_prearmed_operator_backend import OperatorBackend,ControlBlocked
    checking,release,stopped=threading.Event(),threading.Event(),threading.Event()
    calls=[]
    def runner(argv,**kwargs):
        command=argv[2];calls.append(command)
        if command=='final-check':
            checking.set();assert release.wait(5)
        elif command=='stop' and checking.is_set():stopped.set()
        return SimpleNamespace(returncode=0,stdout='')
    backend=OperatorBackend(tmp_path,runner=runner)
    backend.status=lambda:{'service':{'ActiveState':'inactive'}}
    with ThreadPoolExecutor(max_workers=2) as pool:
        start=pool.submit(backend.execute,{'action':'start','payload':{}})
        assert checking.wait(5)
        stop=pool.submit(backend.execute,{'action':'stop','payload':{}})
        assert stopped.wait(5)
        release.set()
        with pytest.raises(ControlBlocked,match='newer Stop'):start.result(5)
        assert stop.result(5)['state']=='COMPLETE'
    assert 'start-live' not in calls and calls[-1]=='stop'

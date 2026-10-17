"""Speed changes retain isolation, cancellation and credential-safe timings."""
import asyncio
import json
import math
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from memecoin_bot.v12_prearmed_io import Builder, BuilderLanes, LiveExecution, metadata_trace_config
from memecoin_bot.v12_prearmed_diagnostics import failure


def test_real_node_warmup_keeps_all_three_unsigned_lanes_ready(monkeypatch):
    monkeypatch.setenv('E4_PRIMARY_RPC_URL','https://unused-primary.invalid')
    monkeypatch.setenv('E4_FALLBACK_RPC_URLS','https://unused-secondary.invalid')
    script=Path(__file__).resolve().parents[1]/'tools/e4-builder/frozen-prearmed.mjs'
    node=shutil.which('node')
    assert node, 'Node is a required production dependency'
    async def scenario():
        lanes=BuilderLanes((node,str(script)))
        try:
            await lanes.warmup()
            workers=(lanes.quote,lanes.buy,lanes.sell)
            pids=[w.process.pid for w in workers]
            assert len(set(pids))==3
            for worker in workers:
                assert (await worker.request({'action':'WARMUP'}))['ok'] is True
            assert pids==[w.process.pid for w in workers]
        finally:await lanes.close()
    asyncio.run(scenario())


def test_cancelled_unsigned_builder_request_is_not_reused_by_next_request(tmp_path):
    script=tmp_path/'unsigned-worker.py'
    script.write_text('import json,sys,time\nfor line in sys.stdin:\n r=json.loads(line)\n if r.get("slow"): time.sleep(5)\n print(json.dumps({"request_id":r["request_id"],"result":{"ok":True}}),flush=True)\n')
    async def scenario():
        builder=Builder((sys.executable,str(script)))
        try:
            task=asyncio.create_task(builder.request({'slow':True}))
            while builder.process is None:await asyncio.sleep(.01)
            process=builder.process
            await asyncio.sleep(.05);task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
            assert process.returncode is not None and builder.process is None
            assert (await builder.request({}))['ok'] is True
        finally:await builder.close()
    asyncio.run(scenario())


def test_metadata_trace_records_phases_without_request_secrets():
    async def scenario():
        trace=metadata_trace_config();trace.freeze();data={}
        context=SimpleNamespace(trace_request_ctx=data)
        secret=SimpleNamespace(url='https://example/?token=SECRET',headers={'token':'SECRET'})
        await trace.on_dns_resolvehost_start.send(None,context,secret)
        await trace.on_dns_resolvehost_end.send(None,context,secret)
        await trace.on_connection_reuseconn.send(None,context,secret)
        assert data['dns_end']>=data['dns_start'] and 'reused' in data
        assert 'SECRET' not in json.dumps(data)
    asyncio.run(scenario())


def test_builder_timings_reject_unknown_fields_and_nonfinite_values():
    error=RuntimeError('BUILDER:ENTRY_CHASE_GUARD_REJECTED')
    error.builder_timings={'state_read_ms':12.4,'total_ms':float('nan'),'simulation_ms':-1,'url':'SECRET'}
    result=failure('quote',error)
    assert result['builder_state_read_ms']==12.4
    assert 'builder_total_ms' not in result and 'builder_simulation_ms' not in result
    assert 'SECRET' not in json.dumps(result)


def test_null_then_confirmed_provider_keeps_polling_while_sibling_is_stalled():
    async def scenario():
        calls=[];cancelled=[]
        async def call(method,args,*,url):
            assert method=='getTransaction' and args[1]['commitment']=='confirmed'
            calls.append(url)
            if url=='stalled':
                try:await asyncio.sleep(10)
                finally:cancelled.append(url)
            return {'slot':12} if calls.count('fast')>=2 else None
        execution=LiveExecution.__new__(LiveExecution)
        execution.rpc=SimpleNamespace(urls=('stalled','fast'),call=call)
        result=await asyncio.wait_for(execution.confirmed_transaction('same-signature',wait_seconds=2),.8)
        assert result=={'slot':12} and calls.count('fast')==2 and calls.count('stalled')==1
        assert cancelled==['stalled']
    asyncio.run(scenario())


def test_receipt_wait_deadline_cancels_reads_and_does_not_send_transactions():
    async def scenario():
        finished=[]
        async def call(method,args,*,url):
            assert method=='getTransaction'
            try:await asyncio.sleep(10)
            finally:finished.append(url)
        execution=LiveExecution.__new__(LiveExecution)
        execution.rpc=SimpleNamespace(urls=('one','two'),call=call)
        assert await asyncio.wait_for(execution.confirmed_transaction('existing',wait_seconds=.05),.3) is None
        assert set(finished)=={'one','two'}
    asyncio.run(scenario())

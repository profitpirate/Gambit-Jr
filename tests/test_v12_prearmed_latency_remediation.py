"""Failure, cancellation and evidence tests; no wallet or financial operations."""
import asyncio
import importlib.util
import json
import shutil
import time
from pathlib import Path

import pytest

from memecoin_bot.v12_prearmed_diagnostics import DiagnosticError, failure
from memecoin_bot.v12_prearmed_io import metadata, retry_after_seconds
from memecoin_bot.v12_prearmed_metadata import MetadataRequests
from memecoin_bot.v12_prearmed_operator_backend import OperatorBackend


def test_concurrent_exact_uri_is_shared_but_completed_result_is_not_cached():
    async def scenario():
        fetches=[];started=asyncio.Event();ready=asyncio.Event();reader=MetadataRequests()
        async def getter(session,uri,**kwargs):
            fetches.append(uri);started.set();await ready.wait()
            return {'twitter':{'status':'original'}},time.time_ns()
        a=asyncio.create_task(reader.fetch(getter,None,'https://example/token'))
        await started.wait()
        b=asyncio.create_task(reader.fetch(getter,None,'https://example/token'))
        await asyncio.sleep(0);ready.set();one,two=await asyncio.gather(a,b)
        assert len(fetches)==1 and one==two and not reader.active
        one[0]['twitter']['status']='mutated';assert two[0]['twitter']['status']=='original'
        await reader.fetch(getter,None,'https://example/token');assert len(fetches)==2
    asyncio.run(scenario())


def test_different_uri_query_is_not_shared():
    async def scenario():
        calls=[];reader=MetadataRequests()
        async def getter(session,uri,**kwargs):
            calls.append(uri);await asyncio.sleep(.01);return {'uri':uri},1
        result=await asyncio.gather(*(reader.fetch(getter,None,u) for u in ('https://example/?v=1','https://example/?v=2')))
        assert len(calls)==2 and result[0]!=result[1]
    asyncio.run(scenario())


def test_cancelled_subscriber_does_not_cancel_other_candidate():
    async def scenario():
        reader=MetadataRequests();entered=asyncio.Event();ready=asyncio.Event();finished=[]
        async def getter(*args,**kwargs):
            entered.set()
            try:await ready.wait();return {},1
            finally:finished.append(True)
        a=asyncio.create_task(reader.fetch(getter,None,'uri'));await entered.wait()
        b=asyncio.create_task(reader.fetch(getter,None,'uri'));await asyncio.sleep(0)
        a.cancel()
        with pytest.raises(asyncio.CancelledError):await a
        assert not finished
        ready.set();assert await b==({},1);assert finished and not reader.active
    asyncio.run(scenario())


def test_last_subscriber_cancellation_closes_underlying_request():
    async def scenario():
        reader=MetadataRequests();entered=asyncio.Event();finished=[]
        async def getter(*args,**kwargs):
            entered.set()
            try:await asyncio.sleep(10)
            finally:finished.append(True)
        task=asyncio.create_task(reader.fetch(getter,None,'uri'));await entered.wait();task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert finished and not reader.active
    asyncio.run(scenario())


def test_late_subscriber_keeps_own_shorter_deadline():
    async def scenario():
        reader=MetadataRequests();entered=asyncio.Event()
        async def getter(*args,**kwargs):
            entered.set();await asyncio.sleep(.06);return {},1
        first=asyncio.create_task(reader.fetch(getter,None,'uri'));await entered.wait()
        with pytest.raises(DiagnosticError,match='METADATA_TIMEOUT'):
            await reader.fetch(getter,None,'uri',timeout_seconds=.01)
        assert await first==({},1) and not reader.active
    asyncio.run(scenario())


def test_failed_shared_request_does_not_poison_next_candidate():
    async def scenario():
        reader=MetadataRequests();calls=[]
        async def getter(*args,**kwargs):
            calls.append(1)
            if len(calls)==1:raise DiagnosticError('METADATA_HTTP_REJECTED',http_status=404)
            return {},1
        with pytest.raises(DiagnosticError):await reader.fetch(getter,None,'uri')
        assert await reader.fetch(getter,None,'uri')==({},1)
    asyncio.run(scenario())


def test_retry_after_beyond_candidate_deadline_does_not_hammer_host():
    class Response:
        status=429;headers={'Retry-After':'60'}
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    class Session:
        calls=0
        def get(self,*args,**kwargs):self.calls+=1;return Response()
    session=Session();events=[]
    with pytest.raises(DiagnosticError):asyncio.run(metadata(session,'https://metadata.example/token',trace=lambda **d:events.append(d)))
    assert session.calls==1 and events[-1]['retry_after_seconds']==60 and events[-1]['retrying'] is False


@pytest.mark.parametrize('header,expected',[('60',60),('nan',7),('SECRET',7),('999999',3600),('-10',0),(None,7)])
def test_retry_header_is_bounded(header,expected):
    assert retry_after_seconds(header,default=7)==expected


def test_rejected_quote_evidence_survives_both_exports_without_secrets(tmp_path):
    error=RuntimeError('BUILDER:ENTRY_CHASE_GUARD_REJECTED');error.provider_index=0
    error.quote_facts={'launch_price':2e-8,'curve_price':4e-8,'price_multiple':2,'maximum_price_multiple':1.5,
                       'state_slot':451000000,'secret':'SECRET'}
    facts=failure('quote',error)
    assert facts['price_multiple']==2 and facts['state_slot']==451000000 and 'secret' not in facts
    source=Path(__file__).resolve().parents[1]/'deployment/gambit-monitor-collector.py'
    shutil.copyfile(source,tmp_path/'gambit-monitor-collector.py');(tmp_path/'prearmed-data').mkdir()
    log=tmp_path/'prearmed-data/live-audit.jsonl';log.write_text(json.dumps({'ts_ns':time.time_ns(),'event':'CANDIDATE_REJECTED','payload':facts})+'\n')
    spec=importlib.util.spec_from_file_location('latency_collector',source);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    for export in (OperatorBackend(tmp_path).logs(),m.audit_tail(log,time.time_ns())):
        assert export['events'][0]['payload']['curve_price']==4e-8
        assert export['events'][0]['payload']['state_slot']==451000000
        assert 'SECRET' not in json.dumps(export)


def test_quote_evidence_rejects_nonfinite_and_noninteger_values():
    error=RuntimeError('BUILDER:ENTRY_CHASE_GUARD_REJECTED')
    error.quote_facts={'curve_price':float('inf'),'launch_price':True,'price_multiple':-3,'state_slot':2.5}
    assert not any(k in failure('quote',error) for k in error.quote_facts)

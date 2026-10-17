import asyncio
import json
from types import SimpleNamespace

import aiohttp
import pytest

from memecoin_bot.v12_prearmed_discovery_budget import DiscoveryBudget, DAILY_STREAM_BYTES, STREAM_LEASE_BYTES, public_first, provider_name
from memecoin_bot.v12_prearmed_io import Rpc, native_feed

PUBLIC='https://api.mainnet.solana.com'
HELIUS='https://mainnet.helius-rpc.com/?api-key=SECRET'
QUICK='https://example.solana-mainnet.quiknode.pro/SECRET/'

@pytest.mark.parametrize('url',['https://api.mainnet.solana.com','https://api.mainnet-beta.solana.com/'])
def test_current_and_legacy_official_public_hosts_are_recognised(url):
    assert provider_name(url)=='SOLANA_PUBLIC'
    assert public_first((HELIUS,QUICK,url))[0]==url
    assert provider_name(url.rstrip('/')+'.untrusted.example')!='SOLANA_PUBLIC'

def test_order_keeps_execution_configuration_unchanged():
    urls=(HELIUS,QUICK,PUBLIC)
    assert public_first(urls)==(PUBLIC,HELIUS,QUICK)
    assert urls==(HELIUS,QUICK,PUBLIC)

def test_stream_limit_survives_restart_and_does_not_expose_credentials(tmp_path):
    path=tmp_path/'budget.json';budget=DiscoveryBudget(path)
    count=0
    while budget.reserve_stream(HELIUS):count+=1
    assert count*STREAM_LEASE_BYTES<=DAILY_STREAM_BYTES<(count+1)*STREAM_LEASE_BYTES
    assert not DiscoveryBudget(path).reserve_stream(HELIUS)
    assert budget.reserve_stream(PUBLIC)
    assert 'SECRET' not in path.read_text()+json.dumps(budget.snapshot())
    assert not budget.reserve_stream('https://unknown.example')

def test_limits_do_not_reset_on_clock_reversal(tmp_path):
    now=[1790599400000000000]
    budget=DiscoveryBudget(tmp_path/'budget.json',clock=lambda:now[0])
    assert budget.reserve_balance(HELIUS)
    now[0]-=86_400_000_000_000
    with pytest.raises(ValueError,match='CLOCK_REVERSED'):budget.reserve_balance(HELIUS)

def test_utc_day_resets_only_at_the_next_day(tmp_path,monkeypatch):
    import memecoin_bot.v12_prearmed_discovery_budget as module
    monkeypatch.setattr(module,'DAILY_BALANCE_REQUESTS',3)
    now=[1790599400000000000]
    path=tmp_path/'budget.json';budget=DiscoveryBudget(path,clock=lambda:now[0])
    for _ in range(3):assert budget.reserve_balance(HELIUS)
    assert not budget.reserve_balance(HELIUS)
    now[0]+=86_400_000_000_000
    assert budget.reserve_balance(HELIUS)
    assert budget.snapshot()['helius_balance_requests']==1

@pytest.mark.parametrize('public_fails',[False,True])
def test_balance_uses_public_then_bounded_fallback(tmp_path,public_fails):
    calls=[]
    class Response:
        status=200
        async def __aenter__(self):return self
        async def __aexit__(self,*_):pass
        async def json(self):return {'result':{'value':100_000_000}}
    class Session:
        def post(self,url,**kwargs):
            calls.append(url)
            if public_fails and url==PUBLIC:raise aiohttp.ClientConnectionError('do not expose SECRET')
            return Response()
    budget=DiscoveryBudget(tmp_path/'budget.json');rpc=Rpc(Session(),(HELIUS,QUICK,PUBLIC),discovery_budget=budget)
    assert asyncio.run(rpc.balance_lamports('wallet'))==100_000_000
    assert calls==([PUBLIC,HELIUS] if public_fails else [PUBLIC])
    assert budget.snapshot()['helius_balance_requests']==int(public_fails)

def test_http_413_balance_endpoint_gets_bounded_cooldown_and_recovers(tmp_path,monkeypatch):
    from memecoin_bot import v12_prearmed_io as module
    now=[100.0];monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    calls=[];events=[]
    class Response:
        def __init__(self,status):self.status=status;self.headers={}
        async def __aenter__(self):return self
        async def __aexit__(self,*_):pass
        async def json(self):return {'result':{'value':100_000_000}}
    class Session:
        def post(self,url,**kwargs):
            calls.append(url)
            return Response(413 if url==PUBLIC else 200)
    budget=DiscoveryBudget(tmp_path/'budget.json')
    rpc=Rpc(Session(),(HELIUS,QUICK,PUBLIC),discovery_budget=budget)
    rpc.trace=lambda event,**details:events.append((event,details))
    async def exercise():
        assert await rpc.balance_lamports('wallet')==100_000_000
        now[0]=120
        assert await rpc.balance_lamports('wallet')==100_000_000
        now[0]=161
        assert await rpc.balance_lamports('wallet')==100_000_000
    asyncio.run(exercise())
    assert calls==[PUBLIC,HELIUS,HELIUS,PUBLIC,HELIUS]
    assert [(event,details.get('http_status')) for event,details in events]==[
        ('RPC_REQUEST_FAILED',413),('RPC_REQUEST_FAILED',413)
    ]
    assert budget.snapshot()['helius_balance_requests']==3

def test_other_rpc_methods_keep_private_priority(tmp_path):
    calls=[]
    class Response:
        status=200
        async def __aenter__(self):return self
        async def __aexit__(self,*_):pass
        async def json(self):return {'result':123}
    class Session:
        def post(self,url,**kwargs):calls.append(url);return Response()
    rpc=Rpc(Session(),(HELIUS,QUICK,PUBLIC),discovery_budget=DiscoveryBudget(tmp_path/'budget.json'))
    assert asyncio.run(rpc.call('getSlot',[]))==123
    assert calls==[HELIUS]

def test_production_feed_prefers_public_without_restricting_launch_filter(tmp_path,monkeypatch):
    from memecoin_bot import v12_prearmed_io as module
    captured=[];sent=[];events=[];health=[];stop=asyncio.Event()
    class Socket:
        close_code=1000
        async def __aenter__(self):return self
        async def __aexit__(self,*_):pass
        async def send_json(self,row):sent.append(row)
        async def receive_json(self,**_):return {'result':3}
        def __aiter__(self):return self
        async def __anext__(self):
            if stop.is_set():raise StopAsyncIteration
            return SimpleNamespace(type=aiohttp.WSMsgType.TEXT,data=json.dumps({'params':{'result':{'value':{'err':None,'signature':'signature','logs':[]}}}}))
    class Session:
        def ws_connect(self,url,**_):captured.append(url);return Socket()
    async def emit(signature,rows,observed):events.extend(rows);stop.set()
    monkeypatch.setattr(module,'anchor_events_from_logs',lambda *a:[{'anchor_event':'CreateEvent','mint':'mint'}])
    budget=DiscoveryBudget(tmp_path/'budget.json')
    asyncio.run(native_feed(Session(),(HELIUS,QUICK,PUBLIC),emit,health.append,stop,discovery_budget=budget))
    assert captured==['wss://api.mainnet.solana.com']
    assert sent[0]['params']==[{'mentions':[module.PUMP_PROGRAM_ID]},{'commitment':'confirmed'}]
    assert len(events)==1 and budget.creates_decoded==1
    assert budget.snapshot()['helius_stream_reserved_bytes']==0
    assert health==[True,False]

def test_balance_exhaustion_never_calls_paid_provider(tmp_path,monkeypatch):
    import memecoin_bot.v12_prearmed_discovery_budget as module
    monkeypatch.setattr(module,'DAILY_BALANCE_REQUESTS',0)
    calls=[];notices=[]
    class Session:
        def post(self,url,**kwargs):
            calls.append(url)
            raise aiohttp.ClientConnectionError('SECRET')
    rpc=Rpc(Session(),(HELIUS,QUICK,PUBLIC),discovery_budget=DiscoveryBudget(tmp_path/'budget.json'))
    rpc.trace=lambda event,**details:notices.append((event,details))
    for _ in range(2):
        with pytest.raises(RuntimeError,match='RPC_UNAVAILABLE:GETBALANCE'):
            asyncio.run(rpc.balance_lamports('wallet'))
    assert calls==[PUBLIC,PUBLIC]
    blocked=[r for r in notices if r[0]=='DISCOVERY_BUDGET_EXHAUSTED']
    assert len(blocked)==2 and {r[1]['provider_index'] for r in blocked}=={0,1}
    assert 'SECRET' not in json.dumps(notices)

@pytest.mark.parametrize('lease_available',[False,True])
def test_feed_fallback_budget_closes_before_reading_unreserved_messages(tmp_path,monkeypatch,lease_available):
    from memecoin_bot import v12_prearmed_io as module
    from memecoin_bot import v12_prearmed_discovery_budget as budget_module
    monkeypatch.setattr(budget_module,'DAILY_STREAM_BYTES',STREAM_LEASE_BYTES if lease_available else 0)
    budget=DiscoveryBudget(tmp_path/'budget.json');stop=asyncio.Event()
    calls=[];notices=[];reads=[]
    class Socket:
        close_code=1000
        async def __aenter__(self):return self
        async def __aexit__(self,*_):pass
        async def send_json(self,row):pass
        async def receive_json(self,**_):return {'result':3}
        def __aiter__(self):return self
        async def __anext__(self):
            reads.append(1)
            assert len(reads)==1,'Unreserved second message was read'
            return SimpleNamespace(type=aiohttp.WSMsgType.TEXT,data=json.dumps({'params':{'result':{'value':{'err':None,'signature':'signature','logs':[]}}}}))
    class Session:
        def ws_connect(self,url,**_):
            calls.append(url)
            if url=='wss://api.mainnet.solana.com':
                if len(calls)>1:stop.set()
                raise aiohttp.ClientConnectionError('SECRET')
            return Socket()
    async def emit(*_):pass
    def diagnose(event,**details):
        notices.append((event,details))
        if event=='DISCOVERY_BUDGET_EXHAUSTED':stop.set()
    monkeypatch.setattr(module,'anchor_events_from_logs',lambda *a:[])
    asyncio.run(native_feed(Session(),(HELIUS,PUBLIC),emit,lambda _:None,stop,diagnose,discovery_budget=budget))
    assert len(reads)==int(lease_available)
    if lease_available:
        assert any(r[0]=='FEED_SOURCE_ROTATION' and r[1]['reason']=='PAID_STREAM_FALLBACK_LIMIT' for r in notices)
        assert budget.snapshot()['helius_stream_reserved_bytes']==STREAM_LEASE_BYTES
    else:
        assert calls==['wss://api.mainnet.solana.com']
    assert not any(r[0]=='FEED_STREAM_ENDED' for r in notices)
    assert 'SECRET' not in json.dumps(notices)

import asyncio
import base64
from types import SimpleNamespace

import pytest
from solders.compute_budget import set_compute_unit_limit
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.system_program import transfer, TransferParams
from solders.transaction import VersionedTransaction

from memecoin_bot.v12_prearmed_expiry import expired_unseen


def entry(*, compute=True):
    key = Keypair()
    instruction = set_compute_unit_limit(400000) if compute else transfer(TransferParams(
        from_pubkey=key.pubkey(), to_pubkey=Keypair().pubkey(), lamports=1))
    tx = VersionedTransaction(MessageV0.try_compile(key.pubkey(), [instruction], [], Hash.new_unique()), [key])
    return SimpleNamespace(payload={'submitted_ns':1}, signature=str(tx.signatures[0]),
                           signed_tx_b64=base64.b64encode(bytes(tx)).decode())


class Rpc:
    urls = ['https://provider-a.test', 'https://provider-b.test']

    def __init__(self, bad=None):
        self.bad, self.calls = bad, []

    async def call(self, method, params, *, url):
        self.calls.append((method, params, url))
        assert method in ('isBlockhashValid', 'getSignatureStatuses', 'getTransaction')
        if url == self.urls[-1] and self.bad == 'outage':
            raise RuntimeError('unavailable')
        if method == 'isBlockhashValid':
            assert params[1]['commitment'] == 'finalized'
            return {'context': {'slot':100}, 'value': self.bad == 'unexpired' and url == self.urls[-1]}
        if method == 'getSignatureStatuses':
            assert params[1]['searchTransactionHistory'] is True
            if url == self.urls[-1]:
                if self.bad == 'status':return {'context':{'slot':101},'value':[{'err':None}]}
                if self.bad == 'failed':return {'context':{'slot':101},'value':[{'err':{'error':'failed'}}]}
                if self.bad == 'stale':return {'context':{'slot':99},'value':[None]}
                if self.bad == 'malformed':return {'value':[]}
            return {'context':{'slot':101},'value':[None]}
        return {'receipt':True} if self.bad == 'receipt' and url == self.urls[-1] else None


def test_agreed_expiry_is_read_only_and_queries_history_after_expiry():
    rpc=Rpc();result=asyncio.run(expired_unseen(entry(),rpc,now_ns=181_000_000_001))
    assert len(result['checks']) == 2
    for url in rpc.urls:
        assert [c[0] for c in rpc.calls if c[2]==url] == ['isBlockhashValid','getSignatureStatuses','getTransaction']


@pytest.mark.parametrize('bad',['outage','unexpired','status','failed','stale','malformed','receipt'])
def test_ambiguous_or_disagreeing_provider_never_clears(bad):
    assert asyncio.run(expired_unseen(entry(),Rpc(bad),now_ns=181_000_000_001)) is None


@pytest.mark.parametrize('condition',['young','future','unsigned','signature','nonce','single','same_host'])
def test_invalid_or_insufficient_evidence_never_clears(condition):
    e=entry(compute=condition!='nonce');rpc=Rpc();now=181_000_000_001
    if condition=='young':now=30_000_000_001
    if condition=='future':e.payload['submitted_ns']=now+1
    if condition=='unsigned':e.signed_tx_b64='AA=='
    if condition=='signature':e.signature='wrong'
    if condition=='single':rpc.urls=rpc.urls[:1]
    if condition=='same_host':rpc.urls=['https://same.test/key1','https://same.test/key2']
    assert asyncio.run(expired_unseen(e,rpc,now_ns=now)) is None
    assert not rpc.calls


def test_resolver_records_terminal_expiry_once_without_build_sign_or_send(monkeypatch):
    from memecoin_bot.v12_prearmed_io import LiveExecution
    from memecoin_bot.v12_execution_journal import ExecutionJournal
    import sqlite3
    e=entry();journal=ExecutionJournal(sqlite3.connect(':memory:',isolation_level=None))
    prepared=journal.prepare('expired-buy',{'submitted_ns':1,'mint':'mint','side':'BUY'})
    journal.mark_signed(prepared.idempotency_key,signed_tx_b64=e.signed_tx_b64,signature=e.signature)
    journal.mark_submitted(prepared.idempotency_key)
    ex=LiveExecution.__new__(LiveExecution);ex.rpc=Rpc();ex.journal=journal
    async def missing(*args,**kwargs):return None
    ex.confirmed_transaction=missing
    monkeypatch.setattr('memecoin_bot.v12_prearmed_io.time.time_ns',lambda:181_000_000_001)
    result=asyncio.run(ex.resolve(journal.by_request('expired-buy')))
    assert result['reason']=='TRANSACTION_EXPIRED_UNSEEN' and result['fee_lamports']==0
    assert journal.by_request('expired-buy').state=='FAILED_TERMINAL'
    calls=len(ex.rpc.calls)
    assert asyncio.run(ex.resolve(journal.by_request('expired-buy')))['status']=='FAILED'
    assert len(ex.rpc.calls)==calls and journal.by_request('expired-buy').attempts==1


def test_resolver_keeps_ambiguous_order_and_throttles_extra_reads(monkeypatch):
    from memecoin_bot.v12_prearmed_io import LiveExecution
    e=entry();e.request_id='pending';e.idempotency_key='key';e.state='SUBMITTED'
    ex=LiveExecution.__new__(LiveExecution);ex.rpc=Rpc('status')
    async def missing(*args,**kwargs):return None
    ex.confirmed_transaction=missing
    monkeypatch.setattr('memecoin_bot.v12_prearmed_io.time.time_ns',lambda:181_000_000_001)
    assert asyncio.run(ex.resolve(e))['status']=='UNKNOWN'
    calls=len(ex.rpc.calls)
    assert asyncio.run(ex.resolve(e))['status']=='UNKNOWN'
    assert len(ex.rpc.calls)==calls

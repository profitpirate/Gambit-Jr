import asyncio,base64
from types import SimpleNamespace
import pytest
from solders.keypair import Keypair
from solders.hash import Hash
from solders.transaction import VersionedTransaction
from memecoin_bot.e4_live import Signer
from memecoin_bot.v12_execution_journal import ExecutionJournal
from memecoin_bot.v12_prearmed_wallet import transfer,assert_flat


@pytest.mark.parametrize('stop,balance,sim_error,expected',[
    (False,100_000_000,None,'COMPLETE'),(True,100_000_000,None,'OPERATOR_STOP_REQUESTED'),
    (False,30_000_000,None,'TRANSFER_WOULD_VIOLATE_FEE_CAP_OR_RESERVE'),
    (False,100_000_000,{'InstructionError':[0,'custom']},'TRANSFER_SIMULATION_FAILED')])
def test_transfer_signs_only_exact_confirmed_intent_and_keeps_reserve(tmp_path,stop,balance,sim_error,expected):
    async def scenario():
        wallet=Keypair();destination=Keypair().pubkey();sent=[]
        signer=Signer.__new__(Signer);signer.keypair=wallet;signer.wallet=str(wallet.pubkey());signer.command=()
        journal=ExecutionJournal(tmp_path/'live.sqlite')
        async def call(method,params,**kw):
            if method=='getLatestBlockhash':return {'value':{'blockhash':str(Hash.default())}}
            if method=='getFeeForMessage':return {'value':5000}
            if method=='simulateTransaction':return {'value':{'err':sim_error}}
            if method=='sendTransaction':
                tx=VersionedTransaction.from_bytes(base64.b64decode(params[0]));tx.verify_and_hash_message()
                assert tx.message.account_keys[0]==wallet.pubkey()
                assert destination in tx.message.account_keys
                instructions=tx.message.instructions
                assert len(instructions)==1 and bytes(instructions[0].data)[4:]==(1_000_000).to_bytes(8,'little')
                sent.append(params[0]);return str(tx.signatures[0])
            pytest.fail('Unexpected RPC '+method)
        async def get_balance(_):return balance
        async def confirmed(signature):
            return {'slot':99,'transaction':{'message':{'accountKeys':[str(wallet.pubkey()),str(destination)]}},
                'meta':{'err':None,'fee':5000,'preBalances':[balance,0],'postBalances':[balance-1_005_000,1_000_000]}}
        execution=SimpleNamespace(wallet=str(wallet.pubkey()),signer=signer,journal=journal,
            rpc=SimpleNamespace(call=call,balance_lamports=get_balance,urls=['primary','backup']),confirmed_transaction=confirmed)
        state=SimpleNamespace(data={'positions':{},'pending':{}})
        kill=tmp_path/'STOP'
        if stop:kill.touch()
        operation={'id':'a'*32,'payload':{'destination':str(destination),'amount_lamports':1_000_000}}
        try:
            if expected=='COMPLETE':
                result=await transfer(operation,execution,state,kill)
                assert result['state']=='COMPLETE' and len(sent)==2 and sent[0]==sent[1]
                assert journal.by_request(operation['id']).state=='CONFIRMED'
                # Repeated delivery checks the receipt; it never broadcasts again.
                assert (await transfer(operation,execution,state,kill))['state']=='COMPLETE'
                assert len(sent)==2
            else:
                with pytest.raises(RuntimeError,match=expected):await transfer(operation,execution,state,kill)
                assert not sent and journal.by_request(operation['id']) is None
        finally:journal.close()
    asyncio.run(scenario())


def test_unresolved_signed_transfer_blocks_another_operation(tmp_path):
    journal=ExecutionJournal(tmp_path/'live.sqlite')
    entry=journal.prepare('old',{'side':'TRANSFER','destination':'wallet','amount_lamports':1})
    journal.mark_signed(entry.idempotency_key,signed_tx_b64='same-bytes',signature='signature')
    with pytest.raises(RuntimeError,match='UNRESOLVED'):
        assert_flat(SimpleNamespace(data={'positions':{},'pending':{}}),journal)
    journal.close()


@pytest.mark.parametrize('buy_ok,sell_ok,residual,expected',[
    (True,True,0,'COMPLETE'),(False,True,0,'FAILED'),
    (True,False,1000,'UNKNOWN'),(True,True,1,'FAILED')])
def test_canary_preserves_recovery_state_and_requires_both_receipts(tmp_path,monkeypatch,buy_ok,sell_ok,residual,expected):
    import json,time
    from pathlib import Path
    from memecoin_bot import v12_prearmed_wallet as wallet_module
    from memecoin_bot.v12_prearmed_runtime import State,Runtime
    async def scenario():
        model=json.loads((Path(__file__).resolve().parents[1]/'research/v12-pre-armed-100-trade-frozen-model.json').read_text())
        state=State(tmp_path/'live.sqlite',wallet='wallet',mode='live')
        journal=ExecutionJournal(state.conn);sent=[]
        class Execution:
            wallet='wallet'
            async def execute(self,request_id,payload):
                sent.append(payload.copy())
                entry=journal.prepare(request_id,payload)
                okay=buy_ok if payload['side']=='BUY' else sell_ok
                if not okay:
                    journal.mark_failed(entry.idempotency_key,'SIMULATION_FAILED',terminal=True)
                    return {'status':'FAILED','reason':'SIMULATION_FAILED'}
                journal.mark_signed(entry.idempotency_key,signed_tx_b64='test-only',signature=payload['side'])
                journal.mark_confirmed(entry.idempotency_key,route='test',slot=1)
                return {'status':'CONFIRMED','tokens_raw':1000,'sol_delta_lamports':-4_000_000 if payload['side']=='BUY' else -5000,
                        'reference_price':0.000000032,'signature':payload['side']}
            async def confirmed_transaction(self,signature):
                cost=4_000_000 if signature=='BUY' else 5000
                return {'slot':1,'meta':{'err':None,'fee':5000,'preBalances':[100_000_000],'postBalances':[100_000_000-cost]}}
        execution=Execution();execution.journal=journal
        async def rpc_call(method,params):
            assert method=='getTokenAccountsByOwner'
            return {'value':[{'account':{'data':{'parsed':{'info':{'tokenAmount':{'amount':str(residual)}}}}}}]}
        rpc=SimpleNamespace(urls=['test'],call=rpc_call)
        async def quote(payload):return {'quoted_tokens_raw':'1000','curve_price':0.000000032}
        builder=SimpleNamespace(request=quote);execution.rpc=rpc;execution.builder=builder
        async def feed(session,urls,observe,health,stop,diagnose):
            await observe('create',[{'anchor_event':'CreateEvent','mint':'token','creator':'creator','is_mayhem_mode':False,
                'virtual_sol_reserves':32_000_000_000,'virtual_token_reserves':1_000_000_000_000_000}],time.time_ns())
            await stop.wait()
        monkeypatch.setattr(wallet_module,'native_feed',feed)
        runtime=Runtime(model=model,state=state,rpc=rpc,builder=builder,execution=execution,wallet='wallet',
            metadata_session=None,emit=lambda *args:None,kill_path=tmp_path/'STOP')
        operation={'id':'b'*32,'payload':{'maximum_total_lamports':10_000_000}}
        try:
            result=await wallet_module.live_test(operation,execution,runtime,None,tmp_path/'STOP')
            assert result['state']==expected
            assert sum(p['side']=='BUY' for p in sent)==1
            assert sent[0]['budget_lamports']=='7005000'
            assert state.data['closed']==0  # A forced test is never a strategy result.
            if expected=='COMPLETE':
                assert result['evidence']['residual_tokens_raw']==0
                assert result['evidence']['buy_signature'] and result['evidence']['sell_signature']
                assert state.data['test_closed']==1 and not state.data['positions']
                with pytest.raises(RuntimeError,match='ALREADY_CLAIMED'):
                    await wallet_module.live_test(operation,execution,runtime,None,tmp_path/'STOP')
            elif buy_ok and not sell_ok:
                assert state.data['positions']['token']['forced_exit'] is True
                assert state.data['positions']['token']['remaining']==1000
        finally:state.conn.close()
    asyncio.run(scenario())

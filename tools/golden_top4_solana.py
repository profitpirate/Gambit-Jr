"""Authorized direct-Solana forward-paper collector with independent E4 observation.

The Axiom-only adapter is retained separately, not silently certified or reused.
"""
from __future__ import annotations
import argparse,asyncio,copy,hashlib,json,os,time
from pathlib import Path
import aiohttp
import golden_horizon_live as collector
from golden_top4_operations import CampaignEngine, OriginalEngine, ARMS, VERSION, write_json
from golden_top4_e4 import Observer, WALLET

SOURCE='DIRECT_SOLANA_NEW_CREATIONS'

async def verify_creation(rpc,decoder,decision,session_started):
    sig=decision['create_signature'];tx=None
    for _ in range(8):
        await asyncio.sleep(2)
        tx=await rpc.call('getTransaction',[sig,{'encoding':'json','commitment':'confirmed','maxSupportedTransactionVersion':1}])
        if tx: break
    if not tx or (tx.get('meta') or {}).get('err') is not None:
        raise RuntimeError('unconfirmed launch: '+sig)
    events=decoder.anchor_events_from_logs(tx['meta'].get('logMessages') or [],decoder.PUMP_PROGRAM_ID)
    if not any(e.get('anchor_event')=='CreateEvent' and e.get('mint')==decision['mint'] for e in events) or tx.get('blockTime') is None or tx['blockTime']<session_started//1_000_000_000-3:
        raise RuntimeError('nonfresh or mismatched creation: '+decision['mint'])
    return {'verified':True,'mint':decision['mint'],'signature':sig,'slot':tx['slot'],
        'block_time':tx['blockTime'],'verified_at_ns':time.time_ns(),'confirmation':'confirmed',
        'creation_transaction_actual_fee_sol':tx['meta']['fee']/1e9,
        'fee_attribution':'external creator transaction, NOT a Gambit fee'}


def verify_manifest(path):
    m=json.loads(path.read_text())
    if m['models']!=list(ARMS) or m['execution']!='PAPER_ONLY' or m['target_per_model']!=100 or m['market_source']!=SOURCE:
        raise ValueError('unregistered campaign or source')
    for name,digest in m['implementation_sha256'].items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest()!=digest: raise ValueError('changed source: '+name)
    frozen=json.loads(Path('docs/research/golden-full3s-v2-freeze.json').read_text())
    for name,digest in frozen['source_sha256'].items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest()!=digest: raise ValueError('changed frozen v2: '+name)
    return m

async def run(args):
    m=verify_manifest(args.manifest);args.output.mkdir(parents=True,exist_ok=True)
    if args.resume:
        previous=json.loads(args.resume.read_text())
        if previous.get('top4_campaign')!=m['campaign_name']: raise ValueError('different campaign state')
    for cls in (OriginalEngine,CampaignEngine):
        cls.output=args.output;cls.smoke=args.smoke;cls.require_reporter=not args.smoke
    # Infrastructure-only collector predicate changes: stop requests and 5s health
    # checkpoints. Selection/sizing/timers/accounting are not rewritten.
    src=Path(collector.__file__).read_text()
    old='time.monotonic() >= deadline or engine.matched_closed() >= args.target'
    assert src.count(old)==1
    src=src.replace(old,old+" or (out / 'stop-request').exists()")
    src=src.replace('last_save >= 30','last_save >= 5')
    exec(compile(src,collector.__file__,'exec'),collector.__dict__)
    collector.Engine=CampaignEngine;collector.ARMS=ARMS;collector.VERSION=VERSION
    collector.verify_creation=verify_creation;collector.READ_METHODS.add('getSignaturesForAddress')
    old_journal=collector.Journal;old_write=collector.write_json
    observer=None;task=None;stop=asyncio.Event()
    ej=old_journal(args.output/'e4-rpc-observations.jsonl')
    decoder=collector.load_module('top4_decoder',args.production_root/'src/memecoin_bot/realtime/pumpfun.py')
    prior=json.loads(args.e4_resume.read_text()) if args.e4_resume and args.e4_resume.exists() else None
    async with aiohttp.ClientSession() as session:
        rpc=collector.Rpc(session,ej)
        class SessionJournal(old_journal):
            def add(self,kind,data,durable=False):
                nonlocal observer,task
                super().add(kind,data,durable)
                if kind=='SESSION' and observer is None:
                    observer=Observer(rpc,decoder,args.output,int(data['id'].rsplit(':',1)[1]),prior)
                    task=asyncio.create_task(observer.run(stop))
        def publish_local(path,data):
            if path.name in ('status.json','final.json') and isinstance(data,dict):
                e=OriginalEngine.last_instance
                if e is not None:
                    data=dict(data)
                    data.update(models=list(ARMS),collector_active=data.get('status') in ('RUNNING','DRAINING'),min_verified_closed_per_model=e.matched_closed(),
                        verified_forward_counts=e.forward_counts(),
                        count_semantics='minimum verified per-model count; v2 selections may differ',
                        collector_scope=SOURCE,axiom_listing_verified=None,
                        local_four_model_tick_ms=collector.quantiles(e.local_ticks),
                        local_four_model_selection_ms=collector.quantiles(e.local_selection),
                        total_report_events=len(e.events),per_action_reporting_file='activity.json',
                        operator_stop_requested=(args.output/'stop-request').exists(),
                        live_testing_started=not args.smoke,
                        e4_monitor={'wallet':WALLET,'checked_ns':observer.checked_ns if observer else None,
                                    'coverage_complete':observer.complete_pages if observer else False,'used_in_selection':False})
            if path.name=='state.json':
                data=dict(data);data['top4_campaign']=m['campaign_name'];data['market_source']=SOURCE
            old_write(path,data)
        collector.Journal=SessionJournal;collector.write_json=publish_local
        try:
            rc=await collector.run(args)
        finally:
            ending=time.time_ns();stop.set()
            if task:
                try:
                    await asyncio.wait_for(task,timeout=30)
                    # The last request is bounded to this market observation window.
                    await asyncio.wait_for(observer.poll(ending),timeout=90)
                except Exception as exc:
                    task.cancel();await asyncio.gather(task,return_exceptions=True)
                    observer.errors.append({'error':'FINAL_DRAIN:'+type(exc).__name__});observer.complete_pages=False
                observer.ended_ns=ending
                final=observer.save();final['market_session_end_ns']=ending
                write_json(args.output/'e4-status.json',final)
            ej.close();collector.Journal=old_journal;collector.write_json=old_write
    if args.smoke:
        if not observer or not observer.checked_ns or not observer.complete_pages:
            raise RuntimeError('E4 preflight could not establish read-only observation')
        write_json(args.output/'readiness.json',{'status':'SOLANA_AND_E4_PREFLIGHT_PASS',
            'source':SOURCE,'live_testing_started':False,'smoke_trades':0,'synthetic_data':False,
            'axiom_feed_verified':False,'e4_checked_ns':observer.checked_ns,
            'market_smoke':json.loads((args.output/'smoke-pass.json').read_text())})
    return rc

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--production-root',type=Path,required=True);p.add_argument('--corpus',type=Path,required=True)
    p.add_argument('--manifest',type=Path,default=Path('config/golden-top4-live.json'))
    p.add_argument('--output',type=Path,required=True);p.add_argument('--resume',type=Path);p.add_argument('--e4-resume',type=Path)
    p.add_argument('--window',default='1');p.add_argument('--duration',type=int,default=14400)
    p.add_argument('--target',type=int,default=100);p.add_argument('--smoke',action='store_true')
    a=p.parse_args()
    if a.target!=100:p.error('100 verified closed trades per model is the frozen minimum')
    return asyncio.run(run(a))
if __name__=='__main__':raise SystemExit(main())

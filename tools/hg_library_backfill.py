#!/usr/bin/env python3
"""Build HG (homegrown) Gambit Jr trade/rejection library from retained live-test evidence.

The extractor is deliberately simple: it does not score or trade. It scans retained
Golden research JSON, records coin-level winners/losses/rejections, joins launch/dev
metadata from the frozen corpus, and merges HG developer history beside E4 history.
"""
from __future__ import annotations
import argparse,gzip,hashlib,json,math,os,time
from collections import defaultdict
from pathlib import Path

SCHEMA='gambit-library-hg-v1'
KNOWN_MODELS={'FULL_2S_CONTROL','PARTIALS_2S','V2_CURRENT','FLOW_V2_1','FULL_3S','FULL_4S','FULL_7S','FULL_3S_V2','GOLDEN_V2','GOLDEN_V2_1','FULL_3S_V2_LIB'}

def finite(x):return isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x)
def atomic(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(obj,indent=2,sort_keys=True,allow_nan=False)+'\n');os.replace(tmp,path)
def load_corpus(path):
    out={}
    with gzip.open(path,'rt',encoding='utf-8') as f:
        for line in f:
            r=json.loads(line);m=r.get('mint')
            if m:out[m]={k:r.get(k) for k in ('creator','name','symbol','create_ns','create_signature','max_multiple_300000ms','max_multiple_60000ms','max_multiple_30000ms','max_multiple_10000ms')}
    return out
def model_from_path(path):
    for x in reversed(path):
        if x in KNOWN_MODELS:return x
    return None
def record_id(payload):return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()[:24]
def classify_trade(d,path,file,meta):
    mint=d.get('mint');pnl=d.get('net_pnl_sol',d.get('pnl_sol'))
    if not mint or not finite(pnl):return None
    model=model_from_path(path);outcome='WIN' if pnl>0 else 'LOSS' if pnl<0 else 'BREAKEVEN';md=meta.get(mint,{})
    row={'source':'HG','dev_source_label':'HG','source_file':file.name,'model':model,'mint':mint,'pnl_sol':float(pnl),'outcome':outcome,'tier':d.get('tier'),'stake_sol':d.get('stake_sol',d.get('budget')),'exit_reason':d.get('exit_reason'),'hold_ms':d.get('hold_ms'),'post_exit_went_higher':d.get('post_exit_went_higher'),'post_exit_went_lower':d.get('post_exit_went_lower'),'post_exit_max_return_fraction':d.get('post_exit_max_return_fraction'),'creator':md.get('creator'),'name':md.get('name'),'symbol':md.get('symbol'),'create_ns':md.get('create_ns'),'create_signature':md.get('create_signature')}
    row['id']='HG:'+record_id({'f':file.name,'p':path,'m':mint,'model':model,'pnl':float(pnl),'signal':d.get('signal_id')});return row
def classify_rejection(d,path,file,meta):
    mint=d.get('mint');reason=d.get('reason') or d.get('abort_reason') or ('NOT_QUALIFIED' if d.get('qualifies') is False else None)
    if not mint or not reason:return None
    md=meta.get(mint,{});mx=md.get('max_multiple_300000ms');runner=finite(mx) and mx>=2.0;failed=finite(mx) and mx<1.05
    row={'source':'HG','dev_source_label':'HG','source_file':file.name,'model':model_from_path(path),'mint':mint,'outcome':'REJECTED','reason':str(reason),'creator':md.get('creator'),'name':md.get('name'),'symbol':md.get('symbol'),'create_ns':md.get('create_ns'),'create_signature':md.get('create_signature'),'max_launch_multiple_5m':mx,'rejected_runner_2x':bool(runner),'rejected_failed_lt_1_05x':bool(failed)}
    row['id']='HG:'+record_id({'f':file.name,'p':path,'m':mint,'reason':str(reason)});return row
def walk(obj,path,file,meta,trades,rejs,seen_t,seen_r):
    if isinstance(obj,dict):
        if obj.get('mint') and (finite(obj.get('net_pnl_sol')) or finite(obj.get('pnl_sol'))):
            r=classify_trade(obj,path,file,meta)
            if r and r['id'] not in seen_t:trades.append(r);seen_t.add(r['id'])
        if obj.get('mint') and (obj.get('reason') or obj.get('abort_reason') or obj.get('qualifies') is False):
            r=classify_rejection(obj,path,file,meta)
            if r and r['id'] not in seen_r:rejs.append(r);seen_r.add(r['id'])
        for k,v in obj.items():walk(v,path+[str(k)],file,meta,trades,rejs,seen_t,seen_r)
    elif isinstance(obj,list):
        for i,v in enumerate(obj):walk(v,path+[str(i)],file,meta,trades,rejs,seen_t,seen_r)
def dev_stats(trades,rejs):
    by=defaultdict(lambda:{'label':'HG','trade_ids':[],'rejection_ids':[],'coin_outcomes':defaultdict(set),'pnl_sol':0.0,'rejected_runner_mints':set(),'rejected_failed_mints':set(),'rejected_mints':set()})
    for r in trades:
        d=r.get('creator')
        if not d:continue
        x=by[d];x['trade_ids'].append(r['id']);x['coin_outcomes'][r['mint']].add(r['outcome']);x['pnl_sol']+=r['pnl_sol']
    for r in rejs:
        d=r.get('creator')
        if not d:continue
        x=by[d];x['rejection_ids'].append(r['id']);x['rejected_mints'].add(r['mint'])
        if r.get('rejected_runner_2x'):x['rejected_runner_mints'].add(r['mint'])
        if r.get('rejected_failed_lt_1_05x'):x['rejected_failed_mints'].add(r['mint'])
    out={}
    for dev,x in by.items():
        wins=losses=mixed=0
        for outcomes in x['coin_outcomes'].values():
            if 'WIN' in outcomes and 'LOSS' in outcomes:mixed+=1
            elif 'WIN' in outcomes:wins+=1
            elif 'LOSS' in outcomes:losses+=1
        n=wins+losses+mixed;wr=wins/n if n else None
        out[dev]={'dev':dev,'label':'HG','unique_traded_coins':n,'winning_coins':wins,'losing_coins':losses,'mixed_model_outcome_coins':mixed,'unique_rejected_coins':len(x['rejected_mints']),'rejected_runner_2x_count':len(x['rejected_runner_mints']),'rejected_failed_count':len(x['rejected_failed_mints']),'record_pnl_sum_sol':x['pnl_sol'],'coin_win_rate_strict':wr,'trade_ids':x['trade_ids'],'rejection_ids':x['rejection_ids'],'golden_bunch_hg':bool(n>=3 and wins>=3 and wr is not None and wr>=.70 and x['pnl_sol']>0)}
    return out
def merge_devs(e4raw,hg):
    e4=e4raw.get('devs',e4raw);out={}
    for dev in set(e4)|set(hg):
        a=e4.get(dev);b=hg.get(dev);out[dev]={'dev':dev,'labels':(['E4'] if a else [])+(['HG'] if b else []),'E4':a,'HG':b,'golden_bunch':bool((a or {}).get('golden_bunch') or (b or {}).get('golden_bunch_hg'))}
    return out
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--corpus',type=Path,required=True);ap.add_argument('--research',type=Path,default=Path('docs/research'));ap.add_argument('--e4-devs',type=Path,required=True);ap.add_argument('--output',type=Path,required=True);a=ap.parse_args()
    meta=load_corpus(a.corpus);trades=[];rejs=[];seen_t=set();seen_r=set();files=[]
    for file in sorted(a.research.glob('golden*.json')):
        if 'library' in file.name or ('tournament' in file.name and 'golden-v21-tournament-final-report' not in file.name):continue
        try:obj=json.loads(file.read_text())
        except Exception:continue
        files.append(file.name);walk(obj,[],file,meta,trades,rejs,seen_t,seen_r)
    hg=dev_stats(trades,rejs);e4=json.loads(a.e4_devs.read_text());master=merge_devs(e4,hg);a.output.mkdir(parents=True,exist_ok=True)
    atomic(a.output/'gambit-library-hg-trades.json',{'schema':SCHEMA,'trades':trades,'rejections':rejs});atomic(a.output/'gambit-library-hg-devs.json',{'schema':SCHEMA,'devs':hg});atomic(a.output/'gambit-library-devs.json',{'schema':'gambit-library-master-devs-v1','devs':master})
    summary={'schema':SCHEMA,'phase':'HG_LIBRARY_PHASE2_COMPLETE','built_at_ns':time.time_ns(),'files_scanned':files,'trade_records':len(trades),'wins':sum(r['outcome']=='WIN' for r in trades),'losses':sum(r['outcome']=='LOSS' for r in trades),'rejections':len(rejs),'rejected_2x_runners':sum(r['rejected_runner_2x'] for r in rejs),'hg_devs':len(hg),'master_devs':len(master),'golden_bunch_devs':sum(v['golden_bunch'] for v in master.values()),'notes':['HG tags mean homegrown Gambit Jr evidence.','Model-specific records are preserved; developer unique-coin stats avoid blindly counting the same coin once per model.','Rejected-runner labels require retained 5-minute launch-path evidence; missing outcomes remain unknown.']}
    atomic(a.output/'gambit-library-master-summary.json',summary);print(json.dumps(summary,sort_keys=True))
if __name__=='__main__':main()

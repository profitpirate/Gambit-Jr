#!/usr/bin/env python3
from __future__ import annotations
import argparse,gzip,hashlib,json,math,statistics,sys
from pathlib import Path
from typing import Any,Sequence
import numpy as np
from sklearn.decomposition import PCA
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import RobustScaler

SHA='6f41376cfee3d54d57774b4368ec8b50c9e59becbbd04650cf56a20ef48dce6c'
CANDS={
 'A_86_96':dict(k=3,policy='hold_1000ms',threshold=0.9923740946678363,validation_trades=10,validation_wr=1.0,validation_pnl=0.09731004880916627,holdout_trades=23,holdout_wr=0.8695652173913043,holdout_pnl=0.14898171110665714),
 'B_75':dict(k=10,policy='hold_2000ms',threshold=0.9838282174643644,validation_trades=20,validation_wr=0.75,validation_pnl=0.42142929954622144),
}

def finite(v,d=0.0):
 try:x=float(v)
 except (TypeError,ValueError):return d
 return x if math.isfinite(x) else d

def integer(v,d=0):
 try:return int(v)
 except (TypeError,ValueError):return d

def digest(path):
 h=hashlib.sha256()
 with path.open('rb') as f:
  for b in iter(lambda:f.read(1<<20),b''):h.update(b)
 return h.hexdigest()

def rank(v): return np.argsort(np.argsort(v,kind='stable'),kind='stable')/max(1,len(v)-1)
def pct(ref,v):
 o=np.sort(ref,kind='stable'); i=np.searchsorted(o,v,side='right')-1
 return np.clip(i/max(1,len(ref)-1),0,1)

def raw_scores(hr,lr,train,pos,k):
 pi=np.flatnonzero(train&pos); ni=np.flatnonzero(train&~pos)
 p=NearestNeighbors(n_neighbors=k,n_jobs=-1).fit(hr[pi]); n=NearestNeighbors(n_neighbors=k,n_jobs=-1).fit(hr[ni])
 def f(x):
  pd=p.kneighbors(x,return_distance=True)[0].mean(1); nd=n.kneighbors(x,return_distance=True)[0].mean(1)
  return np.log1p(nd)-np.log1p(pd)
 return f(hr),f(lr)

def live_matrix(base,hist,live):
 fields=list(base.BASE_NUMERIC)+[f'{s}_0ms' for s in base.WINDOW_STEMS]
 train_rows=[r for r in hist if r.get('split')=='train']; meds={}
 for field in fields:
  vals=[finite(r.get(field),float('nan')) for r in train_rows if r.get(field) is not None]; vals=[x for x in vals if math.isfinite(x)]
  meds[field]=statistics.median(vals) if vals else 0.0
 def make(rows):
  x=np.zeros((len(rows),2*len(fields)),dtype=np.float32)
  for i,r in enumerate(rows):
   j=0
   for f in fields:
    raw=r.get(f); miss=raw is None; x[i,j]=finite(raw,meds[f]) if not miss else meds[f]; x[i,j+1]=float(miss); j+=2
  return x
 return make(live)

def pf(xs:Sequence[float]):
 g=sum(x for x in xs if x>0); l=-sum(x for x in xs if x<0)
 return g/l if l>0 else (999.0 if g>0 else 0.0)

def live_econ(base,rows,scores,idx,policy,start=2.0):
 out={}; hold=integer(policy.removeprefix('hold_').removesuffix('ms'))
 for lat in base.LATENCIES:
  bal=start; peak=bal; dd=0.; active=[]; closed=[]; skipped=0
  for i in sorted(set(map(int,idx)),key=lambda z:(integer(rows[z]['create_ns']),str(rows[z]['mint']))):
   now=integer(rows[i]['create_ns']); active=[x for x in active if x>now]
   if len(active)>=base.MAX_CONCURRENT: skipped+=1; continue
   stake=min(max(0.,bal-base.RESERVE_SOL),bal*base.POSITION_FRACTION)
   if stake<=0:break
   ret=finite(rows[i].get(base.policy_pnl_key(lat,policy)),-base.ENTRY_BUDGET_SOL)/base.ENTRY_BUDGET_SOL
   pnl=stake*ret; bal+=pnl; peak=max(peak,bal); dd=max(dd,(peak-bal)/peak if peak else 0.); active.append(now+(hold+lat)*1_000_000)
   closed.append(dict(mint=rows[i]['mint'],score=float(scores[i]),stake_sol=stake,pnl_sol=pnl,create_ns=now))
  ps=[x['pnl_sol'] for x in closed]; w=sum(x>0 for x in ps)
  out[str(lat)]=dict(trades=len(closed),wins=w,losses=len(closed)-w,win_rate=(w/len(closed) if closed else None),net_pnl_sol=bal-start,ending_bankroll_sol=bal,profit_factor=pf(ps),maximum_drawdown_fraction=dd,skipped_for_concurrency=skipped,closed=closed)
 return out

def summ(blocks):
 vals=list(blocks.values()); traded=[b for b in vals if b['trades']]
 return dict(minimum_trades=min((b['trades'] for b in vals),default=0),minimum_win_rate=min((b['win_rate'] for b in traded),default=None),minimum_net_pnl_sol=min((b['net_pnl_sol'] for b in vals),default=0.),minimum_ending_bankroll_sol=min((b['ending_bankroll_sol'] for b in vals),default=0.),minimum_profit_factor=min((b['profit_factor'] for b in vals),default=0.),maximum_drawdown_fraction=max((b['maximum_drawdown_fraction'] for b in vals),default=0.))

def check(name,c,repro):
 v=repro['validation']
 if v['minimum_trades']!=c['validation_trades'] or abs(v['minimum_win_rate']-c['validation_wr'])>1e-12 or abs(v['minimum_net_pnl_sol']-c['validation_pnl'])>1e-8:raise RuntimeError(f'{name} validation reproduction mismatch {v}')
 if 'holdout_trades' in c:
  h=repro['holdout']
  if h['minimum_trades']!=c['holdout_trades'] or abs(h['minimum_win_rate']-c['holdout_wr'])>1e-12 or abs(h['minimum_net_pnl_sol']-c['holdout_pnl'])>1e-8:raise RuntimeError(f'{name} holdout reproduction mismatch {h}')

def main():
 ap=argparse.ArgumentParser(); ap.add_argument('--research-root',type=Path,required=True); ap.add_argument('--corpus',type=Path,required=True); ap.add_argument('--batch',type=Path); ap.add_argument('--events',type=Path); ap.add_argument('--run-id'); ap.add_argument('--output',type=Path); ap.add_argument('--preflight',action='store_true'); a=ap.parse_args()
 if digest(a.corpus)!=SHA:raise RuntimeError('frozen corpus SHA mismatch')
 sys.path.insert(0,str(a.research_root.resolve()))
 from scripts import e4_v12_allout_launch_corpus as build
 from scripts import e4_v12_allout_launch_corpus_stream as cstream
 from scripts import e4_v12_allout_profit_hazard as base
 from scripts.e4_v12_allout_profit_hazard_stream import load_corpus_stream
 hist=load_corpus_stream(a.corpus,0); train=hist.splits=='train'
 scaler=RobustScaler(quantile_range=(10.,90.)); scaler.fit(hist.x_general[train]); hs=np.clip(scaler.transform(hist.x_general),-15,15).astype(np.float32); pca=PCA(n_components=32,whiten=True,random_state=7331); pca.fit(hs[train]); hr=pca.transform(hs).astype(np.float32)
 if a.preflight:
  checks={}
  for name,c in CANDS.items():
   selected=hist.selected; profit=hist.pnl[c['policy']].min(1)>0
   hi,_=raw_scores(hr,hr[:1],train,selected,c['k']); hp,_=raw_scores(hr,hr[:1],train,profit,c['k'])
   hscore=np.sqrt(np.clip(rank(hi),1e-9,1)*np.clip(rank(hp),1e-9,1))
   vi=np.flatnonzero((hist.splits=='validation')&(hscore>=c['threshold'])); oi=np.flatnonzero((hist.splits=='holdout')&(hscore>=c['threshold']))
   repro={'validation':summ(base.multi_latency_economics(vi,hscore,hist,c['policy'])['latencies']),'holdout':summ(base.multi_latency_economics(oi,hscore,hist,c['policy'])['latencies'])}; check(name,c,repro); checks[name]=repro
  print(json.dumps({'status':'PREFLIGHT_OK','checks':checks},indent=2,sort_keys=True)); return
 if not (a.batch and a.events and a.run_id and a.output): raise SystemExit('--batch --events --run-id --output are required unless --preflight')
 history=cstream.CausalHistory()
 with gzip.open(a.corpus,'rt',encoding='utf-8') as f:
  for line in f:
   if line.strip(): history.enrich([json.loads(line)])
 live=build.parse_run(a.events,a.batch,a.run_id,'live',{})
 for r in live:
  if r.get('observed_e4_first_buy_ns'):
   r['selected_by_e4']=True;r['landed_successfully']=True;r['decision_ns']=r['observed_e4_first_buy_ns']
 history.enrich(live)
 lx=live_matrix(base,hist.rows,live); ls=np.clip(scaler.transform(lx),-15,15).astype(np.float32); lr=pca.transform(ls).astype(np.float32)
 report=dict(version='e4-v12-dual-prototype-live-2sol-v1',frozen_corpus_sha256=SHA,live_run_id=a.run_id,live_launches=len(live),starting_bankroll_sol_per_candidate=2.0,same_live_window=True,no_live_tuning=True,candidates={})
 for name,c in CANDS.items():
  selected=hist.selected; profit=hist.pnl[c['policy']].min(1)>0
  hi,li=raw_scores(hr,lr,train,selected,c['k']); hp,lp=raw_scores(hr,lr,train,profit,c['k'])
  hscore=np.sqrt(np.clip(rank(hi),1e-9,1)*np.clip(rank(hp),1e-9,1)); lscore=np.sqrt(np.clip(pct(hi,li),1e-9,1)*np.clip(pct(hp,lp),1e-9,1))
  vi=np.flatnonzero((hist.splits=='validation')&(hscore>=c['threshold'])); oi=np.flatnonzero((hist.splits=='holdout')&(hscore>=c['threshold']))
  repro={'validation':summ(base.multi_latency_economics(vi,hscore,hist,c['policy'])['latencies']),'holdout':summ(base.multi_latency_economics(oi,hscore,hist,c['policy'])['latencies'])}; check(name,c,repro)
  idx=np.flatnonzero(lscore>=c['threshold']); live_blocks=live_econ(base,live,lscore,idx,c['policy'],2.0)
  report['candidates'][name]=dict(config=c,historical_reproduction=repro,live_signal_count=int(len(idx)),live=live_blocks,live_summary=summ(live_blocks))
 a.output.parent.mkdir(parents=True,exist_ok=True); a.output.write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')
 md=a.output.with_suffix('.md'); lines=['# E4 V12 dual prototype — 2 SOL forward paper test','',f'Live run: `{a.run_id}` — {len(live)} launches','', '| Thesis | Signals | Worst trades | Worst WR | Worst net P&L | Worst ending SOL | Worst PF |','|---|---:|---:|---:|---:|---:|---:|']
 for name,row in report['candidates'].items():
  s=row['live_summary']; wr='N/A' if s['minimum_win_rate'] is None else f"{100*s['minimum_win_rate']:.2f}%"; lines.append(f"| {name} | {row['live_signal_count']} | {s['minimum_trades']} | {wr} | {s['minimum_net_pnl_sol']:+.6f} | {s['minimum_ending_bankroll_sol']:.6f} | {s['minimum_profit_factor']:.3f} |")
 md.write_text('\n'.join(lines)+'\n'); print('\n'.join(lines))
if __name__=='__main__':main()

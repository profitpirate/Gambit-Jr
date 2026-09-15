#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

import golden_management_v2_1 as v21


def f(v: Any, d: float = 0.0) -> float:
    try: x=float(v)
    except (TypeError,ValueError): return d
    return x if math.isfinite(x) else d


def pct(v: Any) -> str:
    return "N/A" if v is None else f"{100*f(v):+.2f}%"


def arm_summary(rows: list[dict[str,Any]], ending: float) -> dict[str,Any]:
    pnls=[f(r.get('pnl_sol')) for r in rows]; wins=sum(x>0 for x in pnls); gains=sum(x for x in pnls if x>0); losses=-sum(x for x in pnls if x<0)
    peak=3.0; dd=0.0
    for r in rows:
        b=f(r.get('balance_after_sol'),peak); peak=max(peak,b); dd=max(dd,(peak-b)/peak if peak else 0)
    reasons=Counter(str(r.get('exit_reason')) for r in rows)
    post_higher=sum(bool((r.get('post_exit') or {}).get('went_higher_than_exit')) for r in rows)
    return {
        'trades':len(rows),'wins':wins,'losses':len(rows)-wins,'win_rate':wins/len(rows) if rows else None,
        'net_pnl_sol':sum(pnls),'ending_balance_sol':ending,'roi_fraction':ending/3.0-1.0,
        'profit_factor':gains/losses if losses else (999.0 if gains else None),'expectancy_sol':sum(pnls)/len(rows) if rows else None,
        'maximum_drawdown_fraction':dd,'avg_return_fraction':sum(f(r.get('return_fraction')) for r in rows)/len(rows) if rows else None,
        'avg_mfe_fraction':sum(f(r.get('mfe_fraction')) for r in rows)/len(rows) if rows else None,
        'avg_mae_fraction':sum(f(r.get('mae_fraction')) for r in rows)/len(rows) if rows else None,
        'post_exit_went_higher_count':post_higher,'exit_reasons':dict(reasons),
        'e4_same_coin_observations':sum(bool(r.get('e4_same_coin_events')) for r in rows),
    }


def feedback(name: str, s: dict[str,Any], rows: list[dict[str,Any]], best_total: float) -> list[str]:
    out=[]
    if s['profit_factor'] is not None and s['profit_factor'] < 1.25: out.append('Profit factor below 1.25: loss containment/entry sizing still needs work.')
    if s['maximum_drawdown_fraction'] > .20: out.append('Maximum drawdown exceeded 20%: reduce misclassified high-conviction exposure or cut failed continuation sooner.')
    gap=best_total-s['net_pnl_sol']
    if gap > .25: out.append(f'Left {gap:.4f} SOL versus the best tournament arm on the identical 100-signal cohort; inspect trade-level counterfactuals.')
    givebacks=sum(1 for r in rows if f(r.get('mfe_fraction'))>.15 and f(r.get('return_fraction'))<.03)
    if givebacks: out.append(f'{givebacks} trades reached >15% MFE but finished below +3%; winner-protection remains an improvement target.')
    bad_losses=sum(1 for r in rows if f(r.get('return_fraction'))<=-.15)
    if bad_losses: out.append(f'{bad_losses} trades lost 15%+ on position return; inspect cluster/flow classification and earlier invalidation.')
    if s['post_exit_went_higher_count'] > len(rows)*.6: out.append('Most coins went higher after final exit; exits may be too defensive or runner retention too small.')
    if name == 'FLOW_V2_1':
        cluster_downgrades=sum(1 for r in rows if int(((r.get('conviction') or {}).get('cluster_profile') or {}).get('redundant_wallets') or 0)>0)
        out.append(f'Cluster adjustment affected {cluster_downgrades} FLOW_V2_1 trades; compare their P&L against V2_CURRENT to validate the penalty.')
    return out or ['No automatic red flag; inspect capture efficiency, tail losses and per-trade best-arm gaps before changing the policy.']


def main()->int:
    p=argparse.ArgumentParser(); p.add_argument('--state',type=Path,required=True); p.add_argument('--minimum-trades',type=int,default=100); p.add_argument('--output-md',type=Path,required=True); p.add_argument('--output-json',type=Path,required=True); a=p.parse_args()
    state=json.loads(a.state.read_text()); arms=state.get('arms') or {}; rows_by_arm={}
    for name in v21.TOURNAMENT_ARMS:
        rows=[r for r in (arms.get(name) or {}).get('ledger',[]) if r.get('status')=='CLOSED']
        if len(rows)<a.minimum_trades: raise SystemExit(f'{name}: need {a.minimum_trades}, have {len(rows)}')
        rows_by_arm[name]=rows[:a.minimum_trades]
    # Alignment is a hard reporting invariant.
    cohort=[]
    for i in range(a.minimum_trades):
        mints={rows_by_arm[n][i].get('mint') for n in v21.TOURNAMENT_ARMS}
        if len(mints)!=1: raise SystemExit(f'cohort misalignment at {i}: {mints}')
        mint=next(iter(mints)); per={n:rows_by_arm[n][i] for n in v21.TOURNAMENT_ARMS}; best_name=max(per,key=lambda n:f(per[n].get('pnl_sol'))); best_pnl=f(per[best_name].get('pnl_sol'))
        trade={'index':i+1,'mint':mint,'best_arm':best_name,'best_pnl_sol':best_pnl,'models':{}}
        for n,r in per.items():
            pnl=f(r.get('pnl_sol')); stake=f(r.get('stake_sol')); mfe=f(r.get('mfe_fraction')); post=r.get('post_exit') or {}; hindsight=max(0.0,stake*mfe) if mfe>0 else 0.0
            trade['models'][n]={
                'result':'WIN' if pnl>0 else 'LOSS','pnl_sol':pnl,'return_fraction':r.get('return_fraction'),'stake_sol':stake,
                'tier':r.get('conviction_tier'),'conviction_score':r.get('conviction_score'),'mfe_fraction':r.get('mfe_fraction'),'mae_fraction':r.get('mae_fraction'),
                'exit_reason':r.get('exit_reason'),'hold_ms':r.get('actual_hold_ms'),'actions':r.get('actions') or [],
                'could_have_made_more_vs_best_arm_sol':max(0.0,best_pnl-pnl),
                'hindsight_mfe_upper_pnl_sol':hindsight,
                'hindsight_mfe_capture_fraction':(pnl/hindsight if hindsight>1e-12 else None),
                'could_have_lost_less_vs_best_arm_sol':max(0.0,best_pnl-pnl) if pnl<0 else 0.0,
                'post_exit_went_higher':bool(post.get('went_higher_than_exit')),'post_exit_went_lower':bool(post.get('went_lower_than_exit')),
                'post_exit_max_return_fraction':post.get('max_return_fraction'),'post_exit_min_return_fraction':post.get('min_return_fraction'),
                'e4_same_coin_events':r.get('e4_same_coin_events') or [],
            }
        cohort.append(trade)
    summaries={name:arm_summary(rows_by_arm[name],f((arms.get(name) or {}).get('cash_sol'))) for name in v21.TOURNAMENT_ARMS}
    best_total=max(s['net_pnl_sol'] for s in summaries.values())
    for name in summaries: summaries[name]['suggested_feedback']=feedback(name,summaries[name],rows_by_arm[name],best_total)
    ranking=sorted(v21.TOURNAMENT_ARMS,key=lambda n:summaries[n]['net_pnl_sol'],reverse=True)
    payload={'version':'golden-management-v2.1-tournament-report-v1','paper_only':True,'fresh_live_market_only':True,'starting_balance_sol_per_arm':3.0,'cohort_trades':a.minimum_trades,'ranking':ranking,'summaries':summaries,'trades':cohort,'e4_note':'E4 is observational only; same-coin events are reported when naturally observed and never affect Golden decisions.'}
    a.output_json.parent.mkdir(parents=True,exist_ok=True); a.output_json.write_text(json.dumps(payload,indent=2,sort_keys=True,allow_nan=False)+'\n')
    out=['# Golden v2.1 — four-model 100-trade true-online tournament','',f'Identical fresh-live cohort: **{a.minimum_trades} qualifying coins**. Each paper arm started with **3 SOL**. No transaction was broadcast.','', '## Ranking','', '| Rank | Model | W/L | WR | Net SOL | Ending SOL | PF | Max DD |','|---:|---|---:|---:|---:|---:|---:|---:|']
    for i,n in enumerate(ranking,1):
        s=summaries[n]; out.append(f"| {i} | {n} | {s['wins']}/{s['losses']} | {100*s['win_rate']:.2f}% | {s['net_pnl_sol']:+.6f} | {s['ending_balance_sol']:.6f} | {s['profit_factor']} | {100*s['maximum_drawdown_fraction']:.2f}% |")
    out += ['', '## Model feedback','']
    for n in ranking:
        out.append(f'### {n}')
        for x in summaries[n]['suggested_feedback']: out.append(f'- {x}')
        out.append('')
    out += ['## Every trade','']
    for t in cohort:
        out += [f"### Trade {t['index']} — `{t['mint']}`",'',f"Best arm on this coin: **{t['best_arm']}** ({t['best_pnl_sol']:+.6f} SOL)",'']
        for n in v21.TOURNAMENT_ARMS:
            d=t['models'][n]
            out += [f"**{n}** — {d['result']} {d['pnl_sol']:+.6f} SOL ({pct(d['return_fraction'])}); tier {d['tier']}; MFE {pct(d['mfe_fraction'])}; MAE {pct(d['mae_fraction'])}; exit `{d['exit_reason']}`; hold {f(d['hold_ms']):.1f}ms.",
                    f"Could have made more vs best arm: {d['could_have_made_more_vs_best_arm_sol']:.6f} SOL. Could have lost less vs best arm: {d['could_have_lost_less_vs_best_arm_sol']:.6f} SOL. Hindsight MFE upper bound: {d['hindsight_mfe_upper_pnl_sol']:.6f} SOL. Post-exit higher/lower: {d['post_exit_went_higher']}/{d['post_exit_went_lower']}."]
        out.append('')
    a.output_md.write_text('\n'.join(out)+'\n'); return 0

if __name__=='__main__': raise SystemExit(main())

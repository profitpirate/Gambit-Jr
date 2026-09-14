#!/usr/bin/env python3
"""Generate the final Golden Management v2 50+ trade report."""
from __future__ import annotations
import argparse, json, math
from collections import Counter
from pathlib import Path
from typing import Any


def f(v: Any, default: float = 0.0) -> float:
    try:
        x=float(v)
    except (TypeError,ValueError):
        return default
    return x if math.isfinite(x) else default


def pct(v: Any) -> str:
    if v is None: return "N/A"
    return f"{100*f(v):+.2f}%"


def main() -> int:
    p=argparse.ArgumentParser()
    p.add_argument('--state',type=Path,required=True)
    p.add_argument('--output-md',type=Path,required=True)
    p.add_argument('--output-json',type=Path,required=True)
    p.add_argument('--minimum-trades',type=int,default=50)
    a=p.parse_args()
    state=json.loads(a.state.read_text())
    account=state.get('account') or {}
    rows=[r for r in account.get('ledger',[]) if r.get('status')=='CLOSED']
    if len(rows)<a.minimum_trades:
        raise SystemExit(f"need {a.minimum_trades} closed trades, have {len(rows)}")
    pnls=[f(r.get('pnl_sol')) for r in rows]
    wins=sum(x>0 for x in pnls); gains=sum(x for x in pnls if x>0); losses=-sum(x for x in pnls if x<0)
    tiers=Counter(str(r.get('conviction_tier')) for r in rows)
    reasons=Counter(str(r.get('exit_reason')) for r in rows)
    action_reasons=Counter(str(x.get('reason')) for r in rows for x in (r.get('actions') or []))
    post_complete=sum(bool((r.get('post_exit') or {}).get('complete')) for r in rows)
    higher=sum(bool((r.get('post_exit') or {}).get('went_higher_than_exit')) for r in rows)
    lower=sum(bool((r.get('post_exit') or {}).get('went_lower_than_exit')) for r in rows)
    control=[f((r.get('control_v1_2s') or {}).get('pnl_sol')) for r in rows if r.get('control_v1_2s')]
    summary={
      'trades':len(rows),'wins':wins,'losses':len(rows)-wins,'win_rate':wins/len(rows),
      'net_pnl_sol':sum(pnls),'ending_cash_sol':f(account.get('cash_sol')),
      'profit_factor':gains/losses if losses else (999.0 if gains else None),
      'tier_counts':dict(tiers),'exit_reasons':dict(reasons),'action_reasons':dict(action_reasons),
      'post_exit_complete':post_complete,'went_higher_than_exit':higher,'went_lower_than_exit':lower,
      'v1_control_net_pnl_sol':sum(control) if control else None,
      'management_vs_v1_control_delta_sol':sum(pnls)-sum(control) if control else None,
    }
    feedback=[]
    if summary['profit_factor'] is not None and summary['profit_factor']<1.25:
        feedback.append('Profit factor remains weak; review loss invalidation and conviction calibration before any real-money consideration.')
    if higher>len(rows)*0.6:
        feedback.append('A majority of coins traded higher after final exit; runner exits may be too aggressive. Inspect post-exit MFE by tier and exit reason.')
    if lower>len(rows)*0.8:
        feedback.append('Most coins traded lower after final exit; final exits are generally protective, but check whether earlier partials surrender too much upside.')
    by_tier={}
    for tier in sorted(tiers):
        rr=[r for r in rows if str(r.get('conviction_tier'))==tier]
        pp=[f(r.get('pnl_sol')) for r in rr]
        gg=sum(x for x in pp if x>0); ll=-sum(x for x in pp if x<0)
        by_tier[tier]={
          'trades':len(rr),'wins':sum(x>0 for x in pp),'win_rate':sum(x>0 for x in pp)/len(rr),
          'net_pnl_sol':sum(pp),'profit_factor':gg/ll if ll else (999.0 if gg else None),
          'avg_return_fraction':sum(f(r.get('return_fraction')) for r in rr)/len(rr),
          'avg_mfe_fraction':sum(f(r.get('mfe_fraction')) for r in rr)/len(rr),
          'avg_mae_fraction':sum(f(r.get('mae_fraction')) for r in rr)/len(rr),
        }
    summary['by_tier']=by_tier; summary['suggested_feedback']=feedback
    payload={'version':'golden-management-v2-final-report-v1','summary':summary,'trades':rows}
    a.output_json.parent.mkdir(parents=True,exist_ok=True)
    a.output_json.write_text(json.dumps(payload,indent=2,sort_keys=True,allow_nan=False)+'\n')

    out=[]
    out += ['# Golden Management v2 — 3 SOL live-market paper campaign','',
      f"Closed trades: **{len(rows)}** | W/L: **{wins}/{len(rows)-wins}** | WR: **{100*wins/len(rows):.2f}%**",
      f"Net P&L: **{sum(pnls):+.6f} SOL** | Ending cash: **{f(account.get('cash_sol')):.6f} SOL** | PF: **{summary['profit_factor']}**",'',
      'Market-data mode: **live Solana websocket / freshly observed launches only**. Execution: **paper only; no transactions broadcast**.','',
      '## Tier performance','', '| Tier | Trades | WR | Net SOL | PF | Avg return | Avg MFE | Avg MAE |','|---|---:|---:|---:|---:|---:|---:|---:|']
    for tier,d in by_tier.items():
        out.append(f"| {tier} | {d['trades']} | {100*d['win_rate']:.2f}% | {d['net_pnl_sol']:+.6f} | {d['profit_factor']} | {pct(d['avg_return_fraction'])} | {pct(d['avg_mfe_fraction'])} | {pct(d['avg_mae_fraction'])} |")
    out += ['', '## Every trade','']
    for i,r in enumerate(rows,1):
        er=r.get('entry_reason') or {}; post=r.get('post_exit') or {}; actions=r.get('actions') or []
        out += [f"### Trade {i} — `{r.get('mint')}`",'',
          f"- Result: **{'WIN' if r.get('win') else 'LOSS'}**, {f(r.get('pnl_sol')):+.6f} SOL ({pct(r.get('return_fraction'))})",
          f"- Tier / size: **{r.get('conviction_tier')} / {100*f(r.get('position_fraction')):.1f}%** of account equity; stake {f(r.get('stake_sol')):.6f} SOL",
          f"- Entry reasoning: prior appearances **{er.get('prior_appearances')}**, prior wins **{er.get('prior_wins')}**, prior WR **{pct(er.get('prior_win_rate'))}**, early buyers **{er.get('buyer_count')}**, conviction **{r.get('conviction_score')}**",
          f"- Timing: CREATE→decision **{f(r.get('create_to_decision_ms')):.3f} ms**, decision→paper entry **{f(r.get('decision_to_entry_ms')):.3f} ms**, total CREATE→entry **{f(r.get('create_to_entry_ms')):.3f} ms**",
          f"- Path: MFE **{pct(r.get('mfe_fraction'))}**, MAE **{pct(r.get('mae_fraction'))}**, hold **{f(r.get('actual_hold_ms')):.1f} ms**",
          f"- Final exit: **{r.get('exit_reason')}**",
          f"- Old v1 2s control: {f((r.get('control_v1_2s') or {}).get('pnl_sol')):+.6f} SOL" if r.get('control_v1_2s') else '- Old v1 2s control: unavailable',
          f"- After exit: went higher **{bool(post.get('went_higher_than_exit'))}**, went lower **{bool(post.get('went_lower_than_exit'))}**, post-window max **{pct(post.get('max_return_fraction'))}**, min **{pct(post.get('min_return_fraction'))}",
          '- Partials / actions:']
        if actions:
            for x in actions:
                out.append(f"  - {x.get('kind')} @ {f(x.get('t_ms')):.1f} ms — {100*f(x.get('fraction_of_entry')):.2f}% of entry, reason `{x.get('reason')}`, mark {pct(x.get('mark_return'))}, proceeds {f(x.get('proceeds_sol')):.6f} SOL")
        else: out.append('  - none')
        out.append('')
    out += ['## Suggested feedback','']
    if feedback:
        out += [f"- {x}" for x in feedback]
    else:
        out.append('- No automatic red flags. Inspect tier calibration, management-vs-v1 delta, MFE capture, MAE containment, and post-exit opportunity cost before changing the policy.')
    a.output_md.write_text('\n'.join(out)+'\n')
    return 0

if __name__=='__main__': raise SystemExit(main())

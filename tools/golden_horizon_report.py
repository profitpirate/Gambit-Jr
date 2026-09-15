#!/usr/bin/env python3
"""Phase-2 evidence gate and Phase-3 report. No outcome generation or replay."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from golden_horizon_engine import ARMS, Config, Engine, HORIZONS, VERSION, quantiles
from golden_horizon_live import EXPECTED_BLOBS, DECODER_BLOB, write_json


def gate(state: dict[str, Any], minimum: int = 100) -> Engine:
    if minimum < 100: raise ValueError("completion requires at least 100, never zero or smoke trades")
    data = state["engine"]
    if data.get("paper_only") is not True or data.get("real_execution") is not False:
        raise ValueError("unrecognised execution provenance")
    if data.get("actual_profit_sol") is not None: raise ValueError("paper actual profit must be unmeasured")
    e = Engine(Config(**data["config"]), data); e.validate()
    if e.matched_closed() < minimum: raise ValueError(f"incomplete: {e.matched_closed()}/{minimum} matched")
    if not state.get("sessions") or any(s["errors"] or s.get("historical_replay") is not False for s in state["sessions"]):
        raise ValueError("invalid session evidence")
    expected = {**EXPECTED_BLOBS, "pumpfun.py": DECODER_BLOB}
    if state.get("source_fingerprint") != expected: raise ValueError("unfrozen source evidence")
    history = {h["signal_id"]: h for h in e.history}
    for arm, a in e.accounts.items():
        for p in a["ledger"]:
            h = history[p["signal_id"]]; d=h["decision"]; proof=h.get("creation_proof") or {}
            if not proof.get("verified") or proof.get("signature") != d.get("create_signature"):
                raise ValueError("missing confirmed creation proof")
            if proof.get("mint") != p["mint"] or proof["block_time"] < d["session_start_ns"]//1_000_000_000-3:
                raise ValueError("stale or mismatched create")
            if not d["session_start_ns"] <= d["create_ns"] <= d["decision_ns"] <= p["entry_ns"] <= p["exit_ns"]:
                raise ValueError("noncausal timestamps")
            if p["post"]["start_ns"] != p["exit_ns"] or not p["post"]["complete"]:
                raise ValueError("post-exit observation not independently complete")
            if any(x.get("actual_transaction_signature") is not None for x in p["actions"]):
                raise ValueError("do not forge actual order signatures")
            if any(x.get("state",{}).get("ns",0)>x["ns"] for x in p["actions"]):
                raise ValueError("future reserve used")
    return e


def wilson(w: int, n: int) -> list[float] | None:
    if not n:return None
    z=1.959963984540054; p=w/n; den=1+z*z/n
    centre=(p+z*z/(2*n))/den; half=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return [centre-half,centre+half]


def utc(ns:int)->str:return datetime.fromtimestamp(ns/1e9,timezone.utc).isoformat()

def esc(s:Any)->str:return str(s).replace('|','\\|').replace('\n',' ').replace('`',"'")

def fmt(x:Any, digits:int=6)->str:return 'unmeasured' if x is None else f'{x:.{digits}f}'


def analyse(e:Engine)->dict[str,Any]:
    report:dict[str,Any] = {"version":VERSION,"matched_closed":e.matched_closed(),"actual_profit_sol":None,
                           "actual_axiom_fills":False,"arms":e.summary(),"horizon_comparisons":[],
                           "conclusion_scope":"exploratory single prospective sample; not proof of money-safe execution"}
    for arm,a in e.accounts.items():
        rows=a['ledger']; s=report['arms'][arm]; wins=[r for r in rows if r['net_pnl_sol']>0]; losses=[r for r in rows if r['net_pnl_sol']<0]
        s['win_rate_wilson_95']=wilson(len(wins),len(rows))
        s['win_rate_interval_caveat']='binomial descriptive interval; correlated wallets and regimes weaken independence'
        s['average_winner_return']=statistics.mean(r['net_return'] for r in wins) if wins else None
        s['average_loser_return']=statistics.mean(r['net_return'] for r in losses) if losses else None
        s['average_winner_stake']=statistics.mean(r['buy_cost'] for r in wins) if wins else None
        s['average_loser_stake']=statistics.mean(r['buy_cost'] for r in losses) if losses else None
        s['average_win_loss_sol_ratio']=s['average_winner_sol']/-s['average_loser_sol'] if wins and losses else None
        s['median_net_trade_pnl']=statistics.median(r['net_pnl_sol'] for r in rows)
        s['net_after_removing_top_3_winners']=s['net_pnl_sol']-sum(sorted((r['net_pnl_sol'] for r in wins),reverse=True)[:3])
        s['post_exit_coverage']=dict(Counter(r['post']['coverage'] for r in rows))
        s['coins_observed_higher_after_exit']=sum(r['post'].get('went_higher') is True for r in rows)
        s['coins_observed_lower_after_exit']=sum(r['post'].get('went_lower') is True for r in rows)
        s['by_tier']={tier:{'trades':len([r for r in rows if r['tier']==tier]),
                            'net_pnl_sol':sum(r['net_pnl_sol'] for r in rows if r['tier']==tier),
                            'wins':sum(r['net_pnl_sol']>0 for r in rows if r['tier']==tier)} for tier in ('BASE','MEDIUM','HIGH','EXCEPTIONAL')}
        all_actions=[x for r in rows+a['aborted_entries'] for x in r['actions']]
        s['attempts']=len(all_actions)
        s['roi_fraction']=s['equity_sol']/e.c.starting_sol-1
        s['expectancy_per_closed_trade_sol']=s['net_pnl_sol']/len(rows) if rows else None
        s['median_winner_sol']=statistics.median(r['net_pnl_sol'] for r in wins) if wins else None
        s['median_loser_sol']=statistics.median(r['net_pnl_sol'] for r in losses) if losses else None
        s['real_chain_confirmation_latency_ms']=None
        s['adverse_quote_drift_bps']=quantiles([x['adverse_quote_drift_bps'] for x in all_actions if 'adverse_quote_drift_bps' in x])
        s['impact_sol_equivalent_informational_not_double_debited']=sum(x.get('price_impact_sol_equivalent',0) for x in all_actions)
        # Cost-only sensitivity is arithmetic on the SAME recorded fills/sizes. It
        # is not a fresh live campaign, and does not re-size or promise new fills.
        successes=sum(x['outcome']=='MODELLED_FILL' for x in all_actions)
        failed=sum(x['outcome']=='MODELLED_LANDED_FAILURE' for x in all_actions)
        volume=sum(r['curve_sol']+r['sell_gross'] for r in rows)
        sensitivity=[]
        for priority,tip,haircut_bps in ((.001,.001,0),(.005,.005,50),(.01,.01,100),(.03,.03,200)):
            extra=(successes+failed)*(priority-e.c.priority_sol)+successes*(tip-e.c.tip_sol)+volume*haircut_bps/10000
            sensitivity.append({'priority_sol':priority,'tip_sol':tip,'additional_execution_haircut_bps':haircut_bps,
                                'fixed_fill_net_pnl_sol':s['net_pnl_sol']-extra,
                                'classification':'RETROSPECTIVE_FIXED_INVENTORY_SENSITIVITY_NOT_LIVE_OUTCOME'})
        s['cost_sensitivity']=sensitivity
        s['failure_cost_revert_protected_alternative']=s['net_pnl_sol']+failed*(e.c.priority_sol+e.c.base_fee_sol)
        s['extra_failed_attempts_break_even_at_base_profile']=max(0,s['net_pnl_sol'])/(e.c.priority_sol+e.c.base_fee_sol)
    for fam in ('FULL','PARTIALS'):
        base=f'{fam}_2S'; base_rows={r['signal_id']:r for r in e.accounts[base]['ledger']}
        for sec in HORIZONS:
            arm=f'{fam}_{sec}S';rows={r['signal_id']:r for r in e.accounts[arm]['ledger']}
            ids=sorted(set(rows)&set(base_rows))
            deltas=[rows[i]['net_return']-base_rows[i]['net_return'] for i in ids]
            report['horizon_comparisons'].append({'arm':arm,'control':base,'paired_coins':len(ids),
                'independently_compounded_net_pnl_difference':report['arms'][arm]['net_pnl_sol']-report['arms'][base]['net_pnl_sol'],
                'mean_paired_net_return_per_deployed_sol_difference':statistics.mean(deltas),
                'median_paired_net_return_difference':statistics.median(deltas),
                'paired_better':sum(x>1e-9 for x in deltas),'paired_worse':sum(x < -1e-9 for x in deltas),
                'interpretation':'same selected coins, independent compounding and fill costs; not pure dollars-at-identical-size isolation'})
    report['ranking']=sorted(ARMS,key=lambda a:report['arms'][a]['net_pnl_sol'],reverse=True)
    report['sample_leader']=report['ranking'][0]
    report['finalist_status']='RESEARCH_CANDIDATE_ONLY_NOT_REAL_MONEY_APPROVED'
    return report


def render(state:dict[str,Any], e:Engine, report:dict[str,Any],manifest:dict[str,Any])->str:
    lines=['# Golden horizon/cost tournament — final research report','',
      '**Real freshly observed Solana launches; paper execution only. No Axiom order was placed. Actual wallet profit, actual Axiom fees, actual order failure rate and on-chain execution latency are unmeasured.**','',
      f"Matched closed cohort: **{e.matched_closed()} coins per variant**. Twelve independent accounts each began with **3 SOL**.",
      'The fixed 250 ms inclusion delay and retry failure classes are model assumptions. The market paths and creation signatures are observed, not generated. Historical data supplied only the frozen pre-existing reputation features.','',
      '## Ranking — gross versus net modeled economics','',
      '| Variant | Trades | W/L/BE | WR | Gross SOL before explicit fees | Modeled fees SOL | Net SOL | Cash SOL | PF incl aborted entry fees | Mark-to-market DD |',
      '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for arm in report['ranking']:
        s=report['arms'][arm]
        lines.append(f"| {arm} | {s['closed_trades']} | {s['wins']}/{s['losses']}/{s['breakeven']} | {s['win_rate']:.2%} | {s['gross_pnl_sol']:+.6f} | {s['total_modelled_fees_sol']:.6f} | {s['net_pnl_sol']:+.6f} | {s['cash_sol']:.6f} | {fmt(s['profit_factor_including_aborted_entry_fees'],3)} | {s['maximum_mark_to_market_drawdown']:.2%} |")
    lines += ['', 'Gross means modeled proceeds on the recorded modeled inventory before explicit fees; AMM impact is already embedded. It is not an ideal midpoint return. Net includes every recorded failed-entry fee even when no position opened. Rent is locked capital, then refunded under a modeled close-account assumption, not fabricated trading income.','',
              '## Does a longer holding time help?','',
              '| Variant | Net SOL difference vs family 2s | Paired mean return difference | Coins better/worse |',
              '|---|---:|---:|---:|']
    for x in report['horizon_comparisons']:
        lines.append(f"| {x['arm']} | {x['independently_compounded_net_pnl_difference']:+.6f} | {x['mean_paired_net_return_per_deployed_sol_difference']:+.2%} | {x['paired_better']}/{x['paired_worse']} |")
    lines += ['', 'These are observed differences in a new prospective sample. Independent balances and price impact can change dollar amounts; the paired per-deployed-SOL column helps separate that from simple bankroll size. Neither is a guarantee of a future optimal horizon.','',
              '## Execution and fee provenance','',
              'Real measurements: socket receipt/dequeue timing, completed decision computation, read-only RPC round trips, creation signatures/confirmed block times and public reserve states. The modeled submission-to-fill timing includes an explicit delay; no signing, dispatch acknowledgement or validator inclusion measurement is claimed.','',
              '```json',json.dumps(manifest['cost_provenance'],indent=2),'```','',
              '## Candidate and variant analysis']
    for arm in ARMS:
        s=report['arms'][arm]
        lines += ['',f'### {arm}',
          f"Net {s['net_pnl_sol']:+.6f} SOL; ROI {s['roi_fraction']:+.2%}; net expectancy per closed trade {s['expectancy_per_closed_trade_sol']:+.6f} SOL. Average winner {fmt(s['average_winner_sol'])} SOL; average loser {fmt(s['average_loser_sol'])} SOL; payoff ratio {fmt(s['average_win_loss_sol_ratio'],3)}.",
          f"Average winner stake {fmt(s['average_winner_stake'])} SOL versus loser stake {fmt(s['average_loser_stake'])}. Median trade {s['median_net_trade_pnl']:+.6f} SOL. Net excluding the three largest winners: {s['net_after_removing_top_3_winners']:+.6f} SOL.",
          f"Modeled fees by category: `{json.dumps(s['fees_by_category'])}`.",
          f"Modeled failed attempts: {s['modelled_landed_failures']}; aborted entries: {s['aborted_entries']}. Actual transaction failure rate: **unmeasured**.",
          f"Decision → modeled entry timing: `{json.dumps(s['entry_timing_ms'])}`. Exit intent → modeled fill: `{json.dumps(s['exit_timing_ms'])}`.",
          f"Post-exit coverage: `{json.dumps(s['post_exit_coverage'])}`. Coins observed higher: {s['coins_observed_higher_after_exit']}; lower: {s['coins_observed_lower_after_exit']}. Both can be true, and missing ticks remain unknown.",
          f"Tier breakdown: `{json.dumps(s['by_tier'])}`.",
          'Cost-only sensitivity on unchanged fills and stakes (not another live test):',
          '| Priority SOL | Tip SOL | Extra execution haircut bps | Net SOL |','|---:|---:|---:|---:|']
        for row in s['cost_sensitivity']:
            lines.append(f"| {row['priority_sol']} | {row['tip_sol']} | {row['additional_execution_haircut_bps']} | {row['fixed_fill_net_pnl_sol']:+.6f} |")
        lines += ['Feedback: inspect concentrated losses, whether tier sizing enlarged losers, and whether the extra holding time changed the paired payoff—not just the win count. Do not tighten stops using prices only known afterwards.']
    lines += ['', '## Every coin, entry, partial and exit','',
              'MFE/MAE below are managed-position net liquidation marks under the model, not guaranteed achievable exits. A hindsight best variant is a diagnostic, never a claim the system could know the future.']
    byarm={a:{r['signal_id']:r for r in e.accounts[a]['ledger']} for a in ARMS}
    for idx,h in enumerate(e.history,1):
        d=h['decision'];sid=h['signal_id'];mint=h['mint']
        lines += ['',f"### Coin {idx}: {esc(d.get('name'))} ({esc(d.get('symbol'))}) — `{mint}`",
                  f"Creation signature: `{d['create_signature']}`. Confirmed proof: `{json.dumps(h['creation_proof'])}`.",
                  f"Received creation {utc(d['create_ns'])}; decision complete {utc(d['decision_ns'])}. Frozen entry reasons: {d.get('prior_appearances')} prior appearances, {fmt(d.get('prior_win_rate'),4)} prior WR, {d.get('buyer_count')} early buyers, tier {d.get('tier')}; conviction `{json.dumps(d.get('conviction'))}`.",
                  f"Early buyers: `{json.dumps(d.get('buyers'))}`."]
        available={a:byarm[a][sid] for a in ARMS if sid in byarm[a]}
        best=max(available,key=lambda a:available[a]['net_return']) if available else None
        if best: lines.append(f'Hindsight best recorded per-deployed-SOL variant: **{best}** (not a deployable future selector).')
        for arm in ARMS:
            if sid not in byarm[arm]:
                failed=[r for r in e.accounts[arm]['aborted_entries'] if r['signal_id']==sid]
                lines.append(f"**{arm}: NO ENTRY, not counted as a trade.** Failed attempts retained: `{json.dumps(failed)}`.");continue
            p=byarm[arm][sid]
            lines += ['',f"**{arm}** — {'WIN' if p['net_pnl_sol']>0 else 'LOSS' if p['net_pnl_sol']<0 else 'BREAKEVEN'}; allocation {p['position_fraction']:.0%}; buy cost {p['buy_cost']:.6f} SOL; gross {p['gross_pnl_sol']:+.6f}; fees {sum(p['fees'].values()):.6f}; net {p['net_pnl_sol']:+.6f} SOL ({p['net_return']:+.2%}).",
                      f"Entry {utc(p['entry_ns'])}; exit {utc(p['exit_ns'])}; hold {p['hold_ms']:.2f} ms; reason {p['exit_reason']}; managed MFE/MAE {fmt(p['mfe'],5)}/{fmt(p['mae'],5)}.",
                      '| Side | Outcome / reason | Intent → fill ms | Tokens | Fraction original | Gross SOL | Fees SOL | Slippage drift bps |',
                      '|---|---|---:|---:|---:|---:|---:|---:|']
            for x in p['actions']:
                lines.append(f"| {x['side']} | {x['outcome']} / {x['reason']} | {(x['ns']-x['intent_ns'])/1e6:.3f} | {fmt(x.get('tokens'),3)} | {fmt(x.get('fraction_of_original'),4)} | {fmt(x.get('gross_sol',x.get('curve_sol')))} | {sum(x['fees'].values()):.6f} | {fmt(x.get('adverse_quote_drift_bps'),2)} |")
            post=p['post'];higher=post.get('went_higher');lower=post.get('went_lower')
            lines.append(f"Post-exit observation starts at this variant's exit: {utc(post['start_ns'])} to {utc(post['end_ns'])}; coverage {post['coverage']}; higher/lower {higher}/{lower}; observed maximum/minimum SOL spot {post['max_spot']}/{post['min_spot']}.")
            if best:
                difference=available[best]['net_return']-p['net_return']
                lines.append(f"Could it have made more/lost less? The retrospectively best recorded variant differed by {difference:+.2%} per deployed SOL. This is an outcome comparison, not evidence that the difference was predictable or that any intermediate stop could execute.")
    lines += ['', '## Conclusion',
              f"The highest net modeled result in this sample is **{report['sample_leader']}**. This is an exploratory research ranking, not an approval to use real money. The appropriate finalists are the simplest robust variants whose paired return, drawdown and cost sensitivity survive scrutiny; do not pick a timing solely because it won this particular sample.",
              'The next phase requested by the user—brutal stress testing and several days of fresh forward data on the selected finalists—is separate. This workflow does not launch it or automatically fund any wallet.',
              '', '## Limitations']
    lines.extend(f'- {x}' for x in manifest['limitations'])
    lines += ['- The phase-2 gate proves real launch provenance and recorded paper accounting, not the attainability of modeled orders on Axiom.',
              '- Multiple horizons are being compared on one cohort; apparent winners need an untouched future evaluation to avoid selection bias.',
              '- Unknown market activity after migration or absent post-exit ticks is not silently filled in.', '']
    return '\n'.join(lines)


def main()->int:
    p=argparse.ArgumentParser();p.add_argument('--state',type=Path,required=True);p.add_argument('--manifest',type=Path,default=Path('config/golden-horizon.json'))
    p.add_argument('--output',type=Path,required=True);p.add_argument('--gate-only',action='store_true')
    args=p.parse_args();state=json.loads(args.state.read_text());manifest=json.loads(args.manifest.read_text())
    if hashlib.sha256(args.manifest.read_bytes()).hexdigest()!=state['manifest_hash']:raise RuntimeError('manifest mismatch')
    e=gate(state);args.output.mkdir(parents=True,exist_ok=True)
    marker={'phase':2,'status':'COMPLETE','matched_closed':e.matched_closed(),'minimum':100,'arms':list(ARMS),
            'fresh_live_creations_confirmed':True,'actual_axiom_execution':False,'paper_only':True,
            'state_sha256':hashlib.sha256(args.state.read_bytes()).hexdigest(),'manifest_hash':state['manifest_hash'],
            'completed_at':datetime.now(timezone.utc).isoformat()}
    write_json(args.output/'phase2-complete.json',marker)
    if args.gate_only:return 0
    report=analyse(e);report['manifest']=manifest;report['source_sessions']=state['sessions']
    write_json(args.output/'final-results.json',report)
    md=args.output/'final-report.md';md.write_text(render(state,e,report,manifest),encoding='utf-8')
    hashes={f:hashlib.sha256((args.output/f).read_bytes()).hexdigest() for f in ('final-results.json','final-report.md')}
    write_json(args.output/'phase3-complete.json',{'phase':3,'status':'COMPLETE','matched_closed':e.matched_closed(),
        'sample_leader':report['sample_leader'],'paper_only':True,'reports_sha256':hashes,
        'completed_at':datetime.now(timezone.utc).isoformat()})
    return 0

if __name__=='__main__':raise SystemExit(main())

"""Read-only accounting and completion audit; never generates trade outcomes."""
from __future__ import annotations
import argparse,hashlib,json,math,statistics
from collections import Counter
from pathlib import Path
from golden_top4_operations import CampaignEngine, ARMS, Config, write_json

def validate_state(state,*,require_target=True):
    if state.get('market_source')!='DIRECT_SOLANA_NEW_CREATIONS':raise ValueError('wrong source')
    raw=state['engine']
    if raw.get('fixture_mode') is not False or raw.get('paper_only') is not True or raw.get('real_execution') is not False:
        raise ValueError('invalid live/paper provenance')
    e=CampaignEngine(Config(**raw['config']),raw);e.validate()
    if not state.get('sessions') or any(s.get('errors') or s.get('historical_replay') is not False for s in state['sessions']):
        raise ValueError('invalid session')
    if require_target and min(e.forward_counts().values())<100:raise ValueError('100 per model not reached')
    for arm,a in e.accounts.items():
        for p in a['ledger']:
            d=e.decisions[p['mint']];proof=e.proofs.get(p['mint'],{})
            if proof.get('verified') is not True or proof.get('mint')!=p['mint'] or proof.get('signature')!=d['create_signature']:
                raise ValueError('missing or mismatched creation')
            if proof['block_time']<d['session_start_ns']//1_000_000_000-3:raise ValueError('old creation')
            if not d['session_start_ns']<=d['create_ns']<=d['decision_ns']<=p['entry_ns']<=p['exit_ns']:
                raise ValueError('noncausal position')
            if not p['post']['complete'] or p['post']['start_ns']!=p['exit_ns']:raise ValueError('incomplete own post window')
            for x in p['actions']:
                if x.get('actual_transaction_signature') is not None:raise ValueError('forged real fill')
                if x['state']['ns']>x['ns']:raise ValueError('future quote')
            events=[x for x in e.events if x.get('kind')=='TRANSACTION_ATTEMPT' and x.get('model')==arm and x.get('signal_id')==p['signal_id']]
            if len(events)!=len(p['actions']):raise ValueError('missing action reports')
    return e


def analyse(e):
    result={'models':e.summary(),'verified_forward_counts':e.forward_counts(),
        'actual_profit_sol':None,'execution':'PAPER_ONLY','axiom_execution':False}
    for arm,a in e.accounts.items():
        rows=a['ledger'];s=result['models'][arm];w=[p for p in rows if p['net_pnl_sol']>0];l=[p for p in rows if p['net_pnl_sol']<0]
        s.update(roi_fraction=s['equity_sol']/3-1,payoff_ratio=s['average_winner_sol']/-s['average_loser_sol'] if w and l else None,
            average_winner_stake_sol=statistics.mean(p['budget'] for p in w) if w else None,
            average_loser_stake_sol=statistics.mean(p['budget'] for p in l) if l else None,
            net_without_top3_winners_sol=s['net_pnl_sol']-sum(sorted([p['net_pnl_sol'] for p in w],reverse=True)[:3]),
            losses_gross_positive_fee_erased=sum(p['gross_pnl_sol']>0 for p in l),
            losers_observed_higher_after_exit=sum(p['post'].get('went_higher') is True for p in l),
            post_exit_coverage=dict(Counter(p['post']['coverage'] for p in rows)))
        s['cost_sensitivity_fixed_paths_not_rerun']={str(mult):s['gross_pnl_sol']-mult*s['total_modelled_fees_sol'] for mult in (0.5,1.0,1.5,2.0)}
        s['by_tier']={tier:{'trades':sum(p['tier']==tier for p in rows),'net_pnl_sol':sum(p['net_pnl_sol'] for p in rows if p['tier']==tier)} for tier in ('BASE','MEDIUM','HIGH','EXCEPTIONAL')}
    common=set.intersection(*[{p['mint'] for p in a['ledger']} for a in e.accounts.values()])
    result['common_coin_subset']={'coins':len(common),'net_pnl_at_actual_independent_stakes':{arm:sum(p['net_pnl_sol'] for p in a['ledger'] if p['mint'] in common) for arm,a in e.accounts.items()},
        'note':'Not equal-size causal attribution; independent compounding changes position sizes.'}
    controls={p['mint']:p for p in e.accounts['FULL_3S']['ledger']}
    veto=[m for m,v in e.runtime.evaluations.items() if not v['accept']]
    observed=[controls[m] for m in veto if m in controls]
    result['v2_rejected_signals']={'count':len(veto),'baseline_observed':len(observed),
        'baseline_losing_SOL_avoided':-sum(p['net_pnl_sol'] for p in observed if p['net_pnl_sol']<0),
        'baseline_winning_SOL_sacrificed':sum(p['net_pnl_sol'] for p in observed if p['net_pnl_sol']>0),
        'unobserved_outcomes_unknown':len(veto)-len(observed),'note':'Descriptive baseline stakes; not a rerun v2 account.'}
    return result


def report(state_path,out,e4_path,delivery_path):
    state=json.loads(state_path.read_text());e=validate_state(state)
    delivery=json.loads(delivery_path.read_text())
    if not delivery.get('healthy') or delivery.get('last_acknowledged_event_id',0)!=len(e.events):raise ValueError('unacknowledged entry/exit reporting')
    result=analyse(e)
    result.update(state_sha256=hashlib.sha256(state_path.read_bytes()).hexdigest(),
        campaign_id=state['campaign_id'],sessions=state['sessions'],cost_model=state['engine']['config'],
        delivered_events=len(e.events),e4=json.loads(e4_path.read_text()),
        limitations=['Live coins, paper fills/costs; no realised Gambit wallet income.',
        'The 250ms fill delay is assumed, not Axiom or validator latency.',
        'Constant-product fills and own-reserve overlays do not model competing traders reacting to our orders.',
        'RPC launch verification can be delayed; native Pump routes only. Migration gaps block positions, never invent fills.',
        'Post-exit price increase is not a guaranteed executable profitable exit; future returns are not selection inputs.',
        'V2 adapts from causal outcomes under a frozen rule, starting with an empty net-reputation index.',
        'E4 complete Pump cycles are a subset; open/ambiguous/non-Pump activities prevent automatic whole-wallet PnL claims.',
        'Cost sensitivity holds fills/stakes fixed; it is not a recompounded simulation.'])
    ranking=sorted(result['models'],key=lambda a:result['models'][a]['net_pnl_sol'],reverse=True)
    result['ranking']=ranking;result['conclusion']=f"{ranking[0]} leads net modeled profit in this sample; this does not establish durable or funded profitability."
    lines=['# Golden four-model prospective Solana paper report','',result['conclusion'],'',
        '| Model | Trades | Forward verified | WR | Gross SOL | Fees SOL | Net SOL | Balance SOL |',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for arm in ranking:
        s=result['models'][arm]
        lines.append(f"| {arm} | {s['closed_trades']} | {s['verified_forward_closed']} | {s['win_rate']:.2%} | {s['gross_pnl_sol']:.6f} | {s['total_modelled_fees_sol']:.6f} | {s['net_pnl_sol']:.6f} | {s['equity_sol']:.6f} |")
    lines+=['','## Full metrics, costs, execution timing and diagnostics','```json',json.dumps(result,indent=2),'```',
        '','## Every coin, entry, exit, failed attempt and post-exit observation']
    for arm,a in e.accounts.items():
        lines+=['',f'## {arm}']
        for p in a['ledger']+a['aborted_entries']:
            d=e.decisions[p['mint']]
            lines+=['',f"### {d.get('name') or 'Name unavailable'} — `{p['mint']}`",'```json',
                json.dumps({'entry_reason':d,'position':p,'v2_evaluation':e.runtime.evaluations.get(p['mint']) if arm=='FULL_3S_V2' else None},indent=2),'```']
    out.mkdir(parents=True,exist_ok=True)
    write_json(out/'results.json',result);(out/'report.md').write_text('\n'.join(lines)+'\n')
    write_json(out/'complete.json',{'status':'COMPLETE_100_FORWARD_PER_MODEL','verified_forward_counts':e.forward_counts(),
        'state_sha256':result['state_sha256'],'results_sha256':hashlib.sha256((out/'results.json').read_bytes()).hexdigest(),
        'report_sha256':hashlib.sha256((out/'report.md').read_bytes()).hexdigest(),'paper_only':True,'actual_profit_sol':None})
    return result

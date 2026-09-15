#!/usr/bin/env python3
"""Measured build gate: synthetic tests are explicitly separated from live proof."""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

from golden_horizon_engine import quantiles
from golden_horizon_live import verify_sources, load_module, write_json


def main():
    p=argparse.ArgumentParser();p.add_argument('--production-root',type=Path,required=True)
    p.add_argument('--smoke',type=Path,required=True);p.add_argument('--stress',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    root=Path(__file__).resolve().parent
    frozen=verify_sources(root,args.production_root/'src/memecoin_bot/realtime/pumpfun.py')
    smoke=json.loads(args.smoke.read_text());stress=json.loads(args.stress.read_text())
    assert smoke['status']=='PASS' and smoke['fresh_launches']>0 and smoke['confirmed_creations']>0
    assert smoke['synthetic_data'] is False
    assert stress['status']=='PASS' and stress['synthetic_unit_paths']>=1000
    assert stress['fixture_positions_counted_as_live']==0
    core=load_module('preflight_core',root/'golden_buyer_reputation_core.py')
    mgmt=load_module('preflight_mgmt',root/'golden_management_v2.py')
    rep=core.Reputation({'unit1':[100,80],'unit2':[50,40]})
    timing=[]
    for _ in range(5000):
        t=time.perf_counter_ns();q,n,w,r=rep.qualifies(['unit1','unit2'])
        d={'prior_appearances':n,'prior_win_rate':r,'buyer_count':2};mgmt.conviction(d)
        timing.append((time.perf_counter_ns()-t)/1e6)
    measured=quantiles(timing)
    assert measured['p95']<10,'local decision computation exceeds target; not an on-chain test'
    result={'phase':1,'status':'COMPLETE','scope':'LIVE_MARKET_PAPER_RESEARCH_ONLY',
            'fresh_feed_and_independent_creation_proof':smoke,'unit_stress':stress,
            'frozen_sources':frozen,'local_decision_computation_ms':measured,
            'actual_axiom_execution_ready':False,'actual_axiom_costs_measured':False,
            'twelve_variants_configured':True,'starting_sol_per_arm':3,'required_matched_closed_per_arm':100,
            'completion_time_ns':time.time_ns(),
            'limitations':'No broadcast or authenticated Axiom data. Documented fees and explicit latency/failure assumptions, not actual fills.'}
    write_json(args.output,result)

if __name__=='__main__':main()

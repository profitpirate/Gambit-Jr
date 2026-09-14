#!/usr/bin/env python3
"""Randomized stress/bulletproof harness for Golden Management v2.

This is deliberately adversarial. It generates recovery paths, collapses, spikes,
oscillation, gaps, and long runners for every sizing tier and asserts lifecycle,
accounting, and bounded-exposure invariants.
"""
from __future__ import annotations

import argparse
import json
import math
import random
from collections import Counter
from pathlib import Path
from typing import Iterable

import golden_management_v2 as mgmt


def generate_path(rng: random.Random, max_ms: float, mode: str) -> list[mgmt.Mark]:
    t=0.0; r=0.0; peak=0.0; out=[]
    while t < max_ms + 500:
        t += rng.uniform(18.0, 95.0)
        if mode=='collapse':
            drift=-rng.uniform(.002,.018)
        elif mode=='runner':
            drift=rng.uniform(.001,.018)
        elif mode=='recover':
            drift=(-rng.uniform(.005,.025) if t < 700 else rng.uniform(.004,.025))
        elif mode=='gap':
            drift=rng.uniform(-.015,.015)
            if rng.random()<.03: drift -= rng.uniform(.20,.55)
        else:
            drift=rng.uniform(-.025,.025)
        r=max(-.95,min(3.0,r+drift))
        peak=max(peak,r)
        out.append(mgmt.Mark(t,r,state_ns=int(t*1_000_000),vsol=30+r*2,vtok=1_000_000))
        if t>max_ms+50: break
    return out


def run_guardian(cfg: mgmt.TierConfig, marks: Iterable[mgmt.Mark]) -> tuple[list[mgmt.Action],mgmt.RunnerGuardian]:
    g=mgmt.RunnerGuardian(cfg); actions=[]
    for m in marks:
        a=g.on_mark(m)
        if a is not None: actions.append(a)
        if g.closed: break
    return actions,g


def assert_invariants(cfg: mgmt.TierConfig, actions: list[mgmt.Action], g: mgmt.RunnerGuardian) -> None:
    sold=sum(a.fraction_of_entry for a in actions)
    assert sold <= 1.000000001, (cfg.name,sold,actions)
    assert g.remaining_fraction >= -1e-12
    initials=[a for a in actions if a.kind=='INITIAL']
    assert len(initials)<=1
    if initials:
        assert math.isclose(initials[0].fraction_of_entry,cfg.initial_fraction,rel_tol=0,abs_tol=1e-9)
    for a in actions:
        assert a.fraction_of_entry>0
        assert a.kind in {'INITIAL','SCALE_OUT','EXIT'}
    if g.closed:
        assert math.isclose(g.remaining_fraction,0.0,abs_tol=1e-9)
        assert math.isclose(sold,1.0,abs_tol=1e-8)


def deterministic_cases() -> None:
    # Exact risk ladder and initial structure.
    assert {k:v.position_fraction for k,v in mgmt.TIERS.items()} == {
        'BASE':.05,'MEDIUM':.08,'HIGH':.15,'EXCEPTIONAL':.25}
    assert {k:v.initial_fraction for k,v in mgmt.TIERS.items()} == {
        'BASE':.30,'MEDIUM':.30,'HIGH':.20,'EXCEPTIONAL':.20}

    # Every tier must flatten at its ceiling even if price is hitting a new high.
    for cfg in mgmt.TIERS.values():
        g=mgmt.RunnerGuardian(cfg)
        first=g.on_mark(mgmt.Mark(cfg.initial_target_ms,.02)); assert first and first.kind=='INITIAL'
        a=g.on_mark(mgmt.Mark(cfg.max_hold_ms,1.5)); assert a and a.kind=='EXIT'
        assert g.closed

    # E4-like recoverable -17% path must not be killed by a dumb fixed -10% stop.
    g=mgmt.RunnerGuardian(mgmt.TIERS['EXCEPTIONAL'])
    assert g.on_mark(mgmt.Mark(300,-.02)).kind=='INITIAL'
    assert g.on_mark(mgmt.Mark(500,-.17)) is None
    assert not g.closed
    # It recovers and begins scaling rather than being dead.
    a=g.on_mark(mgmt.Mark(1000,.18)); assert a and a.kind=='SCALE_OUT'

    # Catastrophic event must flatten immediately after initial.
    g=mgmt.RunnerGuardian(mgmt.TIERS['BASE'])
    g.on_mark(mgmt.Mark(350,-.01))
    a=g.on_mark(mgmt.Mark(500,-.40)); assert a and a.kind=='EXIT' and g.closed


def main() -> int:
    p=argparse.ArgumentParser()
    p.add_argument('--paths',type=int,default=50000)
    p.add_argument('--seed',type=int,default=20260914)
    p.add_argument('--output',type=Path)
    a=p.parse_args()
    deterministic_cases()
    rng=random.Random(a.seed); modes=('collapse','runner','recover','gap','chop')
    counts=Counter(); action_counts=Counter(); max_sold=0.0
    for i in range(a.paths):
        tier=('BASE','MEDIUM','HIGH','EXCEPTIONAL')[i%4]
        cfg=mgmt.TIERS[tier]; mode=modes[i%len(modes)]
        actions,g=run_guardian(cfg,generate_path(rng,cfg.max_hold_ms,mode))
        assert_invariants(cfg,actions,g)
        # Generated paths extend beyond max-hold, so after an initial every healthy
        # policy must ultimately close by an EXIT action.
        assert g.closed, (tier,mode,g.summary(),actions[-3:])
        assert any(x.kind=='EXIT' for x in actions)
        max_sold=max(max_sold,sum(x.fraction_of_entry for x in actions))
        counts[(tier,mode)] += 1
        for x in actions: action_counts[x.reason]+=1
    result={
      'version':'golden-management-v2-stress-v1','seed':a.seed,'paths':a.paths,
      'status':'PASS','max_total_fraction_sold':max_sold,
      'cases':{f'{k[0]}:{k[1]}':v for k,v in counts.items()},
      'action_reasons':dict(action_counts),
    }
    if a.output:
        a.output.parent.mkdir(parents=True,exist_ok=True)
        a.output.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
    print(json.dumps(result,sort_keys=True))
    return 0

if __name__=='__main__': raise SystemExit(main())

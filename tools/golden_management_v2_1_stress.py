#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

import golden_management_v2 as v2
import golden_management_v2_1 as v21


def run_path(rng: random.Random, tier: str, steps: int = 160) -> dict:
    cfg = v2.TIERS[tier]
    tracker = v21.FlowTracker()
    g = v21.FlowAwareGuardian(cfg, tracker)
    sold = 0.0
    ret = rng.uniform(-0.05, 0.05)
    closed_at = None
    max_ret = ret
    min_ret = ret
    for i in range(steps):
        t = 100.0 + i * 100.0
        drift = rng.gauss(0, 0.035)
        if rng.random() < 0.04:
            drift += rng.choice([-0.25, 0.25, 0.45])
        ret = max(-0.95, ret + drift)
        max_ret = max(max_ret, ret); min_ret = min(min_ret, ret)
        # Generate causal tape loosely related to path, but with contradictions too.
        buy_bias = 0.62 if drift > 0 else 0.38
        if rng.random() < 0.20: buy_bias = 1.0 - buy_bias
        for j in range(rng.randint(0, 4)):
            buy = rng.random() < buy_bias
            kind = 'BUY' if buy else 'SELL'
            wallet = f'w{rng.randint(0,20)}'
            sol = max(0.001, rng.lognormvariate(-1.0, 0.9))
            tok = max(1.0, rng.lognormvariate(5.0, 0.8))
            creator = (not buy and rng.random() < 0.01)
            tracker.ingest(v21.FlowEvent(t+j*.1, kind, wallet, sol, tok, ret, is_creator=creator, vsol=30+max(-5,20*ret)))
        action = g.on_mark(v2.Mark(t, ret))
        if action:
            assert 0 < action.fraction_of_entry <= 1.0 + 1e-12
            sold += action.fraction_of_entry
            assert sold <= 1.000000001, (tier, sold, action)
            if action.kind == 'EXIT':
                closed_at = t
                break
    if not g.closed:
        # Safety ceiling must eventually close when presented a final mark.
        action = g.on_mark(v2.Mark(cfg.max_hold_ms + 1, ret))
        if action:
            sold += action.fraction_of_entry
        assert g.closed
    assert sold <= 1.000000001
    return {'sold': sold, 'closed_at': closed_at, 'max_ret': max_ret, 'min_ret': min_ret, 'decisions': len(g.decisions)}


def cluster_fuzz(rng: random.Random, rounds: int) -> None:
    for _ in range(rounds):
        book = v21.PriorEvidenceBook()
        wallets = [f'w{i}' for i in range(rng.randint(1, 8))]
        for w in wallets:
            book.wallet_seen[w] = rng.randint(5, 100)
        for i,a in enumerate(wallets):
            for b in wallets[i+1:]:
                denom = min(book.wallet_seen[a], book.wallet_seen[b])
                if rng.random() < .4:
                    book.pair_seen[tuple(sorted((a,b)))] = rng.randint(0, denom)
        p = book.cluster_profile(wallets)
        assert 0 <= p['independent_groups'] <= len(wallets)
        assert p['redundant_wallets'] == len(wallets)-p['independent_groups']
        c = v21.enhanced_conviction({'prior_win_rate':rng.uniform(.7,1.0),'prior_appearances':rng.randint(10,1000),'buyers':wallets,'creator':''}, book)
        assert c['conviction_tier'] in v2.TIERS
        assert 0 <= c['conviction_score'] <= 100


def main() -> int:
    p=argparse.ArgumentParser(); p.add_argument('--paths',type=int,default=100000); p.add_argument('--seed',type=int,default=20260915); p.add_argument('--output',type=Path,required=True)
    a=p.parse_args(); rng=random.Random(a.seed); max_sold=0.0; closed=0; decision_counts=[]
    tiers=list(v2.TIERS)
    for i in range(a.paths):
        r=run_path(rng, tiers[i%len(tiers)])
        max_sold=max(max_sold,r['sold']); closed += int(r['sold'] > 0.999999); decision_counts.append(r['decisions'])
    cluster_fuzz(rng, max(1000, a.paths//20))
    out={'status':'PASS','paths':a.paths,'seed':a.seed,'max_total_fraction_sold':max_sold,'fully_closed_paths':closed,'cluster_fuzz_rounds':max(1000,a.paths//20),'max_flow_decisions':max(decision_counts) if decision_counts else 0}
    a.output.parent.mkdir(parents=True,exist_ok=True); a.output.write_text(json.dumps(out,indent=2,sort_keys=True)+'\n'); print(json.dumps(out,sort_keys=True)); return 0

if __name__=='__main__': raise SystemExit(main())

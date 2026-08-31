#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from pathlib import Path

from e4_build_winner_creator_registry import DEFAULT_RPCS, RpcPool, resolve_creator


def load_winners(path: Path) -> tuple[dict[str, dict], dict[str, str]]:
    data=json.loads(path.read_text(encoding='utf-8'))
    creators=dict(data.get('creators') or {})
    mint_to_creator={}
    for creator,row in creators.items():
        for mint in row.get('winner_mints') or []:
            mint_to_creator[str(mint)]=str(creator)
    return creators,mint_to_creator


async def run(args: argparse.Namespace) -> dict:
    winner_creators,winner_mints=load_winners(args.winners)
    losing_mints=list(json.loads(args.losers.read_text(encoding='utf-8')))
    urls=[part.strip() for part in args.rpc_urls.split(',') if part.strip()]
    async with RpcPool(urls,timeout=args.timeout,concurrency=args.concurrency) as rpc:
        resolved=[]
        for start in range(0,len(losing_mints),args.batch_size):
            batch=[{'mint':mint,'gross_pnl_sol':0.0,'entry_sol':0.0} for mint in losing_mints[start:start+args.batch_size]]
            resolved.extend(await asyncio.gather(*(resolve_creator(rpc,row) for row in batch)))
            print(json.dumps({'processed':len(resolved),'target':len(losing_mints),'resolved':sum(bool(r.get('creator')) for r in resolved)}),flush=True)
        errors=rpc.errors[-100:]

    loss_by_creator=Counter(str(row['creator']) for row in resolved if row.get('creator'))
    all_creators=set(winner_creators)|set(loss_by_creator)
    rows=[]
    for creator in all_creators:
        winrow=winner_creators.get(creator,{})
        wins=int(winrow.get('e4_observed_wins') or 0)
        losses=int(loss_by_creator.get(creator,0))
        trades=wins+losses
        rows.append({
            'creator':creator,
            'wins':wins,
            'losses':losses,
            'trades':trades,
            'gross_win_rate':wins/trades if trades else 0.0,
            'winning_pnl_sol':float(winrow.get('e4_gross_pnl_sol') or 0.0),
            'winner_mints':list(winrow.get('winner_mints') or []),
            'loser_mints':[r['mint'] for r in resolved if r.get('creator')==creator],
        })
    rows.sort(key=lambda r:(-r['trades'],-r['gross_win_rate'],-r['winning_pnl_sol']))
    repeated=[r for r in rows if r['trades']>=2]
    overlap=[r for r in rows if r['wins'] and r['losses']]
    pure_winners=[r for r in rows if r['wins']>=2 and r['losses']==0]
    pure_losers=[r for r in rows if r['losses']>=2 and r['wins']==0]
    return {
        'version':'e4-creator-expectancy-v1',
        'winner_mints':len(winner_mints),
        'loser_mints':len(losing_mints),
        'resolved_loser_mints':sum(bool(r.get('creator')) for r in resolved),
        'unique_winning_creators':len(winner_creators),
        'unique_losing_creators':len(loss_by_creator),
        'unique_creators_total':len(rows),
        'creator_overlap_count':len(overlap),
        'repeated_creator_count':len(repeated),
        'repeat_pure_winner_creators':len(pure_winners),
        'repeat_pure_loser_creators':len(pure_losers),
        'positions_from_repeat_creators':sum(r['trades'] for r in repeated),
        'repeat_creator_positions_win_rate':sum(r['wins'] for r in repeated)/sum(r['trades'] for r in repeated) if repeated else None,
        'top_creators':rows,
        'overlap_creators':overlap,
        'pure_repeat_winners':pure_winners,
        'pure_repeat_losers':pure_losers,
        'unresolved_losers':[r for r in resolved if not r.get('creator')],
        'rpc_errors':errors,
    }


def main()->int:
    p=argparse.ArgumentParser()
    p.add_argument('--winners',type=Path,default=Path('models/e4/e4-winning-creators.json'))
    p.add_argument('--losers',type=Path,default=Path('models/e4/e4-losing-mints.json'))
    p.add_argument('--output',type=Path,default=Path('artifacts/e4-creator-expectancy.json'))
    p.add_argument('--rpc-urls',default=','.join(DEFAULT_RPCS))
    p.add_argument('--timeout',type=float,default=6.0)
    p.add_argument('--concurrency',type=int,default=12)
    p.add_argument('--batch-size',type=int,default=24)
    args=p.parse_args()
    report=asyncio.run(run(args))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    md=args.output.with_suffix('.md')
    md.write_text(
        '# E4 creator expectancy\n\n'
        f"- Winner mints: **{report['winner_mints']}**\n"
        f"- Loser mints: **{report['loser_mints']}**\n"
        f"- Resolved loser creators: **{report['resolved_loser_mints']}**\n"
        f"- Unique winning creators: **{report['unique_winning_creators']}**\n"
        f"- Unique losing creators: **{report['unique_losing_creators']}**\n"
        f"- Creator overlap (both W and L): **{report['creator_overlap_count']}**\n"
        f"- Repeat creators: **{report['repeated_creator_count']}**\n"
        f"- Repeat-creator position gross WR: **{report['repeat_creator_positions_win_rate']:.2%}**\n\n"
        '## Top repeat creators\n\n' + '\n'.join(
            f"- `{r['creator']}` — {r['wins']}W/{r['losses']}L ({r['gross_win_rate']:.1%}), {r['trades']} trades"
            for r in report['top_creators'] if r['trades']>=2
        ) + '\n',encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('resolved_loser_mints','unique_losing_creators','creator_overlap_count','repeated_creator_count','repeat_creator_positions_win_rate')}),flush=True)
    return 0 if report['resolved_loser_mints']>=70 else 2

if __name__=='__main__':
    raise SystemExit(main())

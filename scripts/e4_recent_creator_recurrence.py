#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from pathlib import Path

from e4_build_winner_creator_registry import DEFAULT_RPCS, RpcPool, resolve_creator


def historical_map(path: Path) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding='utf-8'))
    return {str(row['creator']): dict(row) for row in data.get('top_creators') or []}


async def run(args: argparse.Namespace) -> dict:
    recent = list(json.loads(args.recent.read_text(encoding='utf-8')))
    hist = historical_map(args.history)
    urls = [x.strip() for x in args.rpc_urls.split(',') if x.strip()]
    async with RpcPool(urls, timeout=args.timeout, concurrency=args.concurrency) as rpc:
        resolved = []
        for start in range(0, len(recent), args.batch_size):
            batch = recent[start:start+args.batch_size]
            probes = [
                {'mint': r['mint'], 'gross_pnl_sol': float(r.get('gross_pnl_sol') or 0), 'entry_sol': float(r.get('buy_sol') or 0)}
                for r in batch
            ]
            got = await asyncio.gather(*(resolve_creator(rpc, row) for row in probes))
            for source, item in zip(batch, got):
                resolved.append({**source, **item})
            print(json.dumps({'processed': len(resolved), 'target': len(recent), 'resolved': sum(bool(r.get('creator')) for r in resolved)}), flush=True)
        errors = rpc.errors[-100:]

    counts = Counter(str(r['creator']) for r in resolved if r.get('creator'))
    rows = []
    for r in resolved:
        creator = str(r.get('creator') or '')
        prior = hist.get(creator)
        rows.append({
            **r,
            'creator_repeats_inside_recent39': counts.get(creator, 0),
            'creator_in_historical_184': bool(prior),
            'historical_wins': int((prior or {}).get('wins') or 0),
            'historical_losses': int((prior or {}).get('losses') or 0),
            'historical_trades': int((prior or {}).get('trades') or 0),
            'historical_gross_win_rate': float((prior or {}).get('gross_win_rate') or 0.0),
            'same_mint_already_in_historical_model': bool(prior and r['mint'] in ((prior.get('winner_mints') or []) + (prior.get('loser_mints') or []))),
        })

    unique = {str(r.get('creator')) for r in rows if r.get('creator')}
    recurring_hist = [r for r in rows if r['creator_in_historical_184']]
    recurring_clean = [r for r in recurring_hist if not r['same_mint_already_in_historical_model']]
    fresh_only = [r for r in rows if not r['same_mint_already_in_historical_model']]
    inside_repeat_creators = {c for c, n in counts.items() if n >= 2}
    inside_repeat_rows = [r for r in rows if str(r.get('creator') or '') in inside_repeat_creators]

    def wr(items):
        closed = [r for r in items if r.get('outcome') in {'WIN','LOSS'}]
        return sum(r['outcome']=='WIN' for r in closed) / len(closed) if closed else None

    return {
        'version': 'e4-recent-creator-recurrence-v1',
        'recent_entries': len(rows),
        'resolved_entries': sum(bool(r.get('creator')) for r in rows),
        'unique_recent_creators': len(unique),
        'creators_repeated_inside_recent39': len(inside_repeat_creators),
        'positions_from_inside_repeat_creators': len(inside_repeat_rows),
        'inside_repeat_positions_wr': wr(inside_repeat_rows),
        'entries_creator_in_historical_184': len(recurring_hist),
        'historical_creator_match_rate': len(recurring_hist)/len(rows) if rows else None,
        'entries_same_mint_already_in_history': sum(r['same_mint_already_in_historical_model'] for r in rows),
        'entries_creator_in_history_but_mint_not_in_history': len(recurring_clean),
        'clean_recurring_creator_wr': wr(recurring_clean),
        'fresh_mints_not_in_history': len(fresh_only),
        'fresh_mint_wr': wr(fresh_only),
        'rows': rows,
        'repeat_creators_inside_recent39': [
            {'creator': c, 'count': counts[c], 'outcomes': [r['outcome'] for r in rows if r.get('creator') == c], 'historical': hist.get(c)}
            for c in sorted(inside_repeat_creators, key=lambda c: (-counts[c], c))
        ],
        'rpc_errors': errors,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--recent', type=Path, default=Path('models/e4/e4-fresh-recent-39.json'))
    p.add_argument('--history', type=Path, default=Path('models/e4/e4-creator-expectancy.json'))
    p.add_argument('--output', type=Path, default=Path('artifacts/e4-recent-creator-recurrence.json'))
    p.add_argument('--rpc-urls', default=','.join(DEFAULT_RPCS))
    p.add_argument('--timeout', type=float, default=6.0)
    p.add_argument('--concurrency', type=int, default=12)
    p.add_argument('--batch-size', type=int, default=16)
    args = p.parse_args()
    report = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    md = args.output.with_suffix('.md')
    md.write_text(
        '# E4 recent creator recurrence\n\n'
        f"- Recent entries: **{report['recent_entries']}**\n"
        f"- Resolved: **{report['resolved_entries']}**\n"
        f"- Unique creators: **{report['unique_recent_creators']}**\n"
        f"- Creators repeated inside this 39-entry slice: **{report['creators_repeated_inside_recent39']}**\n"
        f"- Positions from repeat creators inside slice: **{report['positions_from_inside_repeat_creators']}**\n"
        f"- Repeat-slice gross WR: **{report['inside_repeat_positions_wr']:.2%}**\n"
        f"- Entries whose creator is in historical 184-dev registry: **{report['entries_creator_in_historical_184']}** ({report['historical_creator_match_rate']:.2%})\n"
        f"- Same mints already represented in historical model: **{report['entries_same_mint_already_in_history']}**\n"
        f"- New mint but creator already known historically: **{report['entries_creator_in_history_but_mint_not_in_history']}**\n"
        + (f"- Gross WR of those clean recurring-creator entries: **{report['clean_recurring_creator_wr']:.2%}**\n" if report['clean_recurring_creator_wr'] is not None else '')
        + '\n## Repeat creators inside slice\n\n'
        + '\n'.join(f"- `{r['creator']}` — {r['count']} entries, outcomes {r['outcomes']}" for r in report['repeat_creators_inside_recent39'])
        + '\n', encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('recent_entries','resolved_entries','unique_recent_creators','creators_repeated_inside_recent39','positions_from_inside_repeat_creators','inside_repeat_positions_wr','entries_creator_in_historical_184','historical_creator_match_rate','entries_same_mint_already_in_history','entries_creator_in_history_but_mint_not_in_history','clean_recurring_creator_wr')}, default=str), flush=True)
    return 0 if report['resolved_entries'] >= 35 else 2

if __name__ == '__main__':
    raise SystemExit(main())

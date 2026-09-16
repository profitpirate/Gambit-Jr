"""Operational reporting adapter; the four strategy/accounting sources stay pinned.

No orders, secrets, Axiom feed, synthetic production inputs or historical exits.
"""
from __future__ import annotations
import copy
import time
from collections import Counter
# Apply the event-stream reserve-state correction before CampaignEngine creates
# the frozen V2 runtime. Selection, costs, sizing and timers are unchanged.
import golden_top4_v2_eventstream as _v2_eventstream_fix
from golden_top4_engine import CampaignEngine as OriginalEngine, ARMS, VERSION, Config, write_json

class CampaignEngine(OriginalEngine):
    def __init__(self, config, state=None, *, fixture_mode=False):
        super().__init__(config, state, fixture_mode=fixture_mode)
        self.intent_keys=set((state or {}).get('reported_intents', []))
        self.aborted_keys=set((state or {}).get('reported_aborts', []))

    def _event(self, row):
        row=copy.deepcopy(row)
        row['campaign_scope']='DIRECT_SOLANA_NEW_CREATIONS_PAPER_EXECUTION'
        row['axiom_listing_verified']=None
        if row.get('model') in self.accounts:
            arm=row['model'];a=self.accounts[arm]
            eng=self.runtime.candidate if arm=='FULL_3S_V2' else self.controls
            key='FULL_3S' if arm=='FULL_3S_V2' else arm
            pnls=[p['net_pnl_sol'] for p in a['ledger']]
            paid=Counter()
            for model,p in self.positions():
                if model==arm: paid.update(p['fees'])
            row.update(account_cash_sol=a['cash'], account_equity_sol=eng.equity(key),
                account_snapshot_semantics='after local engine tick, before publication',
                cumulative_closed_net_pnl_sol=sum(pnls)-a['failed_entry_fees'],
                cumulative_modelled_fees_by_category=dict(paid),
                closed_trades=len(pnls),wins=sum(x>0 for x in pnls),losses=sum(x<0 for x in pnls),
                win_rate=sum(x>0 for x in pnls)/len(pnls) if pnls else None)
        super()._event(row)

    def emit_intents(self):
        for arm,p in self.positions():
            intent=p.get('entry_intent') if p['status']=='PENDING' else p.get('intent')
            side='BUY' if p['status']=='PENDING' else 'SELL'
            if intent:
                key=f"{arm}:{p['signal_id']}:{side}:{intent['requested_ns']}"
                if key not in self.intent_keys:
                    self.intent_keys.add(key)
                    self._event({'kind':'TRANSACTION_INTENT','model':arm,'mint':p['mint'],
                        'coin_name':self.decisions[p['mint']].get('name'),
                        'signal_id':p['signal_id'],'side':side,'intent':intent,
                        'stake_sol':p['budget'],'intent_has_not_filled':True})
            if p['status']=='NO_ENTRY':
                key=f"{arm}:{p['signal_id']}"
                if key not in self.aborted_keys:
                    self.aborted_keys.add(key)
                    self._event({'kind':'ABORTED_ENTRY','model':arm,'mint':p['mint'],
                        'signal_id':p['signal_id'],'reason':p.get('abort_reason','ENTRY_ATTEMPTS_OR_AGE_LIMIT'),
                        'fees_retained_sol':sum(p['fees'].values())})

    def submit(self, d, s, now):
        result=super().submit(d,s,now); self.emit_intents(); return result

    def tick(self, now, states, last_feed_ns):
        super().tick(now,states,last_feed_ns); self.emit_intents()

    def forward_counts(self):
        counts=super().forward_counts()
        if not self.fixture_mode:
            for arm,a in self.accounts.items():
                uncertain=self.runtime.candidate.uncertain_mints if arm=='FULL_3S_V2' else self.control_uncertain
                counts[arm]=sum(p['mint'] not in uncertain and self.proofs.get(p['mint'],{}).get('verified') is True and
                    (p.get('post') or {}).get('complete') is True and (p.get('post') or {}).get('coverage')=='OBSERVED' for p in a['ledger'])
        return counts

    def persistence(self):
        data=super().persistence()
        data.update(reported_intents=sorted(self.intent_keys),reported_aborts=sorted(self.aborted_keys),
                    market_source='DIRECT_SOLANA_NEW_CREATIONS',axiom_feed_verified=False,
                    v2_event_stream_state_fix=_v2_eventstream_fix.PATCH_VERSION)
        return data

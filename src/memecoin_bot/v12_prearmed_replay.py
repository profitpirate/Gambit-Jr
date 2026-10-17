"""Offline evidence replay. A policy pass never establishes an executable fill.

Uses the production policy and verified receipt cashflows. There is deliberately
no price-peak fill, automatic reset, network client, signer or live import here.
"""
from dataclasses import fields
from .v12_prearmed_policy import EntryFacts
from .v12_prearmed_evidence import FROZEN_MODEL_SHA256


def replay_decision(payload, policy):
    if payload.get('model_sha256') != FROZEN_MODEL_SHA256:
        return {'state': 'INSUFFICIENT_EVIDENCE', 'reason': 'MODEL_MISMATCH'}
    names = [field.name for field in fields(EntryFacts)]
    if any('fact_'+name not in payload for name in names):
        return {'state': 'INSUFFICIENT_EVIDENCE', 'reason': 'MISSING_CAUSAL_INPUTS'}
    decision = policy.entry(EntryFacts(**{name: payload['fact_'+name] for name in names}))
    return {'state': 'REPLAYED', 'accepted': decision.accepted, 'reason': decision.reason,
            'matches_recorded_decision': (decision.accepted is payload.get('decision_accepted')
                                         and decision.reason == payload.get('reason')),
            'execution': 'NOT_ESTABLISHED_BY_POLICY', 'pnl_lamports': None}


class ReceiptReplay:
    """Strict finite-cash replay for one bot mint; no mark-to-market profits.

    cash_delta includes actual fees, rent and proceeds. Unknown/failed receipts
    never invent token fills. Confirmed fill time controls the production hold
    clock, rather than the order-submission time used in earlier paper models.
    """
    def __init__(self, cash_lamports):
        if type(cash_lamports) is not int or cash_lamports < 0:
            raise ValueError('INVALID_INITIAL_CAPITAL')
        self.initial = self.cash = cash_lamports
        self.tokens = self.bought = self.fill_ns = 0
        self.unknown = False
        self.seen = {}

    def apply(self, receipt):
        required = {'signature', 'cash_delta', 'token_delta', 'finalized', 'failed', 'fill_ns'}
        if not required <= receipt.keys() or receipt['finalized'] is not True:
            self.unknown = True
            return 'UNKNOWN_OUTCOME'
        signature = receipt['signature']
        if not isinstance(signature, str) or not signature:
            raise ValueError('RECEIPT_ID_REQUIRED')
        if signature in self.seen:
            if self.seen[signature] != receipt:
                raise ValueError('CONFLICTING_RECEIPT')
            return 'ALREADY_APPLIED'
        cash, tokens, filled = (receipt[k] for k in ('cash_delta', 'token_delta', 'fill_ns'))
        if (any(type(v) is not int for v in (cash, tokens, filled))
                or type(receipt['failed']) is not bool or filled <= 0):
            raise ValueError('INVALID_RECEIPT')
        if receipt['failed'] and (tokens != 0 or cash > 0):
            raise ValueError('FAILED_TRANSACTION_CANNOT_FILL')
        if self.cash+cash < 0 or self.tokens+tokens < 0:
            raise ValueError('CAPITAL_OR_TOKEN_DEFICIT')
        if tokens > 0 and self.bought:
            raise ValueError('SECOND_ENTRY_NOT_SUPPORTED')
        self.seen[signature] = dict(receipt)
        self.cash += cash
        self.tokens += tokens
        if tokens > 0:
            self.bought = tokens
            self.fill_ns = filled
        return 'FAILED_FEE_RECORDED' if receipt['failed'] else 'RECEIPT_APPLIED'

    def snapshot(self):
        closed = bool(self.bought and self.tokens == 0 and not self.unknown)
        return {'cash_lamports': self.cash, 'remaining_tokens_raw': self.tokens,
                'state': 'UNKNOWN' if self.unknown else 'CLOSED' if closed else 'OPEN' if self.tokens else 'NO_FILL',
                'realized_lamports': self.cash-self.initial if closed else None,
                'fill_ns': self.fill_ns or None,
                'hold_deadline_ns': self.fill_ns+2_000_000_000 if self.fill_ns else None}

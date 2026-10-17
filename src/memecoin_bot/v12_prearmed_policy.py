"""Frozen Pre-Armed decisions over explicitly causal input facts.

No signer, RPC client, transaction submission, legacy selection fallback, or
environment-controlled strategy parameters are present in this module.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from .v12_prearmed_evidence import FROZEN_MODEL_SHA256, canonical_hash


@dataclass(frozen=True, slots=True)
class EntryFacts:
    creator: str
    social_handle: str
    social_status_ns: int
    create_ns: int
    decision_ns: int
    metadata_observed_ns: int
    creator_seed_sol: float
    mayhem_mode: bool
    expected_token_output: float
    current_token_output: float
    create_price: float
    current_price: float


@dataclass(frozen=True, slots=True)
class EntryDecision:
    accepted: bool
    reason: str
    fraction: float = 0.0


@dataclass(frozen=True, slots=True)
class ExitAction:
    fraction_of_original: float
    reason: str


@dataclass(frozen=True, slots=True)
class ExitState:
    fill_ns: int
    reference_price: float
    peak_price: float
    first_partial_done: bool = False
    closed: bool = False
    last_observed_ns: int = 0
    last_sequence: int = -1


@dataclass(frozen=True, slots=True)
class ExitUpdate:
    state: ExitState
    actions: tuple[ExitAction, ...]


def positive_finite(value: Any) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        return False


def positive_time(value: Any) -> bool:
    return type(value) is int and value > 0


class FrozenPrearmedPolicy:
    __slots__ = ('_handles', '_attempts', '_maximum_evidence_ns')
    family = 'prearmed_repeat_creator_social_status'
    fraction_of_available_cash = 0.10
    maximum_concurrent_positions = 2
    hold_ns = 2_000_000_000
    modeled_entry_latency_ns = 5_000_000

    def __init__(self, model: Mapping[str, Any]):
        if canonical_hash(model) != FROZEN_MODEL_SHA256:
            raise ValueError('Frozen model fingerprint mismatch')
        # Freeze copied lookup tables; subsequent caller mutations cannot alter policy.
        snapshot = json.loads(json.dumps(model, allow_nan=False))
        self._handles = MappingProxyType({
            str(creator): frozenset(str(handle).lower().lstrip('@') for handle in handles)
            for creator, handles in snapshot['creator_handles'].items()
        })
        self._attempts = MappingProxyType(dict(snapshot['creator_prior_e4_attempts']))
        self._maximum_evidence_ns = snapshot['maximum_evidence_ns']

    def entry(self, facts: EntryFacts) -> EntryDecision:
        times = (facts.create_ns, facts.decision_ns, facts.social_status_ns, facts.metadata_observed_ns)
        if not all(positive_time(value) for value in times):
            return EntryDecision(False, 'INVALID_TIMESTAMPS')
        if facts.create_ns <= self._maximum_evidence_ns:
            return EntryDecision(False, 'PRE_FREEZE_INPUT')
        if facts.decision_ns < facts.create_ns or facts.metadata_observed_ns > facts.decision_ns:
            return EntryDecision(False, 'INPUT_NOT_AVAILABLE_AT_DECISION')
        if type(facts.mayhem_mode) is not bool or facts.mayhem_mode:
            return EntryDecision(False, 'MAYHEM_OR_UNKNOWN_MODE')
        if not isinstance(facts.creator, str) or self._attempts.get(facts.creator, 0) < 1:
            return EntryDecision(False, 'UNKNOWN_CREATOR')
        if not positive_finite(facts.creator_seed_sol) or facts.creator_seed_sol < 2:
            return EntryDecision(False, 'CREATOR_SEED_BELOW_FLOOR')
        handle = str(facts.social_handle).lower().lstrip('@')
        if handle not in self._handles.get(facts.creator, ()):
            return EntryDecision(False, 'NOT_PREARMED_HANDLE')
        if not 0 <= facts.create_ns - facts.social_status_ns <= 10_000_000_000:
            return EntryDecision(False, 'SOCIAL_AGE_OUTSIDE_WINDOW')
        if not all(positive_finite(value) for value in (
            facts.expected_token_output, facts.current_token_output, facts.create_price, facts.current_price,
        )):
            return EntryDecision(False, 'INVALID_QUOTE')
        if facts.current_token_output / facts.expected_token_output < 0.65:
            return EntryDecision(False, 'ENTRY_OUTPUT_GUARD_REJECTED')
        if facts.current_price / facts.create_price > 1.5:
            return EntryDecision(False, 'ENTRY_CHASE_GUARD_REJECTED')
        return EntryDecision(True, 'FROZEN_PREARMED_SELECTED', 0.10)

    def budget(self, available_cash: float, occupied_slots: int) -> float:
        if type(occupied_slots) is not int or occupied_slots < 0:
            raise ValueError('Invalid occupied slot count')
        if not positive_finite(available_cash) or occupied_slots >= 2:
            return 0.0
        return available_cash * 0.10

    def exits(self, state: ExitState, *, observed_ns: int, price: float | None = None,
              complete: bool = False, timer_only: bool = False, sequence: int = 0) -> ExitUpdate:
        """Generate paper/reference actions; execution must acknowledge actual fills.

        State here describes theoretical decisions. It must not be committed as
        an actual partial/close before an executor confirms that sale.
        Exit fractions are fractions of the ORIGINAL tokens, not the remainder.
        """
        if not positive_time(state.fill_ns) or not positive_finite(state.reference_price) or not positive_finite(state.peak_price):
            raise ValueError('Missing or invalid fill reference')
        if (not positive_time(observed_ns) or type(complete) is not bool
                or type(timer_only) is not bool or type(sequence) is not int or sequence < 0):
            raise ValueError('Invalid observation')
        if state.closed or observed_ns <= state.fill_ns:
            return ExitUpdate(state, ())
        if (observed_ns, sequence) <= (state.last_observed_ns, state.last_sequence) and not timer_only:
            return ExitUpdate(state, ())
        deadline = state.fill_ns + self.hold_ns
        if observed_ns > deadline or (timer_only and observed_ns >= deadline):
            remaining = 0.70 if state.first_partial_done else 1.0
            closed = ExitState(state.fill_ns, state.reference_price, state.peak_price, state.first_partial_done, True, observed_ns, sequence)
            return ExitUpdate(closed, (ExitAction(remaining, 'MAXIMUM_HOLD'),))
        if timer_only:
            return ExitUpdate(state, ())
        if not positive_finite(price):
            raise ValueError('Invalid market price')
        peak = max(state.reference_price, state.peak_price, price)
        multiple = price / state.reference_price
        peak_multiple = peak / state.reference_price
        partial = state.first_partial_done
        actions = []
        if not partial and multiple >= 1.15:
            actions.append(ExitAction(0.30, 'FIRST_TAKE'))
            partial = True
        floor = max(0.70, 1.02, peak_multiple * 0.75) if partial else (
            max(0.70, peak_multiple * 0.75) if peak_multiple >= 1.15 else 0.70
        )
        reason = 'LIQUIDITY_EMERGENCY' if complete else (
            'TRAILING_OR_STOP' if multiple <= floor else ''
        )
        if reason:
            actions.append(ExitAction(0.70 if partial else 1.0, reason))
        next_state = ExitState(state.fill_ns, state.reference_price, peak, partial, bool(reason), observed_ns, sequence)
        return ExitUpdate(next_state, tuple(actions))

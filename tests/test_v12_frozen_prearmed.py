"""Offline boundary tests and comparisons with the actual causal paper simulator."""
from __future__ import annotations

import copy
import json
import math
import random
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from memecoin_bot.v12_prearmed_evidence import (
    APPROVED_STATE_SHA256, canonical_hash, evaluate_research_acceptance, read_evidence,
)
from memecoin_bot.v12_prearmed_policy import EntryFacts, ExitState, FrozenPrearmedPolicy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import e4_v12_axiom_paper_live as oracle


@pytest.fixture
def model():
    return read_evidence(ROOT / 'research/v12-pre-armed-100-trade-frozen-model.json')


@pytest.fixture
def policy(model):
    return FrozenPrearmedPolicy(model)


@pytest.fixture
def facts(model):
    creator = next(k for k, handles in model['creator_handles'].items()
                   if handles and model['creator_prior_e4_attempts'].get(k, 0) >= 1)
    create = model['maximum_evidence_ns'] + 60_000_000_000
    return EntryFacts(creator, model['creator_handles'][creator][0], create - 1_000_000_000,
                      create, create + 1_000_000, create, 2.0, False, 100.0, 100.0, 1.0, 1.0)


def test_exact_100_trade_override_is_research_only(model):
    state = read_evidence(ROOT / 'tests/fixtures/v12-approved-fresh-100.json')
    before = copy.deepcopy(state)
    result = evaluate_research_acceptance(model, state)
    assert len(state['ledger']) == 100
    assert canonical_hash(state) == APPROVED_STATE_SHA256
    assert result.accepted and result.reason == 'RECORDED_MANUAL_RESEARCH_PASS'
    assert result.automatic_gate_passed is False
    assert result.production_authorised is False
    assert state == before
    assert state['metrics']['acceptance_gate_passed'] is False


@pytest.mark.parametrize('mutation', ['ledger', 'waiver', 'old_sample', 'production'])
def test_override_cannot_cover_other_evidence(model, mutation):
    state = read_evidence(ROOT / 'tests/fixtures/v12-approved-fresh-100.json')
    if mutation == 'ledger':
        state['ledger'][0]['pnl_sol'] += .01
    elif mutation == 'waiver':
        state['manual_acceptance_override']['scope'] = 'all_strategies'
    elif mutation == 'old_sample':
        state['ledger'] = state['ledger'][:19]
        state['metrics']['closed_trades'] = 19
    else:
        state['production_authorised'] = True
    assert not evaluate_research_acceptance(model, state).accepted


@pytest.mark.parametrize('payload', ['{"x": 1, "x": 2}', '{"x": NaN}', '[]'])
def test_ambiguous_or_nonfinite_evidence_rejected(tmp_path, payload):
    path = tmp_path / 'bad.json'
    path.write_text(payload)
    with pytest.raises(ValueError):
        read_evidence(path)


def test_model_change_rejected_and_caller_cannot_mutate_tables(model, policy, facts):
    model['creator_handles'][facts.creator] = ['other']
    assert policy.entry(facts).accepted
    with pytest.raises(ValueError, match='fingerprint'):
        FrozenPrearmedPolicy(model)


@pytest.mark.parametrize(('field', 'value', 'reason'), [
    ('creator', 'unknown', 'UNKNOWN_CREATOR'),
    ('social_handle', 'unknown', 'NOT_PREARMED_HANDLE'),
    ('mayhem_mode', True, 'MAYHEM_OR_UNKNOWN_MODE'),
    ('mayhem_mode', None, 'MAYHEM_OR_UNKNOWN_MODE'),
    ('creator_seed_sol', 1.999999, 'CREATOR_SEED_BELOW_FLOOR'),
    ('creator_seed_sol', float('inf'), 'CREATOR_SEED_BELOW_FLOOR'),
    ('current_token_output', 64.999999, 'ENTRY_OUTPUT_GUARD_REJECTED'),
    ('current_price', 1.500001, 'ENTRY_CHASE_GUARD_REJECTED'),
    ('current_price', float('nan'), 'INVALID_QUOTE'),
    ('current_price', True, 'INVALID_QUOTE'),
    ('current_price', 10 ** 1000, 'INVALID_QUOTE'),
    ('decision_ns', 1.0, 'INVALID_TIMESTAMPS'),
])
def test_entry_rejections(policy, facts, field, value, reason):
    decision = policy.entry(replace(facts, **{field: value}))
    assert not decision.accepted and decision.reason == reason


def test_causal_availability_and_social_time_boundaries(model, policy, facts):
    assert not policy.entry(replace(facts, create_ns=model['maximum_evidence_ns'])).accepted
    assert not policy.entry(replace(facts, metadata_observed_ns=facts.decision_ns + 1)).accepted
    assert not policy.entry(replace(facts, decision_ns=facts.create_ns - 1)).accepted
    for offset in (0, 10_000_000_000):
        assert policy.entry(replace(facts, social_status_ns=facts.create_ns - offset)).accepted
    for offset in (-1, 10_000_000_001):
        assert not policy.entry(replace(facts, social_status_ns=facts.create_ns - offset)).accepted
    assert policy.entry(replace(facts, current_token_output=65., current_price=1.5)).accepted


def test_sizing_and_slot_limits(policy):
    assert policy.budget(3., 0) == pytest.approx(.3)
    assert policy.budget(2.7, 1) == pytest.approx(.27)
    assert policy.budget(3., 2) == 0
    assert policy.budget(3., 3) == 0
    assert policy.budget(float('nan'), 0) == 0
    with pytest.raises(ValueError):
        policy.budget(3., -1)


def test_quiet_market_deadline_partial_and_stale_event(policy):
    initial = ExitState(1_000_000_000, 1., 1.)
    assert not policy.exits(initial, observed_ns=2_999_999_999, timer_only=True).actions
    first = policy.exits(initial, observed_ns=1_100_000_000, price=1.15)
    assert [(a.fraction_of_original, a.reason) for a in first.actions] == [(.3, 'FIRST_TAKE')]
    assert not policy.exits(first.state, observed_ns=1_100_000_000, price=.5).actions
    # Distinct events can share a receipt timestamp; sequence establishes their order.
    second = policy.exits(first.state, observed_ns=1_100_000_000, sequence=1, price=1.02)
    assert second.state.closed
    assert second.actions[0].fraction_of_original == .7
    timeout = policy.exits(first.state, observed_ns=3_000_000_000, timer_only=True)
    assert [(a.fraction_of_original, a.reason) for a in timeout.actions] == [(.7, 'MAXIMUM_HOLD')]
    assert not policy.exits(timeout.state, observed_ns=4_000_000_000, price=10).actions


def make_simulation(model, points, fill_price=1., fill_tokens=1000.):
    create = model['maximum_evidence_ns'] + 60_000_000_000
    decision = create + 1_000_000
    fill = decision + 5_000_000
    states = [
        oracle.replay.ReserveState(create, 0, 1000., 1000., 1000., 1., 0.),
        oracle.replay.ReserveState(decision, 1, 1000., 1000., 1000., 1., 0.),
        oracle.replay.ReserveState(fill, 2, 1000., fill_tokens, fill_tokens, fill_price, 0.),
    ]
    run = oracle.replay.RunData('synthetic', {}, {}, {'mint': states}, {})
    trace = oracle.base.Trace('synthetic', 'test', 'mint', 'creator', create, 0, 'sig', False, False, True, 1)
    trace.points = [oracle.base.Point(fill + offset, 'BUY', '', '', 0, 0., price,
                                     1000., 1000., 1000., complete)
                    for offset, price, complete in points]
    candidate = oracle.Candidate('synthetic', 'mint', 'creator', 'handle', 'status', 1.,
                                 create, decision, 1, 2.)
    trade, rejection = oracle.simulate_trade(run, trace, candidate, .3, model,
                                           oracle.load_costs(model), oracle.load_policy(model))
    return trade, rejection, fill, trace, states


def test_exit_actions_match_actual_paper_simulator_on_2000_paths(model, policy):
    rng = random.Random(20260927)
    paths = [[], [(2_000_000_000, 1.15, False)], [(1, 1.2, True)],
             [(1, 1.2, False), (1, .7, False)], [(2_000_000_001, 5., False)],
             [(2_000_000_000, 1.2, False), (2_000_000_000, .7, False)]]
    prices = [.5, .7, 1., 1.02, 1.149999, 1.15, 1.5, 2., 4.]
    while len(paths) < 2000:
        offsets = sorted(rng.randrange(0, 2_500_000_000) for _ in range(rng.randrange(25)))
        paths.append([(offset, rng.choice(prices), rng.random() < .02) for offset in offsets])
    for points in paths:
        trade, rejection, fill, trace, _ = make_simulation(model, points)
        assert rejection is None
        state = ExitState(fill, 1., 1.)
        actions = []
        closed_ns = None
        for sequence, point in enumerate(trace.points):
            update = policy.exits(state, observed_ns=point.timestamp_ns, price=point.price_sol,
                                  complete=point.complete, sequence=sequence)
            actions.extend(update.actions)
            state = update.state
            if state.closed:
                closed_ns = min(point.timestamp_ns, fill + policy.hold_ns)
                break
        if not state.closed:
            update = policy.exits(state, observed_ns=fill + policy.hold_ns, timer_only=True)
            actions.extend(update.actions)
            closed_ns = fill + policy.hold_ns
        assert len(actions) == trade['exit_transactions'], points
        assert actions[-1].reason == trade['exit_reason'], points
        assert closed_ns == trade['exit_ns'], points
        assert sum(action.fraction_of_original for action in actions) == pytest.approx(1.)


def test_quote_guards_match_paper_simulator(model, policy, facts):
    for price in (.5, 1., 1.5, math.nextafter(1.5, math.inf), 2.):
        for tokens in (100., 649., 650., 651., 1000., 2000.):
            trade, rejection, _, _, states = make_simulation(model, [], price, tokens)
            curve_input = oracle.curve_input_for_budget(.3, oracle.load_costs(model))
            expected = oracle.replay.buy_tokens(curve_input, states[1])
            current = oracle.replay.buy_tokens(curve_input, states[2])
            result = policy.entry(replace(facts, expected_token_output=expected,
                                          current_token_output=current, current_price=price))
            assert result.accepted == (trade is not None), (price, tokens, rejection)

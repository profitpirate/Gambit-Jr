import importlib.util
import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from memecoin_bot.v12_prearmed_timing import Timings
from memecoin_bot.v12_prearmed_diagnostics import Diagnostics
from memecoin_bot.v12_prearmed_policy import EntryFacts, FrozenPrearmedPolicy
from memecoin_bot.v12_prearmed_replay import ReceiptReplay, replay_decision
from memecoin_bot.v12_prearmed_evidence import FROZEN_MODEL_SHA256
from memecoin_bot.v12_prearmed_health import assessment

ROOT = Path(__file__).resolve().parents[1]
MODEL = json.loads((ROOT/'research/v12-pre-armed-100-trade-frozen-model.json').read_text())


def test_timing_is_bounded_and_ignores_untrusted_values():
    stats = Timings()
    for index in range(1000):
        stats.observe('candidate_queue_ms', index, index+1)
        stats.observe('url?secret', index, index)
    for value in (-1, float('nan'), float('inf'), True, 'secret'):
        stats.observe('candidate_queue_ms', value, 1001)
    row = stats.snapshot(1001)['metrics']['candidate_queue_ms']
    assert row == dict(count=1000, samples=512, latest_ns=1000, last=999., max=999., p50=743., p95=974., p99=994.)
    assert len(stats.samples) == 1


def test_slot_lag_requires_sustained_fresh_reference_and_recovers():
    stats = Timings()
    for seconds in range(32):
        now = (seconds+100)*10**9
        stats.head(100+seconds, now)
        stats.feed({'_feed_slot': 90+seconds}, now, now)
    assert stats.snapshot(now)['feed']['sustained_lag']
    assert not stats.snapshot(now+6*10**9)['feed']['sustained_lag']
    stats.feed({'_feed_slot': 131}, now, now)
    assert not stats.snapshot(now)['feed']['sustained_lag']
    stats.feed({'_feed_slot': 121}, now, now)
    assert not stats.snapshot(now)['feed']['sustained_lag']


def test_observation_preserves_coarse_chain_clock_and_no_negative_metrics():
    stats = Timings()
    stats.feed({'anchor_event':'CreateEvent','timestamp':100,
                '_feed_received_ns':100_500_000_000, '_feed_decoded_ns':100_501_000_000},
                100_500_000_000, 100_503_000_000)
    result = stats.snapshot(100_503_000_000)
    assert result['metrics']['create_age_approx_ms']['last'] == 500
    assert result['metrics']['feed_decode_ms']['last'] == 1
    assert result['metrics']['feed_dispatch_ms']['last'] == 2
    assert result['feed']['chain_age_resolution_ms'] == 1000
    assert result['feed']['lag_slots'] is None


def test_quote_and_build_timings_are_distinct_without_changing_payload():
    events=[];d=Diagnostics(lambda *x: events.append(x),lambda:10**18,'run')
    d.record('CANDIDATE_PROGRESS',stage='quote',reason='QUOTE_AVAILABLE',builder_total_ms=123)
    d.record('EXECUTION_PROGRESS',stage='sign',reason='BUILD_AND_SIMULATION_PASSED',builder_total_ms=456)
    result=d.snapshot()['timing']['metrics']
    assert result['quote_total_ms']['last']==123 and result['build_total_ms']['last']==456
    assert events[0][1]['builder_total_ms']==123


def test_stale_status_cannot_claim_live_feed_lag():
    report={'service':{'ActiveState':'active'},'status_age_seconds':1,'status':{
        'diagnostics':{'timing':{'feed':{'sustained_lag':True,'reference_fresh':True}}}}}
    warning='Incoming feed is persistently behind the confirmed chain'
    assert warning in assessment(report,[])
    report['status_age_seconds']=11
    assert warning not in assessment(report,[])


def test_all_frozen_creators_replay_same_production_rules_at_boundaries():
    policy=FrozenPrearmedPolicy(MODEL)
    now=MODEL['maximum_evidence_ns']+100_000_000_000
    cases=[({},True),({'creator_seed_sol':1.999999999},False),
           ({'current_price':1.500001},False),({'current_price':1.5},True),
           ({'current_token_output':64.999},False),({'current_token_output':65},True),
           ({'social_status_ns':now-10_000_000_001},False),
           ({'metadata_observed_ns':now+1},False),({'mayhem_mode':True},False)]
    for creator,handles in MODEL['creator_handles'].items():
        if not policy.creator_known(creator):
            continue
        base=EntryFacts(creator,handles[0],now-1_000_000_000,now,now,now,2,False,100,100,1,1)
        for delta,accepted in cases:
            facts=replace(base,**delta)
            decision=policy.entry(facts)
            assert decision.accepted is accepted
            payload={'model_sha256':FROZEN_MODEL_SHA256,'decision_accepted':decision.accepted,
                     'reason':decision.reason, **{'fact_'+k:v for k,v in asdict(facts).items()}}
            replay=replay_decision(payload,policy)
            assert replay['matches_recorded_decision'] and replay['pnl_lamports'] is None
            assert replay['execution']=='NOT_ESTABLISHED_BY_POLICY'


def test_missing_inputs_cannot_be_backfilled_with_future_values():
    policy=FrozenPrearmedPolicy(MODEL)
    assert replay_decision({'model_sha256':FROZEN_MODEL_SHA256},policy)['state']=='INSUFFICIENT_EVIDENCE'
    assert replay_decision({},policy)['reason']=='MODEL_MISMATCH'


def receipt(signature, cash, tokens, *, failed=False, fill_ns=10_000_000_000):
    return dict(signature=signature,cash_delta=cash,token_delta=tokens,
                finalized=True,failed=failed,fill_ns=fill_ns)


def test_receipts_include_fees_partial_sales_and_actual_fill_clock():
    book=ReceiptReplay(1_000_000_000)
    book.apply(receipt('buy',-102_000_000,100))
    assert book.snapshot()['hold_deadline_ns']==12_000_000_000
    partial=receipt('partial',70_000_000,-50)
    book.apply(partial)
    assert book.snapshot()['realized_lamports'] is None
    assert book.apply(partial)=='ALREADY_APPLIED'
    book.apply(receipt('failedsale',-100_000,0,failed=True))
    book.apply(receipt('close',20_000_000,-50))
    assert book.snapshot()['realized_lamports']==-12_100_000


def test_unknown_outcome_never_becomes_a_simulated_winner():
    book=ReceiptReplay(1_000_000_000)
    book.apply(receipt('buy',-100,10))
    book.apply({'finalized':False})
    book.apply(receipt('close',1000,-10))
    assert book.snapshot()['state']=='UNKNOWN' and book.snapshot()['realized_lamports'] is None


def test_replay_rejects_impossible_fills_and_conflicting_receipts_atomically():
    book=ReceiptReplay(100)
    for row in [receipt('excess',-101,10),receipt('failed',-10,10,failed=True),receipt('short',10,-1)]:
        with pytest.raises(ValueError):book.apply(row)
        assert book.snapshot()['cash_lamports']==100
    book.apply(receipt('buy',-50,10))
    with pytest.raises(ValueError,match='CONFLICTING'):book.apply(receipt('buy',-49,10))
    assert book.snapshot()['cash_lamports']==50


def load_deployment(name):
    spec=importlib.util.spec_from_file_location('reliability_test_'+name,ROOT/'deployment'/name)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def test_sanitized_snapshot_preserves_metrics_not_arbitrary_strings(tmp_path):
    collector=load_deployment('gambit-monitor-collector.py')
    d=Diagnostics(lambda *x:None,lambda:10**18,'run')
    d.timings.observe('candidate_queue_ms',12,10**18)
    status={'diagnostics':d.snapshot()}
    status['diagnostics']['timing']['metrics']['https://secret']={'p50':'secret'}
    status['diagnostics']['timing']['metrics']['candidate_queue_ms']['url']='secret'
    file=tmp_path/'status.json';file.write_text(json.dumps(status))
    safe=collector.status_snapshot(file.resolve())
    assert safe['diagnostics']['timing']['metrics']['candidate_queue_ms']['p50']==12
    assert 'secret' not in json.dumps(safe)


def test_outage_ping_is_empty_bounded_and_cannot_follow_redirect():
    module=load_deployment('gambit-outage-heartbeat.py')
    class Response:
        status=200
        def __enter__(self):return self
        def __exit__(self,*args):pass
    class Opener:
        def open(self,request,timeout):
            assert request.data==b'' and request.method=='POST' and timeout==5
            return Response()
    module.ping('https://hc-ping.com/test',Opener())
    assert module.NoRedirect().redirect_request(None,None,None,None,None,None) is None


def test_outage_errors_never_print_ping_secret(capsys,monkeypatch):
    module=load_deployment('gambit-outage-heartbeat.py')
    def reject():raise RuntimeError('https://hc-ping.com/SECRET')
    monkeypatch.setattr(module,'load_url',reject)
    assert module.main()==1
    assert capsys.readouterr().out=='VPS_HEARTBEAT_FAILED\n'

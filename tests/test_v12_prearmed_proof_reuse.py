"""Offline provenance/failure tests; these are not transaction execution proofs."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('reuse_under_test',ROOT/'deployment/execution-proof-reuse.py')
reuse=importlib.util.module_from_spec(spec);spec.loader.exec_module(reuse)

@pytest.fixture
def evidence():
    installed={'image_id':'new-image','archive_sha256':'a'*64}
    values={'E4_WALLET_PUBLIC_KEY':'wallet','E4_PRIMARY_RPC_URL':'test-provider','GAMBIT_POSITION_PERCENT':'10'}
    proof={'image_id':'tested-image','wallet':'wallet','state':'COMPLETE','residual_tokens_raw':0,
           'buy_signature':'recorded-buy','sell_signature':'recorded-sell','pending_orders':0,'open_positions':0}
    digest=reuse.config_hash(values)
    record={'schema':1,'basis':'UNCHANGED_EXECUTION_STRICTER_CREATOR_RULES','reviewed_ns':100,
            'candidate_archive_sha256':'a'*64,'candidate_image_id':'new-image','wallet':'wallet',
            'config_sha256':digest,'baseline_config_sha256':digest,'baseline_image_id':'tested-image',
            'dependencies_equal':True,'base_layers_equal':True,'dependency_sha256':'dep','baseline_dependency_sha256':'dep',
            'independent_receipt_checks':[{'provider':p,'buy_signature':'recorded-buy','sell_signature':'recorded-sell',
              'finalized':True,'residual_tokens_raw':0,'observed_ns':100} for p in ('A','B')],
            'full_suite':{'image_id':'new-image','exit_code':0,'running':False,'failures':0,'errors':0,'tests':1200}}
    return installed,values,proof,record

def check(e,now=100):
    installed,values,proof,record=e
    reuse.validate_evidence(record,proof,installed=installed,wallet='wallet',values=values,now_ns=now)

def test_matching_evidence_keeps_the_original_test_image_identity(evidence):
    before=copy.deepcopy(evidence[2]);check(evidence)
    assert evidence[2]==before and evidence[2]['image_id']=='tested-image'

@pytest.mark.parametrize('field,value',[
    ('wallet','other-wallet'),('candidate_image_id','wrong-image'),('candidate_archive_sha256','b'*64),
    ('config_sha256','wrong'),('baseline_config_sha256','wrong'),('dependencies_equal',False),
    ('base_layers_equal',False),('dependency_sha256','other-dependencies'),('reviewed_ns',101),
    ('reviewed_ns',-4_000_000_000_000),('basis','relabeled-canary'),('schema',2),
])
def test_changed_stale_or_unreviewed_evidence_is_rejected(evidence,field,value):
    evidence[3][field]=value
    with pytest.raises(ValueError):check(evidence)

@pytest.mark.parametrize('field,value',[
    ('state','UNKNOWN'),('wallet','other-wallet'),('residual_tokens_raw',1),('buy_signature',''),
    ('sell_signature',''),('pending_orders',1),('open_positions',1),('image_id','unverified-image')])
def test_ambiguous_or_incomplete_prior_trade_is_rejected(evidence,field,value):
    evidence[2][field]=value
    with pytest.raises(ValueError):check(evidence)

@pytest.mark.parametrize('change',[{'finalized':False},{'observed_ns':101},{'residual_tokens_raw':1},
                                   {'buy_signature':'wrong'},{'provider':'A'}])
def test_both_independent_finalized_receipts_are_required(evidence,change):
    evidence[3]['independent_receipt_checks'][1].update(change)
    with pytest.raises(ValueError):check(evidence)

@pytest.mark.parametrize('change',[{'image_id':'old-image'},{'exit_code':1},{'failures':1},{'running':True},{'tests':0}])
def test_current_image_must_pass_its_own_suite(evidence,change):
    evidence[3]['full_suite'].update(change)
    with pytest.raises(ValueError):check(evidence)

def test_provider_wallet_or_sizing_change_invalidates_reuse(evidence):
    for key in evidence[1]:
        changed=copy.deepcopy(evidence);changed[1][key]='different'
        with pytest.raises(ValueError):check(changed)

def manifest(root,changes=None):
    files={'src/memecoin_bot/v12_prearmed_runtime.py':b'unchanged execution\n',
           'research/frozen-model.json':b'unchanged strategy\n',
           reuse.LIBRARY:json.dumps({'promoted':[]}).encode(),
           reuse.ACTIVATION:json.dumps({'review_mode':'ORIGINAL_FROZEN_RULES_ONLY','preserve_entry_and_risk_rules':True,
              'library_sha256':reuse.sha(b'{"promoted":[]}')}).encode(),
           reuse.REVIEWED_PRESENTATION:b'def status_text(x):\n return "old"\ndef send_trade(x):\n return x\n'}
    files.update(changes or {})
    for name,raw in files.items():
        p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(raw)
    (root/'release-manifest.json').write_text(json.dumps({'files':{n:reuse.sha(raw) for n,raw in files.items()}}))

def test_only_formatter_and_test_changes_are_allowed(tmp_path):
    old,new=tmp_path/'old',tmp_path/'new';manifest(old)
    manifest(new,{reuse.REVIEWED_PRESENTATION:b'def status_text(x):\n return "original rules"\ndef send_trade(x):\n return x\n',
                  'tests/new.py':b'assert True\n'})
    assert reuse.validate_release_delta(old,new,{})['changed_files']==[reuse.REVIEWED_PRESENTATION,'tests/new.py']

@pytest.mark.parametrize('name',[ 'src/memecoin_bot/v12_prearmed_runtime.py','research/frozen-model.json',
                                  'tools/e4-builder/package-lock.json','deployment/prearmed-control.py'])
def test_unreviewed_execution_strategy_dependency_or_guard_changes_block_reuse(tmp_path,name):
    old,new=tmp_path/'old',tmp_path/'new';manifest(old);manifest(new,{name:b'changed'})
    with pytest.raises(ValueError):reuse.validate_release_delta(old,new,{})

def test_discord_command_changes_are_not_presentation_changes(tmp_path):
    old,new=tmp_path/'old',tmp_path/'new';manifest(old)
    manifest(new,{reuse.REVIEWED_PRESENTATION:b'def status_text(x):\n return "old"\ndef send_trade(x):\n return x+1\n'})
    with pytest.raises(ValueError,match='COMMAND_CODE'):reuse.validate_release_delta(old,new,{})

def test_manifest_tampering_is_rejected(tmp_path):
    old,new=tmp_path/'old',tmp_path/'new';manifest(old);manifest(new)
    (new/'src/memecoin_bot/v12_prearmed_runtime.py').write_text('tampered')
    with pytest.raises(ValueError,match='INTEGRITY'):reuse.validate_release_delta(old,new,{})

def test_added_promotion_is_not_eligible_for_this_reuse_route(tmp_path):
    old,new=tmp_path/'old',tmp_path/'new';manifest(old);manifest(new,{reuse.LIBRARY:b'{"promoted":["creator"]}'})
    with pytest.raises(ValueError,match='ORIGINAL_CREATOR'):reuse.validate_release_delta(old,new,{})


def io_evidence(evidence):
    record=evidence[3]
    record.update(basis=reuse.IO_BASIS,source_review_basis=reuse.IO_BASIS,candidate_manifest_sha256='manifest')
    record['reliability_review']={key:True for key in (
        'strategy_selection_sizing_risk_unchanged','signing_broadcast_recovery_unchanged',
        'transaction_instructions_equal','rpc_methods_read_only',
        'blockhash_bank_and_freshness_guards','provider_order_and_cooldown_preserved',
        'runtime_equal_except_observation','hold_clock_unchanged')}
    record['reliability_review'].update(baseline_archive_sha256=reuse.RELIABILITY_BASELINE,
        candidate_archive_sha256='a'*64,builder_tests_passed=37,runtime_tests_passed=88)
    record['entry_speed_review']={key:True for key in (
        'transaction_logic_equal', 'signing_broadcast_recovery_unchanged',
        'runtime_equal_except_bounded_quote_prefetch', 'strategy_and_safety_conditions_unchanged',
        'builder_equal_except_unused_sdk_construction', 'feed_equal_except_cooperative_yield',
        'prefetch_is_unsigned_quote_only')}
    record['entry_speed_review'].update(prefetch_max_completed_age_ms=20,entry_speed_tests_passed=13,
        candidate_archive_sha256=reuse.RELIABILITY_BASELINE)
    record['expiry_recovery_review']={'execute_method_ast_equal':True,'build_and_sign_unchanged':True,
        'automatic_retry_added':False,'expiry_tests_passed':17,'candidate_archive_sha256':reuse.RELIABILITY_BASELINE,
        'incident_evidence_sha256':'b'*64}
    record['unsigned_io_review']={'candidate_image_id':'new-image','baseline_image_id':'tested-image',
        'candidate_manifest_sha256':'manifest','transaction_logic_equal':True,
        'unsigned_transaction_bytes_equal':True,'all_signatures_zero':True,'simulation_passed':True,
        'transactions_sent':0,'signer_used':False,'observed_ns':100}
    return evidence


@pytest.mark.parametrize('change',[{'transaction_logic_equal':False},
    {'strategy_and_safety_conditions_unchanged':False},{'prefetch_is_unsigned_quote_only':False},
    {'prefetch_max_completed_age_ms':21},{'entry_speed_tests_passed':12},{'candidate_archive_sha256':'other'}])
def test_entry_speed_release_requires_its_own_review(evidence,change):
    value=io_evidence(evidence);value[3]['entry_speed_review'].update(change)
    with pytest.raises(ValueError,match='ENTRY_SPEED'):check(value)


def test_reviewed_io_keeps_old_receipt_and_requires_unsigned_evidence(evidence):
    value=io_evidence(evidence);before=copy.deepcopy(value[2]);check(value)
    assert value[2]==before
    del value[3]['unsigned_io_review']
    with pytest.raises(ValueError,match='UNSIGNED_IO'):check(value)


@pytest.mark.parametrize('change',[{'execute_method_ast_equal':False},{'build_and_sign_unchanged':False},
    {'automatic_retry_added':True},{'expiry_tests_passed':16},{'candidate_archive_sha256':'wrong'},
    {'incident_evidence_sha256':''}])
def test_expiry_release_requires_its_own_bound_review(evidence,change):
    value=io_evidence(evidence);value[3]['expiry_recovery_review'].update(change)
    with pytest.raises(ValueError,match='EXPIRY_RECOVERY'):check(value)


@pytest.mark.parametrize('field,value',[
    ('candidate_image_id','different'),('baseline_image_id','different'),
    ('candidate_manifest_sha256','different'),('transaction_logic_equal',False),
    ('unsigned_transaction_bytes_equal',False),('all_signatures_zero',False),
    ('simulation_passed',False),('transactions_sent',1),('signer_used',True),
    ('observed_ns',101),('observed_ns',-4_000_000_000_000)])
def test_io_review_failure_cannot_reuse_proof(evidence,field,value):
    value_evidence=io_evidence(evidence);value_evidence[3]['unsigned_io_review'][field]=value
    with pytest.raises(ValueError,match='UNSIGNED_IO'):check(value_evidence)


def test_io_source_pair_is_exact_and_atomic(tmp_path,monkeypatch):
    name='src/memecoin_bot/v12_prearmed_runtime.py';extra='src/memecoin_bot/new_io.py'
    monkeypatch.setattr(reuse,'IO_SOURCE_PAIRS',{
        name:[reuse.sha(b'unchanged execution\n'),reuse.sha(b'reviewed IO')],
        extra:[None,reuse.sha(b'new IO')]})
    old,new=tmp_path/'old',tmp_path/'new';manifest(old)
    manifest(new,{name:b'reviewed IO',extra:b'new IO'})
    assert reuse.validate_release_delta(old,new,{})['source_review_basis']==reuse.IO_BASIS
    manifest(new,{name:b'reviewed IO',extra:b'changed IO'})
    with pytest.raises(ValueError,match='IO_SOURCE'):reuse.validate_release_delta(old,new,{})
    manifest(new,{name:b'arbitrary execution',extra:b'new IO'})
    with pytest.raises(ValueError,match='IO_SOURCE'):reuse.validate_release_delta(old,new,{})


def test_review_pins_match_shipped_source():
    for name,pair in reuse.IO_SOURCE_PAIRS.items():
        assert reuse.sha((ROOT/name).read_bytes().replace(b'\r\n',b'\n'))==pair[1],name


@pytest.mark.parametrize('change',[{'transaction_instructions_equal':False},{'rpc_methods_read_only':False},
    {'runtime_equal_except_observation':False},{'baseline_archive_sha256':'wrong'},
    {'candidate_archive_sha256':'wrong'},{'builder_tests_passed':36},{'runtime_tests_passed':87}])
def test_read_concurrency_requires_its_own_review(evidence,change):
    value=io_evidence(evidence);value[3]['reliability_review'].update(change)
    with pytest.raises(ValueError,match='RELIABILITY'):check(value)

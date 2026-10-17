"""Validate a separately recorded reuse assessment; never rewrite a live-test receipt.

Creator restrictions and presentation may differ. A separately reviewed IO/logging
release requires exact source-pair pins plus unsigned same-state transaction
equivalence. Dependencies, wallet and non-release settings must match. The caller still requires fresh full readiness and an explicit start.
"""
import ast
import hashlib
import json
import re
import time
from pathlib import Path

VOLATILE = {'E4_LIVE', 'V12_LIVE_TRADING_ENABLED', 'V12_LIVE_ARM_TOKEN',
            'V12_LIVE_ARM_TOKEN_SHA256', 'GAMBIT_RECOVERY_ONLY',
            'V12_INTEGRITY_SIGNATURE', 'V12_INTEGRITY_PUBLIC_KEY'}
REVIEWED_PRESENTATION = 'src/memecoin_bot/v12_prearmed_discord.py'
REVIEWED_GUARDS = {'deployment/prearmed-control.py', 'deployment/execution-proof-reuse.py'}
LIBRARY = 'models/e4/v12-creator-library.json'
ACTIVATION = 'models/e4/v12-prearmed-library-activation.json'

# Exact reviewed release only. No wildcard allowance for execution changes.
IO_BASIS = 'UNCHANGED_TRANSACTIONS_REVIEWED_READ_CONCURRENCY'
RELIABILITY_BASELINE = '89311b81d9b95a7b0b613c5cd340eab3760963e6c9c8c91a0d3b1402edb4a377'
IO_SOURCE_PAIRS = {'src/memecoin_bot/v12_prearmed_io.py': ['e8651d2f69de46b3e43d65ea92b92583c912fb42954c7abff3fd060570bab966', 'ff669433dd51847e9587b4ebf9083d8b2073ca427ad5399e28e6d3b888a68520'], 'src/memecoin_bot/v12_prearmed_runtime.py': ['ebe0ca431129bd9ab2e59cf77a0903743a4bd45ca098292e82dfe9b563ea43f3', '3efff4e09085d775d7c53bea789ca987a7ccc0a8a9144311de706354ed44d5dd'], 'src/memecoin_bot/v12_prearmed_diagnostics.py': ['9bbe390a8b6ae4c9a93fc5c4a04de744d41bde518addacbed33d6c0659157554', 'c832aa959854054f6f872d94ce285f8b16cee0dc278cc79719d8f4dfd89383b8'], 'src/memecoin_bot/v12_prearmed_metadata.py': [None, '6dec2390afeeb7df87446a1d63a997cf7f4203ee2d53efe8253dd406633bb4c0'], 'src/memecoin_bot/v12_prearmed_operator_backend.py': ['277b45a2b2957281063f65f0fd61910258db3635effdce57c99e411069d2bb6c', '5dcf6e5875ad474863e3f95dbae2f69e9f9530ed6713abec7b4ce5d50c00fc0e'], 'tools/e4-builder/frozen-prearmed.mjs': ['276c1430f200297e4df32c0a79cdf9fce2ffd96cc83082a58acbe0bd673d5916', '362677f0b1f1ced47fdf9751bbcee46648aa63335a4e6d13514d6656a64c90f5'], 'tools/e4-builder/frozen-prearmed.test.mjs': ['0b7ce4ca01ecf4fcdf06b5b7b83452daf894e7404d44fa117f16e84fa8eadc56', '3d1949bbc9b8b05d82ad4803d2c472605eaae68affae15d0a5a8cd7896501c2f'], 'deployment/gambit-monitor-collector.py': ['17d450465c7f4febfc9f37cc69d75e275a6950043248fb9b18bb72db31877c3a', 'a744d4bfad4b947f23a60bbe1e468452e9c9bd49e1347d5c0f32e7da3b8fc6ba'], 'src/memecoin_bot/v12_prearmed.py': ['e2df38e8bf6238ce51769d9154e12af8e80b372309ef92a13f5baae3ebbe7871', 'e09d76e45a8995674378f8e559c5f9bcad225bdcd773831642f60306b1ce2504'], 'src/memecoin_bot/v12_prearmed_notices.py': ['f964e01e1a6315390031fc38de337235c3f1af3968ea0ac123ec9ba10a981527', 'a16f4a6e9a42ffca90943e9574dc92dd9b965d5fab43dfa01dfc078f6d85fe7c'], 'src/memecoin_bot/v12_prearmed_health.py': ['e4b3621c4d13d20156a849175e36f26a51af91ceb32304e512e640304d0410f5', '3ca8791ba6a1b06846fa03c2d80c6eb69d70a5de6c3cf40b1fe245ac37913ccb'], 'src/memecoin_bot/v12_prearmed_reporting.py': [None, '99f0d0ad794947d710870fe943b1f1a9368f777cd69ffb56fd318b61c52bb6b8'], 'src/memecoin_bot/v12_prearmed_expiry.py': [None, '6b85a237264d03b77175ec147842c358876d5cf1ec8c85d8f6491c74e27480ad'], 'src/memecoin_bot/v12_prearmed_timing.py': [None, 'd92c032d3cdc4fabcc6f257da8ff05dcd92d6614d3d27d2c620deb9f15c63905'], 'src/memecoin_bot/v12_prearmed_replay.py': [None, 'dd7f147ada0ebe417a72221c9b4dacdcddb60690c9b82fb2e8083d2f3c6d5d5b'], 'tools/e4-builder/rpc-read-budget.mjs': [None, '306957c3a0e98d977666354a4694a6ee2139bbd13b899106f97fdaa563fc970c'], 'tools/e4-builder/rpc-read-budget.test.mjs': [None, '9c92fe4d2c93aab84041d22154d799aae224177c2764c5c865396ad80d925683'], 'deployment/gambit-outage-heartbeat.py': [None, 'f99e0b1c5460982c354e1ab518872754d814c51945b8e9fe6ab6ce7f304baa01'], 'deployment/gambit-outage-heartbeat.service': [None, '9343293133c4394b646ba38dfa66160e9a2a7c2a398c598e407dbdc8d0c99b19'], 'deployment/gambit-outage-heartbeat.timer': [None, '41c6aa4259d2dfac66b467a6f1d654a8673c6db748b1025d1c93e77d4ad35343']}

def sha(raw):
    return hashlib.sha256(raw).hexdigest()

def config_hash(values):
    return sha(json.dumps({k:v for k,v in values.items() if k not in VOLATILE}, sort_keys=True).encode())

def checked_manifest(root):
    root = root.resolve()
    raw = (root / 'release-manifest.json').read_bytes()
    manifest = json.loads(raw)
    for name, digest in manifest['files'].items():
        path = root / name
        if not path.resolve().is_relative_to(root) or path.is_symlink() or sha(path.read_bytes()) != digest:
            raise ValueError('REUSE_RELEASE_INTEGRITY_FAILED')
    return manifest['files'], sha(raw)

def presentation_ast(raw):
    tree = ast.parse(raw)
    tree.body = [n for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 or n.name not in {'status_text', 'library_text'}]
    return ast.dump(tree, include_attributes=False)

def validate_release_delta(baseline, candidate, approved_guard_hashes):
    old, old_manifest = checked_manifest(baseline)
    new, new_manifest = checked_manifest(candidate)
    changed = {k for k in old.keys() | new.keys() if old.get(k) != new.get(k)}
    io_changed = bool(changed & IO_SOURCE_PAIRS.keys())
    if io_changed and any([old.get(k), new.get(k)] != pair for k, pair in IO_SOURCE_PAIRS.items()):
        raise ValueError('REUSE_REVIEWED_IO_SOURCE_MISMATCH')
    for name in changed:
        if io_changed and name in IO_SOURCE_PAIRS:
            continue
        if name.startswith('tests/'):
            continue
        if name in (LIBRARY, ACTIVATION):
            continue
        if name == REVIEWED_PRESENTATION:
            if presentation_ast((baseline/name).read_bytes()) != presentation_ast((candidate/name).read_bytes()):
                raise ValueError('REUSE_DISCORD_COMMAND_CODE_CHANGED')
            continue
        if name in REVIEWED_GUARDS and name in new and new[name] == approved_guard_hashes.get(name):
            continue
        raise ValueError('REUSE_EXECUTION_OR_DEPENDENCY_SOURCE_CHANGED')
    # This release-scoped reuse route only accepts withdrawal of all extra
    # promotions. Any addition or changed frozen model requires a new review.
    library = json.loads((candidate/LIBRARY).read_text())
    activation = json.loads((candidate/ACTIVATION).read_text())
    if library.get('promoted') != [] or activation.get('review_mode') != 'ORIGINAL_FROZEN_RULES_ONLY':
        raise ValueError('REUSE_REQUIRES_ORIGINAL_CREATOR_RULES')
    digest = sha(json.dumps(library, sort_keys=True, separators=(',', ':'), allow_nan=False).encode())
    if activation.get('library_sha256') != digest or activation.get('preserve_entry_and_risk_rules') is not True:
        raise ValueError('REUSE_CREATOR_PIN_INVALID')
    return {'changed_files': sorted(changed), 'baseline_manifest_sha256': old_manifest,
            'candidate_manifest_sha256': new_manifest,
            'source_review_basis': IO_BASIS if io_changed else 'UNCHANGED_EXECUTION_STRICTER_CREATOR_RULES'}

def validate_evidence(record, proof, *, installed, wallet, values, now_ns=None):
    now_ns = time.time_ns() if now_ns is None else now_ns
    if record.get('schema') != 1 or record.get('basis') not in ('UNCHANGED_EXECUTION_STRICTER_CREATOR_RULES', IO_BASIS):
        raise ValueError('REUSE_ASSESSMENT_INVALID')
    if record.get('basis') == IO_BASIS:
        reliability = record.get('reliability_review', {})
        if (any(reliability.get(key) is not True for key in (
                'strategy_selection_sizing_risk_unchanged','signing_broadcast_recovery_unchanged',
                'transaction_instructions_equal','rpc_methods_read_only',
                'blockhash_bank_and_freshness_guards','provider_order_and_cooldown_preserved',
                'runtime_equal_except_observation','hold_clock_unchanged'))
                or reliability.get('baseline_archive_sha256') != RELIABILITY_BASELINE
                or reliability.get('candidate_archive_sha256') != installed['archive_sha256']
                or reliability.get('builder_tests_passed',0) < 37
                or reliability.get('runtime_tests_passed',0) < 88):
            raise ValueError('REUSE_RELIABILITY_REVIEW_REQUIRED')
        # These are retained prior-release assessments, not relabelled as new tests.
        review = record.get('unsigned_io_review', {})
        speed = record.get('entry_speed_review', {})
        if (any(speed.get(key) is not True for key in (
                'transaction_logic_equal', 'signing_broadcast_recovery_unchanged',
                'runtime_equal_except_bounded_quote_prefetch', 'strategy_and_safety_conditions_unchanged',
                'builder_equal_except_unused_sdk_construction', 'feed_equal_except_cooperative_yield',
                'prefetch_is_unsigned_quote_only'))
                or speed.get('prefetch_max_completed_age_ms') != 20
                or speed.get('entry_speed_tests_passed', 0) < 13
                or speed.get('candidate_archive_sha256') != RELIABILITY_BASELINE):
            raise ValueError('REUSE_ENTRY_SPEED_REVIEW_REQUIRED')
        expiry = record.get('expiry_recovery_review', {})
        if (expiry.get('execute_method_ast_equal') is not True
                or expiry.get('build_and_sign_unchanged') is not True
                or expiry.get('automatic_retry_added') is not False
                or expiry.get('expiry_tests_passed', 0) < 17
                or expiry.get('candidate_archive_sha256') != RELIABILITY_BASELINE
                or not re.fullmatch('[0-9a-f]{64}', expiry.get('incident_evidence_sha256', ''))):
            raise ValueError('REUSE_EXPIRY_RECOVERY_REVIEW_REQUIRED')
        if (record.get('source_review_basis') != IO_BASIS
                or review.get('candidate_image_id') != installed['image_id']
                or review.get('baseline_image_id') != record.get('baseline_image_id')
                or review.get('candidate_manifest_sha256') != record.get('candidate_manifest_sha256')
                or review.get('transaction_logic_equal') is not True
                or review.get('unsigned_transaction_bytes_equal') is not True
                or review.get('all_signatures_zero') is not True
                or review.get('simulation_passed') is not True
                or review.get('transactions_sent') != 0
                or review.get('signer_used') is not False
                or not 0 <= now_ns-review.get('observed_ns', 0) <= 3_600_000_000_000):
            raise ValueError('REUSE_UNSIGNED_IO_REVIEW_REQUIRED')
    if not isinstance(record.get('reviewed_ns'), int) or not 0 <= now_ns-record['reviewed_ns'] <= 3_600_000_000_000:
        raise ValueError('REUSE_ASSESSMENT_STALE')
    if (record.get('candidate_archive_sha256') != installed['archive_sha256']
            or record.get('candidate_image_id') != installed['image_id']
            or record.get('wallet') != wallet
            or record.get('config_sha256') != config_hash(values)):
        raise ValueError('REUSE_RELEASE_WALLET_OR_CONFIG_MISMATCH')
    if (proof.get('image_id') != record.get('baseline_image_id') or proof.get('wallet') != wallet
            or proof.get('state') != 'COMPLETE' or proof.get('residual_tokens_raw') != 0
            or not proof.get('buy_signature') or not proof.get('sell_signature')
            or proof.get('pending_orders') not in (0, []) or proof.get('open_positions') not in (0, [])):
        raise ValueError('REUSE_ORIGINAL_EXECUTION_PROOF_INVALID')
    if (record.get('dependencies_equal') is not True or record.get('base_layers_equal') is not True
            or not record.get('dependency_sha256') or record.get('baseline_dependency_sha256') != record['dependency_sha256']):
        raise ValueError('REUSE_RUNTIME_DEPENDENCIES_CHANGED')
    if record.get('baseline_config_sha256') != record['config_sha256']:
        raise ValueError('REUSE_BASELINE_CONFIG_CHANGED')
    receipts = record.get('independent_receipt_checks', [])
    if len(receipts) != 2 or len({r.get('provider') for r in receipts}) != 2:
        raise ValueError('REUSE_INDEPENDENT_RECEIPTS_REQUIRED')
    for receipt in receipts:
        if (receipt.get('buy_signature') != proof['buy_signature'] or receipt.get('sell_signature') != proof['sell_signature']
                or receipt.get('finalized') is not True or receipt.get('residual_tokens_raw') != 0
                or not 0 <= now_ns-receipt.get('observed_ns', 0) <= 3_600_000_000_000):
            raise ValueError('REUSE_RECEIPT_RECHECK_FAILED')
    suite = record.get('full_suite', {})
    if (suite.get('image_id') != installed['image_id'] or suite.get('exit_code') != 0
            or suite.get('running') is not False or suite.get('failures') != 0 or suite.get('errors') != 0
            or suite.get('tests', 0) < 1188):
        raise ValueError('REUSE_EXACT_IMAGE_TESTS_REQUIRED')

def validate_reuse(stage, installed, wallet, values):
    stage = Path(stage)
    record = json.loads((stage/'prearmed-data/execution-proof-reuse.json').read_text())
    proof_raw = (stage/'prearmed-data/execution-test-pass.json').read_bytes()
    proof = json.loads(proof_raw)
    if sha(proof_raw) != record.get('original_proof_sha256'):
        raise ValueError('REUSE_ORIGINAL_PROOF_CHANGED')
    validate_evidence(record, proof, installed=installed, wallet=wallet, values=values)
    baseline_digest = record.get('baseline_archive_sha256', '')
    if not re.fullmatch('[0-9a-f]{64}', baseline_digest):
        raise ValueError('REUSE_BASELINE_INVALID')
    baseline = stage/'releases'/('prearmed-'+baseline_digest[:12])
    candidate = Path(installed['release'])
    delta = validate_release_delta(baseline, candidate, record.get('reviewed_guard_sha256', {}))
    if any(record.get(k) != v for k,v in delta.items()):
        raise ValueError('REUSE_REVIEWED_SOURCE_CHANGED')
    if record.get('basis') != delta['source_review_basis']:
        raise ValueError('REUSE_BASIS_MISMATCH')
    if proof.get('source_sha256') != delta['baseline_manifest_sha256']:
        raise ValueError('REUSE_TESTED_SOURCE_MISMATCH')
    if record.get('candidate_manifest_sha256') != sha((candidate/'release-manifest.json').read_bytes()):
        raise ValueError('REUSE_INSTALLED_SOURCE_MISMATCH')
    return {'basis': record['basis'], 'baseline_image_id': proof['image_id'],
            'candidate_image_id': installed['image_id'], 'new_trade_test': False}

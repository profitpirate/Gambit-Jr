#!/usr/bin/env bash
set -euo pipefail

if grep -q 'def test_public_capital_burst_is_accepted' tests/test_e4_hardening_v6.py; then
  python scripts/e4_migrate_legacy_policy_tests_v10.py
fi
if ! grep -q 'test-prearmed-intent' tests/test_e4_hardening_v6.py; then
  python scripts/e4_migrate_prearmed_test_v10.py
fi
if grep -q 'vars(self.counters)' src/memecoin_bot/e4_pipelines_v10.py; then
  python scripts/e4_patch_v10_pipeline_hotstores.py
fi
if grep -q 'vars(runtime.metrics)' src/memecoin_bot/e4_pipeline_runtime_v10.py; then
  python scripts/e4_patch_v10_runtime_faststream.py
fi
if ! grep -q 'def _apply_v10' src/memecoin_bot/e4_hardening_v10.py; then
  python scripts/e4_patch_v10_state_e4_observer.py
fi
if grep -q 'created_ns = time.time_ns()' scripts/e4_v10_social_stream.py; then
  python scripts/e4_patch_v10_social_timestamp.py
fi
if ! grep -q 'part not in _GENERIC_SOCIAL_TERMS' src/memecoin_bot/e4_pipelines_v10.py; then
  python scripts/e4_patch_v10_generic_terms.py
fi
if ! grep -q 'exact_ca_social_launch' src/memecoin_bot/e4_pipelines_v10.py; then
  python scripts/e4_patch_v10_direct_ca_social.py
fi
if ! grep -q 'last_sell_fraction' src/memecoin_bot/e4_pipelines_v10.py; then
  python scripts/e4_patch_v10_copy_exit.py
fi
if ! grep -q 'last_sell_signature' src/memecoin_bot/e4_pipelines_v10.py; then
  python scripts/e4_patch_v10_e4_signal_idempotency.py
fi
if ! grep -q 'def finalize_stale_learning' src/memecoin_bot/e4_pipelines_v10.py; then
  python scripts/e4_patch_v10_learning_finalizer.py
fi
if ! grep -q 'Different builder versions use different transaction field names' tools/e4-builder/race-proxy-v3.mjs; then
  python scripts/e4_patch_v10_builder_proxy.py
fi
if grep -q '9xQeWvG816bUx9EPfEZn9Y6b7nWn1N5uVxkZ7L1pump' tests/test_e4_v10_direct_ca_social.py; then
  python scripts/e4_patch_v10_direct_ca_test_mints.py
fi

python -m compileall -q \
  src/memecoin_bot/e4_hardening_v10.py \
  src/memecoin_bot/e4_pipelines_v10.py \
  src/memecoin_bot/e4_pipeline_runtime_v10.py \
  scripts/e4_v10_social_stream.py \
  scripts/e4_v10_discovery_worker.py \
  scripts/e4_v10_discovery_loop.py

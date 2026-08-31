#!/usr/bin/env bash
set -euo pipefail

# V11 is a consolidated source branch. Only deterministic migrations that are
# still valid for the current source shape may run here; feature code belongs
# in src/, not in ephemeral CI text-rewrite patches.
if grep -q 'def test_public_capital_burst_is_accepted' tests/test_e4_hardening_v6.py \
   && [ -f scripts/e4_migrate_legacy_policy_tests_v10.py ]; then
  python scripts/e4_migrate_legacy_policy_tests_v10.py
fi
if ! grep -q 'test-prearmed-intent' tests/test_e4_hardening_v6.py \
   && [ -f scripts/e4_migrate_prearmed_test_v10.py ]; then
  python scripts/e4_migrate_prearmed_test_v10.py
fi
if grep -q 'vars(self.counters)' src/memecoin_bot/e4_pipelines_v10.py \
   && [ -f scripts/e4_patch_v10_pipeline_hotstores.py ]; then
  python scripts/e4_patch_v10_pipeline_hotstores.py
fi
if grep -q 'vars(runtime.metrics)' src/memecoin_bot/e4_pipeline_runtime_v10.py \
   && [ -f scripts/e4_patch_v10_runtime_faststream.py ]; then
  python scripts/e4_patch_v10_runtime_faststream.py
fi
if grep -q 'created_ns = time.time_ns()' scripts/e4_v10_social_stream.py \
   && [ -f scripts/e4_patch_v10_social_timestamp.py ]; then
  python scripts/e4_patch_v10_social_timestamp.py
fi

python -m compileall -q \
  src/memecoin_bot/e4_hardening_v10.py \
  src/memecoin_bot/e4_pipelines_v10.py \
  src/memecoin_bot/e4_pipeline_runtime_v10.py \
  scripts/e4_v10_social_stream.py \
  scripts/e4_v10_discovery_worker.py \
  scripts/e4_v10_discovery_loop.py

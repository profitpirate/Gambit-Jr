#!/usr/bin/env bash
set -euo pipefail

bash scripts/e4_v10_apply_all_patches.sh
python scripts/e4_patch_v10_entrypoint.py
python scripts/e4_patch_v10_builder_preload_v4.py

# The following patchers are intentionally guarded by semantic markers so the
# script is safe in immutable CI checkouts and already-consolidated branches.
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
  src/memecoin_bot/e4_exec \
  scripts/e4_v10_social_stream.py \
  scripts/e4_v10_discovery_worker.py \
  scripts/e4_v10_discovery_loop.py
node --check tools/e4-builder/fast-preload-v4.mjs
node --check tools/e4-builder/race-proxy-v3.mjs

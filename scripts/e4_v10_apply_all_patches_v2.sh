#!/usr/bin/env bash
set -euo pipefail

bash scripts/e4_v10_apply_all_patches.sh

# V11 source-first consolidation. These migrations are deterministic and the
# workflow commits the resulting canonical source only after every gate passes.
python scripts/e4_v11_wire_canonical_manager.py
python scripts/e4_v11_fix_manager_cycle.py

if [ -f scripts/e4_patch_v10_entrypoint.py ]; then
  python scripts/e4_patch_v10_entrypoint.py
fi
if [ -f scripts/e4_patch_v10_builder_preload_v4.py ]; then
  python scripts/e4_patch_v10_builder_preload_v4.py
fi

python -m compileall -q \
  src/memecoin_bot/e4_hardening_v10.py \
  src/memecoin_bot/e4_pipelines_v10.py \
  src/memecoin_bot/e4_pipeline_manager_v11.py \
  src/memecoin_bot/e4_pipeline_runtime_v10.py \
  src/memecoin_bot/e4_exec \
  scripts/e4_v10_social_stream.py \
  scripts/e4_v10_discovery_worker.py \
  scripts/e4_v10_discovery_loop.py
node --check tools/e4-builder/fast-preload-v4.mjs
node --check tools/e4-builder/race-proxy-v3.mjs

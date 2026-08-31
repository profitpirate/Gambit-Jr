#!/usr/bin/env bash
set -euo pipefail

mkdir -p runtime logs
pids=()

cleanup() {
  local code=$?
  trap - EXIT INT TERM
  for pid in "${pids[@]:-}"; do
    kill -TERM "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  exit "$code"
}
trap cleanup EXIT INT TERM

export E4_PIPELINES_BACKGROUND="${E4_PIPELINES_BACKGROUND:-true}"
export E4_BUILDER_COMMAND="${E4_BUILDER_COMMAND:-node tools/e4-builder/race-proxy-v3.mjs}"

python scripts/e4_v10_discovery_loop.py \
  >>logs/e4-v10-discovery.log 2>&1 &
pids+=("$!")

if [[ -n "${X_BEARER_TOKEN:-}" && -f "${E4_SOCIAL_ACCOUNTS_PATH:-models/e4/e4-social-accounts.txt}" ]]; then
  python scripts/e4_v10_social_stream.py \
    --sync-rules \
    >>logs/e4-v10-social.log 2>&1 &
  pids+=("$!")
else
  echo "E4 V10 social stream disabled: set X_BEARER_TOKEN and E4_SOCIAL_ACCOUNTS_PATH" >&2
fi

E4_LIVE="${E4_LIVE:-false}" gambit-e4 run --live \
  >>logs/e4-v10-execution.log 2>&1 &
pids+=("$!")

wait -n "${pids[@]}"

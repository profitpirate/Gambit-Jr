#!/usr/bin/env bash
set -euo pipefail

# Run on the deployment host only after the GitHub production environment is manually approved.
: "${GAMBIT_RELEASE_SHA:?Set GAMBIT_RELEASE_SHA to the approved immutable commit}"
: "${GAMBIT_PRODUCTION_DIR:?Set GAMBIT_PRODUCTION_DIR to the existing production checkout}"

cd "$GAMBIT_PRODUCTION_DIR"
git fetch origin codex/gambit-jr-v1.5-finalization
test "$(git rev-parse origin/codex/gambit-jr-v1.5-finalization)" = "$GAMBIT_RELEASE_SHA"

mkdir -p backups
backup="backups/memecoin-$(date -u +%Y%m%dT%H%M%SZ).db"
cp --preserve=all data/memecoin.db "$backup"

previous_sha="$(git rev-parse HEAD)"
git switch --detach "$GAMBIT_RELEASE_SHA"
docker compose build --pull bot
if ! docker compose up -d --no-deps bot; then
  git switch --detach "$previous_sha"
  docker compose up -d --build --no-deps bot
  exit 1
fi

if ! curl --fail --silent --show-error --retry 12 --retry-delay 5 \
  http://127.0.0.1:8080/health >/dev/null; then
  git switch --detach "$previous_sha"
  docker compose up -d --build --no-deps bot
  exit 1
fi

python scripts/production_acceptance.py

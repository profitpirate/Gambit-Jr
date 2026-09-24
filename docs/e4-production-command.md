# Canonical V12 production command

There is one live execution authority:

```bash
python -m memecoin_bot.e4_exec migrate
E4_LIVE=true python -m memecoin_bot.e4_exec run --live
```

The historical `memecoin_bot.e4_prod` command is retained only as a compatibility
alias and delegates to the same `e4_exec` preflight and runtime. It is not an
alternate live path.

Before a live run can start, `e4_exec` requires the fail-closed V12 readiness
gate to pass: the fresh 100-trade causal certification, explicit live arm token,
distinct trading/storage wallets, external signer, redundant TLS RPC/routes,
healthy execution DB, dual operator notifications, tamper-evident audit key,
AES-256 backup key, signed code-integrity manifest, and a clear kill switch.

## Container deployment

Use an external Vault Transit Ed25519 signer. No private key is mounted into the
container.

```bash
cp .env.e4.example .env.e4
VAULT_TOKEN_HOST_PATH=/secure/path/vault-token \
  docker compose -f docker-compose.e4-prod.yml up -d --build
```

The production container runs non-root with a read-only root filesystem,
`no-new-privileges`, all Linux capabilities dropped, writable state isolated to
`data/`, `backups/`, `run/`, and `var/`, plus a local-only health check.

Axiom remains display-only: point Axiom at the same public wallet address if you
want the wallet and trades visible there. Gambit does not log in to or automate
the Axiom website.

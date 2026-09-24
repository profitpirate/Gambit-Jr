# E4 / V12 Pre-Armed security and secret boundary

## Current boundary

The frozen V12 Pre-Armed confirmation remains a causal paper-live research run. The production
readiness tooling is deliberately credential-free and cannot sign, submit, approve, buy, sell, or
move funds. Completing the 100-trade acceptance gate is necessary before any separate execution
integration should be considered.

The existing E4 execution engine is independently fail-closed. Starting it requires both the
\`--live\` command-line flag and \`E4_LIVE=true\`. The example environment defaults to
\`E4_LIVE=false\`.

## Secret handling

Never store a seed phrase or raw private key in Git, a prompt, Discord, SQLite, logs, screenshots,
container environment variables, Docker image layers, or a committed \`.env\` file. The runtime
rejects the common raw-secret environment variable names.

The preferred boundary is an external local signer through the existing \`E4_SIGNER_COMMAND\`
contract. If an operator instead uses \`E4_KEYPAIR_PATH\`, the file must:

- be a regular file, not a symlink;
- be mounted read-only;
- have owner-only permissions (\`chmod 600\` or \`chmod 400\`);
- remain outside the repository and Docker build context.

For encrypted-at-rest storage, keep the encrypted secret outside this repository and decrypt it only
into a short-lived, owner-only RAM-backed location immediately before starting the signer. The
application itself does not implement key escrow or persist decrypted key material.

## Container controls

\`docker-compose.e4-prod.yml\` uses a read-only root filesystem, drops all Linux capabilities, enables
\`no-new-privileges\`, uses a \`noexec,nosuid\` temporary filesystem, limits PIDs, mounts the keypair
read-only, bounds logs, and gives runtime state only the explicit writable mounts under \`data/\` and
\`var/e4/\`.

The Docker build context excludes local environments, key/keypair files, SQLite/WAL files, data,
evidence, outputs, and runtime journals. The image uses Node 22 to match the pinned builder package
requirements.

## Transaction construction

The V12 builder is local-first. The production example uses the persistent race proxy and the pinned
Pump SDK builder. Remote PumpPortal construction is disabled by default and must never be treated as
an automatic fallback.

## Readiness and audit commands

These commands are safe to run without credentials:

\`\`\`bash
python scripts/v12_prearmed_readiness.py --no-fail
python scripts/v12_prearmed_brutal_stress.py --iterations 100000
python scripts/e4_security_audit.py
python scripts/e4_execution_ledger_audit.py --database data/e4.db
\`\`\`

The readiness command intentionally reports blockers while the fresh causal 100-trade confirmation is
incomplete. That is a safety feature, not a test failure.

## External work deliberately deferred

Credentials, paid/private RPC endpoints, route-specific authorization headers, the operator-owned
signer/key material, and real on-chain/Axiom reconciliation require operator-provided external
information. None should be fabricated or stored in the repository.

## Verified builder dependency remediation

The production builder lockfile was regenerated and accepted only after a clean `npm ci --omit=dev`,
the vendored bigint compatibility self-test, the native local Pump transaction-construction self-test,
and `npm audit --omit=dev --audit-level=high` all passed.

The verified production graph pins Pump SDK 2.0.0, SPL Token 0.4.15, Solana web3.js 1.99.0,
bn.js 5.2.5, toml 4.2.0, stream-json 3.5.0 and uuid 11.1.1. The unpatched native
`bigint-buffer` package is replaced by the repository-owned pure-JavaScript compatibility package
under `tools/e4-builder/vendor/bigint-buffer-safe`; that replacement has no native addon or
postinstall script and performs explicit buffer, width and bigint range checks.


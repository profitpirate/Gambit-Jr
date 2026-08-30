# Gambit Jr V2 — E4 live execution

## Sole authority

`memecoin_bot.e4_live` is a separate live order authority. The order path does not consult
`RunnerDecision`, CONTROL_V15, legacy scoring, WATCH/STRONG tiers, narrative, social data, Discord, or
slow enrichment.

```text
V1.5 canonical Pump event
  → E4 in-memory token state
  → E4 entry policy
  → one-entry / two-position invariant
  → local transaction builder
  → local signer
  → identical-signature multi-route submission
  → event-driven E4 exit policy
  → closed position
  → excess-SOL sweep
```

Current V1.5 remains the event/intelligence foundation: direct Pump ingestion, ordered trades, reserve
state, wallet identity, persistence, and historical evidence. It cannot override an E4 decision.

## Reproduced wallet behaviour

The observed contract is stored at `models/e4/e4-observed-v1.json`.

Hard-coded invariants:

- exactly one entry attempt per mint after the order is committed;
- no averaging down and no re-entry;
- maximum two concurrent positions;
- early Pump curve/low-FDV entry profile;
- 20% acceleration or 30% normal first partial;
- failed confirmation closed inside the observed five-second failure window;
- event-driven runner exits;
- 60-second observed absolute hold horizon;
- transaction route racing with one identical signature;
- excess SOL swept to a public Phantom address when no position remains.

The exact hidden selected-vs-ignored entry function is not knowable from E4 buys alone. The live engine
records the complete point-in-time state immediately before every observed E4 buy/sell. A calibrated
selected-vs-ignored logistic artifact can later replace the deterministic entry fallback by adding a
`selection_model` object to `E4_MODEL_PATH`; it cannot weaken the one-entry or two-position invariants.

## Event integration

The engine tails Gambit V1.5's operational SQLite/WAL journal every 2 ms by default. It discovers a
compatible event table from its column signatures rather than relying on one historical table name.
This reuses the latest V1.5 realtime fabric without putting the V1.5 decision stack in front of E4.

A production Helius/private stream must feed V1.5. The public RPC latency measured by V1.5 is not
compatible with trades whose full lifetime can be only a few seconds.

## Builder

The engine sends one JSON request to `E4_BUILDER_COMMAND` and expects:

```json
{"transaction_base64":"..."}
```

`tools/e4-builder/index.mjs` supports Pump buy/sell transaction construction and native SOL vault
sweeps. It never receives the private key. The builder boundary can be replaced with a pinned official
Pump/PumpSwap SDK implementation without touching E4 selection, sizing, route racing, or exits.

## Signer

The recommended signer loads an operator-owned Solana keypair from a read-only mounted file:

```text
E4_KEYPAIR_PATH=/run/secrets/e4-solana-keypair.json
```

The file must match `E4_WALLET_PUBLIC_KEY`. The key is never persisted in Gambit databases, logs,
Discord, GitHub, prompts, or the transaction builder. An external local signer can instead implement
this stdin/stdout contract:

Request:

```json
{"transaction_base64":"...","expected_public_key":"..."}
```

Response:

```json
{"signed_transaction_base64":"...","signature":"..."}
```

Only the Phantom public receive address is configured as `E4_VAULT_PUBLIC_KEY`; Gambit cannot spend
from the vault.

## Route racing

`E4_ROUTE_URLS_JSON` defines any compatible low-latency Solana `sendTransaction` endpoints available
to the operator. The exact same signed transaction is sent over each route with a small stagger, so
multiple route acceptance cannot create multiple independent fills.

For every order, Gambit records route submit/response latency, accepted route, signature, confirmation
slot, errors, and every competing route result. Helius Sender, Nozomi, Jito-compatible senders, a
private staked RPC, and direct RPC can therefore be benchmarked using actual landed transactions.

## Start

```bash
cp .env.e4.example .env.e4
python -m pip install -r requirements-e4.txt
cd tools/e4-builder && npm install --omit=dev && cd ../..
python -m memecoin_bot.e4_live migrate
E4_LIVE=true python -m memecoin_bot.e4_live run --live
```

Both `E4_LIVE=true` and `--live` are required to prevent an accidental local command from signing with
a mounted wallet. This is an acknowledgement interlock, not a paper/shadow trading mode.

Do not paste a seed phrase, private key, Axiom login, Phantom login, Apple login, or Google login into
ChatGPT, Codex, Discord, GitHub, `.env`, source code, or screenshots.

## Operator boundary

The system executes direct on-chain transactions. The operator is responsible for running it only in
a location/account context where the relevant services and interactions are permitted. It contains no
VPN, geolocation spoofing, browser impersonation, or access-control bypass.

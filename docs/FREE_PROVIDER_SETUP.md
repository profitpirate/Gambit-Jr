# Free provider setup

## HELIUS

PROVIDER: Helius

signup page: https://dashboard.helius.dev/signup

free tier: 1,000,000 credits/month and 10 requests/second; standard WebSocket traffic is credit-metered

required ENV variable: `HELIUS_API_KEY`; leave `SOLANA_RPC_URL` at the public
mainnet default to make Helius the derived primary automatically. Public mainnet
remains the configured fallback. The key is never logged or fingerprinted.

why it is useful: selective hot-account, creator, wallet and transaction enrichment on a production-grade Solana RPC

## PUMPPORTAL

PROVIDER: PumpPortal

signup page: https://pumpportal.fun/data-api/

free tier: new-token and migration streaming have no data fee; trade feeds are not enabled without explicit budget approval

required ENV variable: `PUMPPORTAL_API_KEY`

why it is useful: low-latency launch and migration redundancy over one multiplexed socket while native chain evidence remains truth

## DUNE

PROVIDER: Dune

signup page: https://dune.com/auth/register

free tier: 15 low-limit query executions/minute and 40 result requests/minute, subject to Dune credits and current plan limits

required ENV variable: `DUNE_API_KEY`

Gambit owns and versions the SQL in
`src/memecoin_bot/historical/sql/dune/`, renders strict monthly parameters,
executes `POST /v1/sql/execute`, polls, pages, validates schemas, writes Parquet
outside Git, and checkpoints each partition. `DUNE_QUERY_ID` is not mandatory.
Only when Dune explicitly rejects direct SQL for the current plan does Gambit
emit a one-time instruction and use a configured saved-query ID as fallback.

Acquisition is deliberately inert by default. `DUNE_DRY_RUN=true`, an empty
`DUNE_START_MONTH`/`DUNE_END_MONTH`, and `DUNE_MAX_EXECUTIONS=0` prevent an
accidental 2024-to-present run. Inspect the exact plan first:

```bash
python -m memecoin_bot.convergence historical-plan
```

One complete-month pilot (four executions maximum):

```bash
DUNE_START_MONTH=2026-07 DUNE_END_MONTH=2026-07 DUNE_MAX_EXECUTIONS=4 \
DUNE_DRY_RUN=false \
DUNE_QUERY_NAMES=monthly_universe,pumpfun_launches,pumpfun_trades,outcome_reconstruction \
DUNE_PILOT_SAMPLE_ROWS=10000 \
python -m memecoin_bot.convergence historical-pilot --execute
```

Completed partitions are skipped. `--force` is the only way to request a re-run.
Execution IDs are checkpointed before result retrieval, rate-limit retries are
bounded, and a failed invocation resumes rather than silently creating a second
execution. A partition is not complete until schema and Pump.fun/PumpSwap semantic
checks pass. Parquet and raw evidence remain outside Git.
If a source result exceeds `DUNE_PILOT_SAMPLE_ROWS`, the pilot retrieves a bounded
server-side sample, records the source's exact total rows/bytes, and persists the
partition as `PILOT_SAMPLE_COMPLETE`. This proves schema and semantics without
causing a later full acquisition to skip the partition. Set the value to `0` only
for an explicitly approved full materialization.

why it is useful: month-partitioned, indexed Solana history without downloading the full chain archive

## TELEGRAM

PROVIDER: Telegram

signup page: https://my.telegram.org/apps

free tier: official Telegram API access for the operator's own account and authorized/public channels

required ENV variable: `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION`, `TELEGRAM_CHANNELS`

why it is useful: optional address-mention velocity and cross-channel narrative confirmation from channels the operator is authorized to observe

Public channels can instead use `TELEGRAM_PUBLIC_CHANNELS` with the read-only
`https://t.me/s/<channel>` collector. It never attempts private content, retains no
raw message text, waits between requests, and marks channels without a public
preview unavailable.

## SOLANA CORROBORATION AND ENRICHMENT

| Provider | Configuration | Bounded role |
|---|---|---|
| Alchemy | `ALCHEMY_API_KEY`, or explicit `ALCHEMY_SOLANA_RPC_URL` and optional `ALCHEMY_SOLANA_WSS_URL` | secondary standard Solana RPC/WSS; explicit URLs override derived endpoints |
| Shyft | `SHYFT_API_KEY`, or `SHYFT_SOLANA_RPC_URL` | tertiary `getSlot` fallback only |
| Solana Tracker | `SOLANA_TRACKER_API_KEY`; optional RPC/WSS/Data URL overrides | independent RPC/WSS plus indexed token price/metadata corroboration |
| Birdeye | `BIRDEYE_API_KEY` | optional holder, wallet and plan-exposed sniper/insider/bundle/smart-money enrichment |
| Solscan | `SOLSCAN_API_KEY` | low-rate indexed token/holder/transaction cross-check |

These sources enrich evidence and health. They do not outrank Helius/native chain
truth and do not independently gate `RunnerDecision`. Birdeye fields are parsed only
when the operator's plan actually returns them.

## SLOW CONTEXT AND PUBLIC SOCIAL RESEARCH

| Provider | Configuration | Bounded role |
|---|---|---|
| CoinGecko | optional `COINGECKO_API_KEY` | slow SOL trend/volatility and broad regime context; never the hot-token critical path |
| Neynar/Farcaster | `NEYNAR_API_KEY` | read-only cast mention, author-spread and engagement research |
| YouTube Data API v3 | `YOUTUBE_API_KEY` | high-priority/sampled search only, with a six-hour default cache and per-process search budget |
| Mastodon | `MASTODON_INSTANCE_URLS`; optional `MASTODON_ACCESS_TOKEN` | sequential multi-instance public search fallback |

Social observations normalize to `SocialEvidence`, hash author/channel/query
identifiers, omit raw content, and classify links as `COMMUNITY`, `PROFILE`,
`OFFICIAL_PROJECT`, `UNKNOWN`, or `NONE` using only evidence available at the
observation timestamp. Community/profile fields are unweighted research hypotheses.

Run the low-volume live admission probes with:

```bash
python -m memecoin_bot.convergence providers --probe
```

Output reports configuration, admission/live states, counts, latency, errors and
role without returning keys, authorization headers, or credential-bearing URLs.

## KEYLESS SOURCES

PROVIDER: DexScreener, GeckoTerminal, native Solana RPC and Bluesky Jetstream

signup page: none

free tier: public, rate-limited interfaces; public Solana RPC is fallback/health comparison only

required ENV variable: none

why it is useful: market/promotion events, new-pool redundancy, chain-health truth and a keyless public social transport; none alone proves a runner

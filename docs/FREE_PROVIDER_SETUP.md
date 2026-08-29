# Free provider setup

## HELIUS

PROVIDER: Helius

signup page: https://dashboard.helius.dev/signup

free tier: 1,000,000 credits/month and 10 requests/second; standard WebSocket traffic is credit-metered

required ENV variable: `HELIUS_API_KEY`

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

required ENV variable: `DUNE_API_KEY`, plus the reviewed saved-query identifier in `DUNE_QUERY_ID`

why it is useful: month-partitioned, indexed Solana history without downloading the full chain archive

## TELEGRAM

PROVIDER: Telegram

signup page: https://my.telegram.org/apps

free tier: official Telegram API access for the operator's own account and authorized/public channels

required ENV variable: `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION`, `TELEGRAM_SOCIAL_CHANNELS`

why it is useful: optional address-mention velocity and cross-channel narrative confirmation from channels the operator is authorized to observe

## KEYLESS SOURCES

PROVIDER: DexScreener, GeckoTerminal, native Solana RPC and Bluesky Jetstream

signup page: none

free tier: public, rate-limited interfaces; public Solana RPC is fallback/health comparison only

required ENV variable: none

why it is useful: market/promotion events, new-pool redundancy, chain-health truth and a keyless public social transport; none alone proves a runner

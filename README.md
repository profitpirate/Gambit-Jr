# Solana Memecoin Intelligence & Signal Bot — V1

A read-only, autonomous intelligence service for Solana memecoins. It discovers newly
active tokens, collects real market and mint data, applies fail-closed safety gates,
scores only supported evidence, persists every decision, and monitors qualifying
signals. It never connects to a wallet and contains no trading code.

## Completion status

This repository is a production-oriented **P0 implementation**, not a claim that the
brief's live acceptance test is complete. A real shadow cycle on 2026-08-20 naturally
discovered and evaluated three Solana tokens. All were rejected; no threshold was
weakened. There were no Discord credentials or cloud target, so a real Discord signal,
active-signal restart, natural lifecycle, and independent 24/7 deployment are not yet
proven. See `outputs/DELIVERY_REPORT.md` and `outputs/live-shadow-evidence.json`.

Implemented:

- documented DEX Screener profile/boost/community-takeover discovery;
- documented DEX Screener pair market snapshots;
- Solana JSON-RPC mint authority, freeze authority, supply, and top-account checks;
- `None`/unknown semantics for unavailable holder, bundler, insider, social velocity,
  developer-history, and distribution data;
- configurable hard gates and versioned deterministic scoring;
- SQLite migrations, indexes, WAL, immutable initial signal trigger, and durable outbox;
- one-shot milestones, failure persistence, active-signal recovery, and performance stats;
- one configured Discord channel, safe message formatting, `/status`, and `/performance`;
- structured JSON logs, provider retry/backoff/circuit state, health HTTP endpoint;
- Docker/Compose deployment, replay simulation, and critical tests.

Deliberately degraded or unavailable without legitimate providers:

- X/Telegram/Discord mention velocity and bot-spam detection;
- developer/funding-wallet/related-wallet history;
- reliable bundler, insider, sniper, holder-count, and smart-money labels;
- external breaking-news/catalyst velocity and multilingual source feeds;
- live upgrade/downgrade intelligence is not yet wired into the tracker;
- PostgreSQL adapter and chain adapters beyond Solana.

None of those values are synthesized as zero.

## Architecture

The production path is:

`discovery → market → Solana safety → developer/narrative/social/on-chain/momentum → hard gates → scoring → immutable signal + Discord outbox → tracker → milestones/failure → analytics`

Provider code is under `src/memecoin_bot/providers`; business rules do not depend on
provider response shapes. SQLite writes a signal and its outbound Discord event in the
same transaction. A unique `(signal_id, multiple)` constraint and outbox event key make
milestones restart-safe.

## Data providers

| Provider | Status | Use |
|---|---|---|
| DEX Screener public API | Free but rate-limited | activation discovery, pairs, price/MC/liquidity/volume/transactions/social links |
| Solana JSON-RPC | Free public endpoint but heavily rate-limited | mint configuration, supply, largest token accounts |
| Dedicated Solana RPC | Optional paid/free-tier upgrade | better reliability for top-account distribution |
| Social/news/developer providers | Not configured | interfaces return unknown; no fabricated evidence |

The implementation follows the documented [DEX Screener API](https://docs.dexscreener.com/api/reference),
[Solana RPC](https://solana.com/docs/rpc), and [Discord rate-limit guidance](https://docs.discord.com/developers/topics/rate-limits).

## Local setup

Requirements: Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
python -m pip install -e '.[dev]'
cp .env.example .env                 # Windows: Copy-Item .env.example .env
```

Set `DISCORD_TOKEN` and `DISCORD_CHANNEL_ID` for slash commands and alerts. A webhook
can send one-way alerts, but slash commands require a bot token. Invite the bot with
`bot` and `applications.commands` scopes and permission to view/send in exactly the
configured channel.

Start in shadow mode first:

```bash
memecoin-bot once --output evidence/live-shadow.json
memecoin-bot run
```

With `SHADOW_MODE=true` and `SHADOW_SEND_ALERTS=false`, decisions persist but Discord is
suppressed. With both set to true, qualifying messages are clearly labelled shadow
tests. Production signalling requires `SHADOW_MODE=false`; this changes labelling only,
not thresholds.

## Tests and replay

```bash
python -m unittest discover -s tests -v
memecoin-bot replay \
  --fixture fixtures/replay_lifecycle.json \
  --database data/replay-$(date +%s).db \
  --output evidence/replay.json
```

Replay uses production scoring, gates, persistence, tracking, milestone, failure, and
analytics code. Its evidence is always labelled simulation and never counts as live E2E
proof.

## Docker deployment

1. Provision any Linux VM/container host with persistent volumes (a free tier is fine if
   it permits an always-on process and outbound HTTPS).
2. Copy the repository and create `.env` from `.env.example`.
3. Add the Discord bot token/channel and preferably a free-tier dedicated Solana RPC URL.
4. Keep `SHADOW_MODE=true`; optionally enable labelled test alerts.
5. Run `docker compose up -d --build`.
6. Verify `docker compose ps`, `docker compose logs -f bot`, and
   `curl http://127.0.0.1:8080/health`.
7. Restart with `docker compose restart bot`; confirm the same database volume, active
   signals, immutable signal MC, milestone rows, and pending outbox state remain.
8. Only after reviewing sustained shadow evidence set `SHADOW_MODE=false` and redeploy.

The Compose policy is `restart: unless-stopped`; named volumes keep SQLite and evidence
independent of the development computer. Back up the `bot-data` volume. Run only one
replica with SQLite. Move to PostgreSQL before horizontal scaling.

## Environment variables

Every supported variable and safe default appears in `.env.example`. Required for full
Discord operation: `DISCORD_TOKEN`, `DISCORD_CHANNEL_ID`. Required runtime configuration:
`SOLANA_RPC_URL`, `DATABASE_PATH`. All other thresholds, weights, intervals, milestones,
timeouts, failure rules, alert behavior, and health port are configurable there.

Never commit `.env`. The service does not accept a seed phrase, private key, wallet,
exchange credential, or swap endpoint.

## Operational acceptance checklist

Before claiming V1 complete, retain evidence for a naturally qualifying real token,
Discord message ID, immutable DB snapshot, subsequent market updates, service restart,
non-duplicated milestones, Discord reconnect, and an independently running cloud
container. Do not lower thresholds to manufacture that evidence.

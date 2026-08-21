# Gambit Jr — V1.3 Radar Intelligence and GMGN Enrichment

A read-only, autonomous intelligence service for Solana and BNB Chain memecoins. It discovers newly
active tokens, collects real market and mint data, applies fail-closed safety gates,
scores only supported evidence, persists every decision, and monitors qualifying
signals. It never connects to a wallet and contains no trading code.

## Completion status

The V1.2 code path is implemented and covered by deterministic unit and replay tests.
This is not a claim that the brief's live operational acceptance test is complete:
Discord credentials, a cloud target, and a naturally qualifying real token are external
requirements. Thresholds must not be weakened to manufacture that evidence.

Implemented:

- DEX Screener profile/boost/community-takeover discovery and pair market snapshots;
- GeckoTerminal new-pool discovery for Solana and BNB Chain, with isolated provider failures;
- chain-aware identity, fair bounded discovery, and per-chain candidate caps;
- Solana JSON-RPC mint authority, freeze authority, supply, and top-account checks;
- BSC JSON-RPC contract-bytecode and standard `owner()` checks with explicit unknowns;
- `None`/unknown semantics for unavailable holder, bundler, insider, social velocity,
  developer-history, and distribution data;
- configurable hard gates and versioned deterministic scoring;
- SQLite migrations, indexes, WAL, immutable initial signal trigger, and durable outbox;
- one-shot milestones, failure persistence, active-signal recovery, and performance stats;
- mobile-first Discord embeds/buttons and all five operational slash commands;
- durable early-radar, outcome, missed-runner, and pre-move opportunity-coverage records;
- structured JSON logs, provider retry/backoff/circuit state, health HTTP endpoint;
- Docker/Compose deployment, replay simulation, and critical tests.

Deliberately degraded or unavailable without legitimate providers:

- X/Telegram/Discord mention velocity and bot-spam detection;
- developer/funding-wallet/related-wallet history;
- reliable bundler, insider, sniper, holder-count, and smart-money labels;
- external breaking-news/catalyst velocity and multilingual source feeds;
- PostgreSQL adapter and chain adapters beyond Solana and BNB Chain.

None of those values are synthesized as zero.

## Architecture

The production path is:

`multisource discovery → durable candidate → real snapshots → EARLY_RADAR → normalized qualified signal → tracking → missed-runner analytics`

Candidate states are `DISCOVERED`, `SCREENING`, `CANDIDATE`, `PENDING_EVIDENCE`,
`FAILED_PROVIDER`, `EARLY_RADAR`, `REJECTED_UNSAFE`, `EXPIRED`, and `SIGNALLED`. Signal classes remain
`WATCH`, `STRONG`, and `HIGH_CONVICTION`. Mint/freeze authority and verified excessive
holder concentration are terminal safety failures. Low liquidity, low market cap,
missing momentum history, insufficient coverage, and provider outages remain retryable.
Duplicate discovery updates the known token; it does not own or disable monitoring.

## Normalized scoring

The database retains raw component points, normalized score, confidence, and source
availability. Let `available_weight` be the sum of weights whose evidence is genuinely
available and `earned_points` the points from those components:

```text
confidence = available_weight / total_configured_weight
normalized_score = earned_points / available_weight * 100
```

For example, 46 earned points from 65 available weight gives confidence 65% and a
normalized score of 70.77 (WATCH). A high normalized score with confidence below
`MIN_CONFIDENCE_FOR_SIGNAL` must not produce a signal. Unknown evidence is not
equivalent to negative evidence and is never converted into fabricated evidence.

## V1.2 discovery and chain adapters

One shared lifecycle handles canonical `(chain, token_address)` identities for `solana`
and `bsc`. The same literal address on both chains remains two distinct tokens.

Active discovery adapters are:

- GeckoTerminal keyless new-pool feeds for Solana and BNB Chain;
- DexScreener latest profiles;
- DexScreener latest boosts;
- DexScreener community takeovers.

Each adapter fails independently. Cross-source discoveries merge into one token and one
candidate while `discovery_sources` preserves every observed source and the first source
on `tokens` remains immutable. Candidate limits are bounded globally and per chain.

DexScreener supplies canonical market snapshots for both chains. Solana safety retains
mint/freeze and top-account checks. BNB safety verifies contract bytecode and probes the
standard `owner()` selector through BSC JSON-RPC. Owner/admin state, holder distribution,
and transfer mechanics stay explicitly unknown when they cannot be proven. BNB owner
renouncement is useful evidence, not proof that a token is safe.

## EARLY_RADAR

EARLY_RADAR is a lower-confidence attention alert, never a qualified signal. It requires
basic chain validation, at least `RADAR_MIN_SNAPSHOTS`, liquidity and market-cap limits,
an age inside `RADAR_MAX_AGE_MINUTES`, at least `RADAR_MIN_CONDITIONS` abnormal conditions,
and `RADAR_SCORE_THRESHOLD`. Conditions include market-cap velocity, volume acceleration,
liquidity growth, buy pressure, buy-pressure acceleration, and volume/market-cap activity.

Late vertical price moves, a market cap above the radar range, collapsing liquidity,
dominant sell pressure, and observations outside the early window apply hard penalties
and suppress alerts. Radar events are unique by candidate and escalation level, so a
restart or repeated monitoring cycle cannot resend the same event. Radar and signal
timestamps/market caps are stored separately, including radar-to-signal latency and
multiple.

## Unicode metadata

Names, symbols, and descriptions remain original Unicode in SQLite and Discord. Narrative
tokenization uses Unicode word characters and lightweight multilingual concept terms;
Chinese, Japanese, Korean, Arabic, Cyrillic, accented Latin, and emoji metadata cannot
cause rejection. Uninterpreted narrative evidence is `UNKNOWN`, never negative. Optional
translation fields exist separately and are not required for monitoring or signals.

## Discord and opportunity coverage

Automatic radar, signal, upgrade, milestone, deterioration, and failure messages use
mobile-first Discord embeds. Every card shows the full copyable contract address and an
explicit SOLANA or BNB CHAIN label. Valid DexScreener plus Solscan/BscScan link buttons
are constructed without credentials.

Commands are `/status`, `/candidates`, `/rejections`, `/missed`, and `/performance`.
`/missed` reports tokens that crossed `MISSED_RUNNER_MULTIPLE` without a qualified signal,
including discovery market cap, peak, multiple, radar status, and the last non-signal
reason. Signal hit rates and opportunity coverage remain separate metric families.
Coverage uses explicit counts for major runners discovered, radar-flagged, signalled,
and completely missed, plus discovery-to-signal and radar-to-signal latency.

Provider code is under `src/memecoin_bot/providers`; business rules do not depend on
provider response shapes. SQLite writes a signal and its outbound Discord event in the
same transaction. A unique `(signal_id, multiple)` constraint and outbox event key make
milestones restart-safe.

## Data providers

| Provider | Status | Use |
|---|---|---|
| GeckoTerminal public API | Free but rate-limited | recent new-pool discovery on Solana and BNB Chain |
| DEX Screener public API | Free but rate-limited | activation discovery, pairs, price/MC/liquidity/volume/transactions/social links |
| Solana JSON-RPC | Free public endpoint but heavily rate-limited | mint configuration, supply, largest token accounts |
| BSC JSON-RPC | Free public endpoint but rate-limited | bytecode existence and standard owner probe |
| Dedicated Solana RPC | Optional paid/free-tier upgrade | better reliability for top-account distribution |
| Social/news/developer providers | Not configured | interfaces return unknown; no fabricated evidence |

The implementation follows the documented [DEX Screener API](https://docs.dexscreener.com/api/reference),
[GeckoTerminal API](https://docs.coingecko.com/reference/latest-pools-list),
[Solana RPC](https://solana.com/docs/rpc),
[BNB Smart Chain JSON-RPC](https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/json-rpc-endpoint/),
and [Discord rate-limit guidance](https://docs.discord.com/developers/topics/rate-limits).

## Local setup

Requirements: Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
python -m pip install -e '.[dev]'
cp .env.example .env                 # Windows: Copy-Item .env.example .env
```

Set `DISCORD_TOKEN` and `DISCORD_CHANNEL_ID` for `/status`, `/candidates`, `/rejections`,
`/missed`, `/performance`, and alerts. A webhook
can send one-way alerts, but slash commands require a bot token. Invite the bot with
`bot` and `applications.commands` scopes and permission to view/send in exactly the
configured channel.

Start in shadow mode first:

```bash
memecoin-bot once --output evidence/live-shadow.json
memecoin-bot run
```

V1.2 remains `SHADOW_MODE=true` and `SHADOW_SEND_ALERTS=true`. Qualifying messages are
explicitly labelled read-only shadow signals. There is no wallet, private key,
transaction signing, swap, purchase, or sale path.

## V1.3 intelligence upgrade

V1.3 preserves the V1.2 discovery, chain safety, Radar, scoring, signal and outcome paths. It adds an
optional fail-open GMGN Agent/OpenAPI provider using only API-key authenticated `GET` routes:
`/v1/token/info`, `/v1/token/security`, `/v1/token/pool_info`,
`/v1/market/token_top_holders`, and `/v1/market/token_top_traders`. The provider contains no signed
routes, swap/order calls, wallet, seed or private-key configuration. GMGN downtime leaves existing
providers and qualification logic running and records unavailable fields as unknown.

The additive `004_v13_intelligence.sql` migration persists immutable Radar payloads, GMGN raw
snapshots, field provenance, separate wallet-label evidence, priority transitions, social history,
intelligence events, per-channel alert delivery, paper simulations, latency metrics, outcomes and
version/config fingerprints. The `/status` state reconciliation reports a zero difference only when
every tracked token belongs to exactly one visible lifecycle state.

The database-backed, read-only Radar Board runs on `RADAR_BOARD_PORT` (default `8081`) and serves
`/api/status`, `/api/radar`, and `/api/token?address=<CA>`. Discord adds `/radar`, `/runners`,
`/failed`, `/token`, and `/smartmoney`; alert cards include chain-aware GMGN links. Configure
multiple bot channels with `DISCORD_CHANNEL_IDS=id1,id2`; delivery success and retry state are
persisted independently per channel.

These V1.2 qualification defaults are unchanged: WATCH 65, STRONG 75, HIGH_CONVICTION 85, and
minimum confidence 0.60. Social presence or a single labelled wallet cannot independently create
Priority, and terminal safety evidence cannot be overridden by smart-money evidence.

### GMGN rate-limit assumptions

GMGN's official token skill documents a leaky bucket with rate/capacity 20 and route weights of 1
for info/security/pool and 5 for holders/traders. Jr applies caching, per-token in-flight dedupe,
bounded concurrency, exponential retry/backoff, 429 handling through the resilient client, and a
circuit breaker. Static enrichment defaults to a 120-second TTL. Operators should lower concurrency
or increase TTL if their issued key has stricter limits.

### V1.3 deployment and rollback

Before deploying, stop the V1.2 process and back up the SQLite database plus WAL/SHM files. Check out
`codex/gambit-jr-v1.3-intelligence-gmgn`, configure `.env`, keep `SHADOW_MODE=true`, and run
`docker compose up -d --build`. Verify ports 8080 and 8081, provider health, state reconciliation,
Discord commands, and restart recovery. GMGN may remain disabled; the scanner continues normally.

Rollback is application-first: stop V1.3, check out the V1.2 branch, and start the old container
against a pre-upgrade database backup. Migration 004 is additive, but restoring the backup is the
supported rollback because older application code does not own V1.3 tables.

## Tests and replay

```bash
python -m unittest discover -s tests -v
memecoin-bot replay \
  --fixture fixtures/replay_v12_multichain.json \
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
7. Restart with `docker compose restart bot`; confirm the same candidates, snapshot
   histories, signals, immutable signal MC, milestone rows, and pending outbox state remain.
8. Keep V1.1 in shadow mode while reviewing sustained evidence.

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

## Backup, deployment, and rollback

Before migration, stop the bot or use SQLite's online backup command and copy
`/app/data/memecoin.db` (plus `-wal`/`-shm` when copying a live database) to dated,
off-host storage. Migrations `002_candidate_lifecycle.sql` and `003_radar_multichain.sql`
are additive and preserve
tokens, evaluations, signals, milestones, provider health, and the durable outbox.

Deploy `codex/gambit-jr-v1.2-radar-multichain` only after unit, replay, Compose,
restart, health, and Discord shadow checks. Verify `docker compose ps`, then
`curl http://127.0.0.1:8080/health`, and exercise all five Discord commands. To roll
back, stop the V1.2 container, restore the pre-migration database backup, check out
`codex/gambit-jr-v1.1-candidate-lifecycle`, and run `docker compose up -d --build`. The V1.1
branch is not modified by V1.2 development.

## Known limitations

Social velocity, developer reputation, bundler/insider/sniper intelligence,
smart-money labels, funding-wallet history, and breaking-news velocity remain UNKNOWN
until legitimate providers are integrated. BNB transfer restrictions and holder
concentration are also unknown without a dedicated security/indexing provider.

## Resource profile

The service runs one discovery loop, one bounded candidate loop, one low-cadence outcome
loop, and one signal tracker.
New-pool feeds are polled once per discovery cycle; candidate history is bounded by
`SNAPSHOT_HISTORY_LIMIT`, candidates by global and per-chain limits, and SQLite remains
single-instance. Run one Compose replica only. Adaptive per-candidate cadence remains a
future optimization; the bounded shared cadence is intentionally simpler and predictable.

## Operational acceptance checklist

Before claiming V1.2 live acceptance complete, retain evidence for a naturally qualifying real token,
Discord message ID, immutable DB snapshot, subsequent market updates, service restart,
non-duplicated milestones, Discord reconnect, and an independently running cloud
container. Do not lower thresholds to manufacture that evidence.

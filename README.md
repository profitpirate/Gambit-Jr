# Gambit Jr — V1.5 Runner/Survival Intelligence

V1.5 now has one final signal authority: `RunnerDecision`. The older scoring,
Alpha, V1.5 deterministic, V3 and Runner Thesis outputs remain explicit controls
or research evidence; none can independently route a signal. CONTROL_V15 is the
champion until a target-specific calibrated challenger passes an unseen,
same-universe chronological evaluation and receives explicit approval.

Repository-owned Dune SQL lives in `src/memecoin_bot/historical/sql/dune/`.
`DUNE_API_KEY` is sufficient for preferred direct SQL execution; `DUNE_QUERY_ID`
is only an optional saved-query fallback for plans that reject direct SQL. Dune is
dry-run-only unless an explicit complete-month range, a positive execution cap and
`DUNE_DRY_RUN=false` are all set. With a
Helius key and the default public Solana URL, the runtime safely derives Helius
RPC/WSS endpoints as primary and retains public mainnet as fallback. Keys are not
included in fingerprints or logs.

The final V1.5 storage and research contract is documented in
[`docs/v15-historical-intelligence.md`](docs/v15-historical-intelligence.md). It adds an immutable raw
archive, separate point-in-time research warehouse, explicit approved production feature store,
checkpointed provider-neutral backfills, leakage-failing chronological research, bounded historical
live context, and signal-first contract actions. No external historical corpus is bundled. The
credential-free acquisition and truthful negative research result are documented in
[`docs/v15-finalization.md`](docs/v15-finalization.md). DATA COMPLETE, RESEARCH COMPLETE, STAGING
COMPLETE and PRODUCTION READY remain false; the existence of the schema is not a proven edge.

V1.5 adds a production-consumed decision core with independent runner and failure
scores, setup conviction separate from evidence coverage, stage-specific evaluation,
provider conflict/freshness states, notional tradeability estimates, effective actor
concentration, buyer replacement, immutable V1.5 T0 calls, and signal-only Discord
routing. Public automatic tiers are `PREMIUM`, `STRONG`, `HIGH_RISK_MOMENTUM`, and
`CATALYST_REVIVAL`; Genesis and Radar remain internal/queryable. The read-only boundary
is unchanged: there is no transaction signing, swap, approval, private-key, seed, or
fund-transfer path.

A read-only, autonomous intelligence service for Solana and BNB Chain memecoins. Gambit Jr now starts
at the launch event, performs a low-latency T0 decision, enriches evidence in parallel, builds wallet,
creator, and narrative context, estimates survival and convexity, and follows every promoted setup
through measured outcomes. Discord is the complete supported product surface. Jr never connects to a
wallet and contains no transaction, signing, order, swap, purchase, or sale path.

## V1.4 completion and operational boundary

The V1.4 code path is covered by deterministic migration, lifecycle, promotion, replay, provider,
graph, anti-lookahead, delivery-idempotency, two-restart, 1,000-candidate, and 10,000-event tests.
Live provider acceptance can prove public Solana/BNB discovery and market plumbing. Actual Discord
delivery, a naturally qualifying live token, and operator-verified BNB launch-factory deployments
remain credential/external-event dependent; no threshold is weakened to manufacture that evidence.

Implemented:

- bounded, deduplicating launch-event ingestion with source/receive/candidate/T0 latency evidence;
- Solana `logsSubscribe` Pump.fun program adapter and a reconnecting read-only path;
- configurable BNB factory-log polling fallback for operator-verified Four.meme/Flap deployments;
- authoritative `DISCOVERED → GENESIS_RADAR → HOT_RADAR → PRIORITY_RADAR → QUALIFIED_SIGNAL`
  promotion with separate entry timing and evidence confidence;
- explicit Genesis cards labelled extremely early, high uncertainty, and not qualified signals;
- T0 plus staged/parallel enrichment, immutable call snapshots, and versioned feature/provenance data;
- first-buyer cohort schema, connected-wallet graph, cluster memory, and false-positive avoidance;
- creator history/quality, narrative leader election, clone/saturation penalties, and capital rotation;
- survival/rug-warning and payoff/convexity engines whose `UNKNOWN` state is never coerced to bad;
- right-tail recall, missed-runner, comparable benchmark schema, qualified 2x precision, and small-sample warnings;
- `/menu`, `/help`, `/scan` with Refresh/Watch, `/compare`, `/watch`, `/unwatch`, `/watchlist`,
  `/wallet`, `/clusters`, `/creator`, and `/narrative` in the burnt-orange Gambit visual system;
- `GENESIS_ALL`, `HOT_PLUS`, `PRIORITY_PLUS`, and `QUALIFIED_ONLY` guild alert policies, with legacy aliases;

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
- mobile-first Discord embeds/buttons and all operational slash commands;
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

The V1.4 production path is:

`attention → launch event → T0 → parallel intelligence → wallet/creator/narrative graph → survival → payoff → Genesis → Hot → Priority → Qualified → outcome attribution`

Launch events are stored before candidate creation with `source_event_timestamp`, `source_received_at`,
and `candidate_created_at`. The event queue is bounded and deduplicated. Solana uses official RPC
PubSub semantics; BSC's public endpoints may disable `eth_getLogs`, so the BNB direct adapter stays
disabled until an operator supplies a WebSocket/log-capable RPC plus verified factory addresses and
event topics. DexScreener and GeckoTerminal remain independent polling fallbacks.

T0 permits incomplete evidence only for Genesis. `UNKNOWN` remains unknown. Terminal contract safety
evidence still rejects; connected wallets are warnings and context, not automatic rejection. Entry
state is independent from confidence, and `CHASING` or `LATE` can never qualify regardless of score.
Thresholds are versioned and never self-modify in V1.4.

### Discord-first workflow

Run `/menu` to discover the product. `/scan` evaluates any supported contract in parallel without
creating a candidate, Radar event, signal, or performance record; Refresh repeats that isolated scan,
and Watch adds only a user/guild watchlist row. Automatic alerts go to one configured channel per
guild, while commands work in any permitted guild text channel. `/test-alert` remains a delivery-only
audit and never creates intelligence.

Candidate states retain the V1.3 lifecycle values and add the authoritative V1.4 promotion states
`GENESIS_RADAR`, `HOT_RADAR`, `PRIORITY_RADAR`, and `QUALIFIED_SIGNAL`. Legacy signal classes
`WATCH`, `STRONG`, and `HIGH_CONVICTION` remain compatible. Mint/freeze authority and verified excessive
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

Commands include `/menu`, `/help`, `/scan`, `/compare`, `/watch`, `/unwatch`, `/watchlist`,
`/wallet`, `/clusters`, `/creator`, `/narrative`, `/status`, `/performance`, `/candidates`,
`/rejections`, `/missed`, `/radar`, `/runners`, `/failed`, `/token`, `/smartmoney`, `/setup`,
`/server-settings`, and `/test-alert`.
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
| Solana JSON-RPC/PubSub | Free public endpoint but heavily rate-limited | direct launch logs, mint configuration, supply, largest token accounts |
| BSC JSON-RPC | Free public endpoint but rate-limited | verified factory logs, bytecode existence, and standard owner probe |
| Dedicated Solana RPC | Optional paid/free-tier upgrade | better reliability for top-account distribution |
| Helius / Alchemy / Shyft / Solana Tracker | Optional configured free/credit tiers | primary, secondary and tertiary Solana transport plus independent corroboration |
| Birdeye / Solscan | Optional configured credit tiers | plan-exposed actor/holder evidence and indexed cross-checks; enrichment only |
| CoinGecko | Keyless or optional demo key | slow SOL/broad-market regime context only |
| Neynar / YouTube / Telegram public web / Mastodon / Bluesky | Optional research sources | normalized PIT social evidence; no hard-coded signal weight |

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

Set `DISCORD_TOKEN` for slash commands. Run `/setup` with Manage Server permission in each guild to
designate its one automatic-alert channel and alert tier. A webhook
can send one-way alerts, but slash commands require a bot token. Invite the bot with
`bot` and `applications.commands` scopes. Commands work in permitted guild text channels;
automatic alerts go only to the guild's configured alert channel.

Start in shadow mode first:

```bash
memecoin-bot once --output evidence/live-shadow.json
memecoin-bot run
```

Run and inspect the durable V1.5 research loop separately from the public runtime:

```bash
python -m memecoin_bot.convergence run
python -m memecoin_bot.convergence status
python -m memecoin_bot.convergence providers --probe
python -m memecoin_bot.convergence historical
python -m memecoin_bot.convergence historical-plan
# Requires an explicit range/cap, DUNE_DRY_RUN=false, and --execute:
python -m memecoin_bot.convergence historical-pilot --execute
python -m memecoin_bot.convergence champion
python -m memecoin_bot.convergence metrics
python -m memecoin_bot.convergence audits
python -m memecoin_bot.convergence report
```

The convergence store is shadow/research-only and cannot route public signals. Missing optional
credentials block only their provider jobs. See [`docs/FREE_PROVIDER_SETUP.md`](docs/FREE_PROVIDER_SETUP.md)
for the small worthwhile provider set and [`docs/v15-code-audit-final.md`](docs/v15-code-audit-final.md)
for the final architecture, security, dependency, database and performance audit.

Safe defaults are `SHADOW_MODE=true`, `OPERATOR_SHADOW_ALERTS_ENABLED=false`, and
`PUBLIC_ALERTS_ENABLED=false`. A research shadow call is persisted evidence only;
operator shadow and public Discord delivery are separate explicit routes. There is no wallet, private key,
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

The optional database-backed, read-only Radar Board runs on `RADAR_BOARD_PORT` when enabled and serves
`/api/status`, `/api/radar`, and `/api/token?address=<CA>`. Discord adds `/radar`, `/runners`,
`/failed`, `/token`, and `/smartmoney`; alert cards include chain-aware GMGN links. Configure
guild alert channels with `/setup`; delivery success and retry state are persisted independently
per guild/channel. Legacy `DISCORD_CHANNEL_IDS` remains a fallback only until guild settings exist.

These V1.2 qualification defaults are unchanged: WATCH 65, STRONG 75, HIGH_CONVICTION 85, and
minimum confidence 0.60. Social presence or a single labelled wallet cannot independently create
Priority, and terminal safety evidence cannot be overridden by smart-money evidence.

## V1.3.1 lifecycle, signal quality, and Discord

Migration `005_v131_signal_quality_discord.sql` adds explicit candidate attempts, bounded
exponential retry state, scheduling lanes, auditable startup reconciliation, explicit provider
states, confidence history, narrative/news/catalyst evidence, ongoing Radar outcomes, guild
settings, and guild-scoped delivery identities. Existing V1.3 tokens, candidates, Radar events,
signals, outcomes, provider evidence, and outbox history are retained.

`last_attempted_at` is written before provider I/O. Candidate max age is checked before missing-pair
or provider-error returns. Missing pairs and provider failures persist `next_retry_at` with 30s,
60s, 120s, and bounded later delays. Startup reconciliation transitions old pre-signal rows to
`EXPIRED` with `STALE_PENDING_RECONCILIATION`, retains the prior reason, and cannot duplicate the
transition. Qualified `SIGNALLED` entities are tracked separately and are not expired as stale
pre-signal candidates.

The scheduler reserves capacity for fresh launches, active Radar entities, and near-signal setups,
then fills remaining capacity across active and retry lanes with chain round-robin selection and
per-chain caps. A retry backlog therefore cannot starve fresh Solana or BNB launches.

Every intelligence pillar has score, confidence, evidence, unknowns, risks, and freshness. Overall
convergence requires independent pillar diversity. Setup grades (`C` through `A+`) persist their
component explanation and apply explicit entry-timing penalties for EXTENDED, CHASING, and LATE.
Narrative context separately records identity, cluster, freshness, velocity, saturation, decay,
first-mover/copycat state, peers, and provenance. News/catalyst tables accept only
provenance-backed read-only evidence; the service does not synthesize headlines or sentiment.

Discord is the primary frontend. Every command renders an embed, `/token` and `/smartmoney` expose
structured evidence without raw JSON, and `/status` separates live pipeline, pending ages,
providers, Discord delivery, lifetime counts, and state reconciliation. Provider states are
`HEALTHY`, `DEGRADED`, `DOWN`, `DISABLED`, `UNKNOWN`, `RATE_LIMITED`, or `CIRCUIT_OPEN`; configured-off
GMGN is `DISABLED` and excluded from the configured-health denominator.

One global scanner serves all guilds. New guilds receive commands but no automatic alerts until an
administrator runs `/setup`. Delivery identity is `(outbox event, guild, channel)`, successful guilds
are never retried, and one guild failure does not block another. Tiers are `ALL`, `HOT`, `PRIORITY`,
and `QUALIFIED`. `/test-alert` writes only a test-delivery audit row and cannot create a token, Radar
event, signal, or market outbox event.

The HTTP Radar Board is disabled by default in V1.3.1 because Discord contains the supported
operational UI. Qualification defaults remain exactly WATCH 65, STRONG 75, HIGH_CONVICTION 85, and
minimum confidence 0.60.

### GMGN rate-limit assumptions

GMGN's official token skill documents a leaky bucket with rate/capacity 20 and route weights of 1
for info/security/pool and 5 for holders/traders. Jr applies caching, per-token in-flight dedupe,
bounded concurrency, exponential retry/backoff, 429 handling through the resilient client, and a
circuit breaker. Static enrichment defaults to a 120-second TTL. Operators should lower concurrency
or increase TTL if their issued key has stricter limits.

### V1.4 deployment and rollback

Before deploying, stop the prior process and back up the SQLite database plus WAL/SHM files. Check out
`codex/gambit-jr-v1.4-alpha-engine-discord`, configure `.env`, keep `SHADOW_MODE=true`, and run
`docker compose up -d --build`. Verify ports 8080 and 8081, provider health, state reconciliation,
Discord commands, and restart recovery. GMGN may remain disabled; direct BNB launch ingestion must
remain disabled until verified factory addresses/topics and a log-capable RPC are configured.

Rollback is application-first: stop V1.4, check out the prior release, and start the old container
against a pre-upgrade database backup. Migration 006 is additive, but restoring the backup is the
supported rollback because older application code does not own V1.4 tables.

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
Discord operation: `DISCORD_TOKEN`, followed by `/setup` in each guild. Required runtime configuration:
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

The service runs a durable canonical realtime worker, native chain sources, one broad
discovery loop, an adaptive candidate loop, one low-cadence outcome loop, and one signal
tracker. Native Pump program logs and curve accounts drive early Solana state. Optional
PumpPortal new-token/migration events reconcile into the same event identity. Selective
Helius monitoring uses standard `logsSubscribe` plus `getTransaction`; paid enhanced
`transactionSubscribe` is not required. Configured BNB factories use `eth_subscribe` with
the existing persisted `eth_getLogs` cursor as gap recovery.

Candidate history is bounded by `SNAPSHOT_HISTORY_LIMIT`, candidates by global and
per-chain limits, and SQLite remains single-instance. GENESIS/HOT/WARM/COLD/DEAD state
sets each candidate's next due time, while DexScreener requests are grouped in batches of
at most 30. Run one Compose replica only.

Offline event replay and the human-gated challenger lab are explicit commands:

```bash
python scripts/v15_realtime_replay.py --database RESEARCH.duckdb \
  --corpus Pumpfun_Memecoin_Corpus --output replay-manifest.json
python scripts/v15_realtime_research.py --database RESEARCH.duckdb \
  --output realtime-trajectory-results.json
memecoin-bot realtime-research --output outputs/operational-realtime-challenger.json
python scripts/v15_realtime_probe.py --seconds 60 --database probe.db \
  --output probe.json
```

All challengers persist with `public_route=false`; neither command promotes a production
feature. Transaction-only replay is labelled `HISTORICAL_PROXY` and never substitutes
market cap or virtual reserve values for native real-SOL account state.

## Operational acceptance checklist

Before claiming V1.2 live acceptance complete, retain evidence for a naturally qualifying real token,
Discord message ID, immutable DB snapshot, subsequent market updates, service restart,
non-duplicated milestones, Discord reconnect, and an independently running cloud
container. Do not lower thresholds to manufacture that evidence.

# V1.5 finalization truth report

Evidence date: 2026-08-28 UTC. Raw provider payloads and SQLite files are intentionally excluded
from Git. Reproduce the acquisition with `gambit-history finalize-public`.

## Completion levels

| Level | State | Evidence |
|---|---|---|
| Code complete | PASS | Credential-free adapters, all-table Jr importer, manifests, normalization, research, challenger, operator status, staging profile, frontend fixtures and release gates exist. |
| Data complete | FAIL | No read-only copy of the production Jr database was available; the free public token corpus is not launch-complete and lacks transaction-level wallet/creator/funder/social history. |
| Research complete | FAIL | One real exploratory walk-forward run executed, but the unbiased failure corpus and several intelligence-family ablations are unavailable. |
| Staging complete | FAIL | The isolated profile and acceptance executable exist, but no staging container was deployed during this Windows-host task. |
| Production ready | FAIL | Zero features are approved and zero prospective challenger decisions exist. Do not deploy this branch yet. |

## Real coverage acquired locally

| Dataset | Time range UTC | Entities | Observations | Raw bytes | Bias / use |
|---|---:|---:|---:|---:|---|
| Binance BTCUSDT daily | 2021-01-01 to 2026-08-28 | 1 | 2,068 | 1,858,203 | Complete selected-symbol regime history |
| Binance SOLUSDT daily | 2021-01-01 to 2026-08-28 | 1 | 2,067 | 1,838,160 | Complete selected-symbol regime history |
| Binance BNBUSDT daily | 2021-01-01 to 2026-08-28 | 1 | 2,067 | 1,838,519 | Complete selected-symbol regime history |
| GeckoTerminal Solana new-pool snapshots | 2026-08-28 01:57:22 to 02:05:51 | 277 | 315 | 683,579 | Latest-ten-page rolling window only |
| GeckoTerminal Solana ranked snapshots | 2026-08-28 01:59:02 to 02:07:50 | 200 | 232 | 497,589 | Current provider ranking; survivor-selected |
| GeckoTerminal ranked-pool daily OHLCV | 2026-03-01 to 2026-08-28 | 38 | 1,584 | 1,506,239 | High current-ranking survivorship bias |

Totals: 8,333 raw observations, 8,222,289 raw archive bytes, a 40,910,848-byte
warehouse, 7,741 normalized events, 31,070 point-in-time feature rows and 10 outcomes.
The launch snapshot values use acquisition time, not pool creation time. Daily candles become
available only at candle close. Both rules prevent current values from being backdated.

## Real exploratory research result

Strict windows were:

- train: 2026-03-04 to 2026-04-24, 7 outcomes;
- validation: 2026-04-24 to 2026-08-21, 2 outcomes;
- untouched test: 2026-08-21, 1 outcome.

The real cohort contained seven sub-1.5x survivors, one 1.5x outcome, one 3x outcome and one
50x+ outcome. It contained zero directly observed rugs or price-collapse failures, which itself
demonstrates that the current-ranked-pool universe is not suitable for production research.

On the one-item untouched test window, deterministic random, volume, momentum and
safety-filtered momentum baselines all selected one item and had 0% 5x precision. Market-cap
and liquidity baselines were unavailable because historical values were not present. The full
model could not be fitted because the training window contained no 5x runner. All wallet,
creator, funding, narrative, buyer, survival and payoff ablations are marked unavailable rather
than assigned invented effects. Regime rows existed, but the sample was too small to estimate an
incremental contribution.

Leakage state was PASS. The daily-candle live-usability adjustment used a truthful 86,400-second
decision delay and a documented 2% execution haircut; adjusted 5x rate across all ten outcomes
was 10%. No pool-specific impact or sellability history was available. Drift warnings appeared
for initial momentum, volatility and volume acceleration; BTC/SOL regime returns were stable in
this tiny comparison.

Approval decision: `RESEARCH_ONLY`. Approved production features: **0**. This is the required
truthful negative decision; no edge is claimed.

Latest local latency measurements used 50 calls: coverage query p50/p95
0.110/0.186 ms; offline warehouse point-in-time lookup 39.521/52.835 ms; separate approved-context
empty lookup 0.007/0.010 ms; Discord payload rendering p50/p95 0.025/0.044 ms.
The approved store and payload render are comfortably below the 25 ms live
budget. The broader research warehouse is offline and must not enter the live critical path.
Rate-limited acquisition across 13 completed jobs had median throughput 1.63 records/second.
Discovery-to-candidate, candidate-to-T0, T0-to-decision and Discord delivery delay require the
production operational database and therefore remain UNKNOWN in this local evidence run.

## Missing evidence and exact requirements

| Source | Requirement | Cost class | Expected gain |
|---|---|---|---|
| Existing Jr history | Read-only copy of production `DATABASE_PATH`; import with `ingest-operational-all` | No provider cost | Highest: actual calls, rejections, misses, outcomes, wallets and creators |
| CoinGecko on-chain expanded history | `COINGECKO_API_KEY` with a plan supporting pagination beyond page ten | Paid subscription | Broader launch universe and fewer rolling-window gaps |
| Birdeye historical | `BIRDEYE_API_KEY`, coverage determined by plan and licensing review | Limited free or paid | Trades, holders, wallet participation and deeper token OHLCV |
| Dune Solana exports | `DUNE_API_KEY` plus operator-reviewed query IDs | Free-tier or paid compute | Dead/non-runner launches, trades and actor relationships |
| Historical social archive | Provider-specific archive credential and usage rights | Paid archive | Pre-launch preparation, unique mentioners and legitimate velocity |

Current social metadata remains current metadata. It is never converted into historical velocity.
Credentialed ingestion is restart-safe and keeps keys in environment variables:

```bash
DUNE_API_KEY='...' gambit-history ingest-dune --query-id 123 --chain solana \
  --entity-field token_address --observed-at-field block_time
BIRDEYE_API_KEY='...' gambit-history ingest-birdeye-ohlcv \
  --addresses operator-reviewed-addresses.json \
  --start 2024-01-01T00:00:00+00:00 --end 2026-01-01T00:00:00+00:00
```

If Dune output lacks a source availability timestamp, rows become available only at acquisition
time. This conservative default prevents index results downloaded today from leaking into an old
decision. API keys are headers, never URL parameters or persisted provenance.

## Shadow challenger gate

The challenger has no alert-routing interface and persists `public_alert_routed=false`. Promotion
requires at least 250 prospective decisions, at least 30 live days, p95 lookup latency no greater
than 25 ms, matured outcome comparisons, drift review and manual approval. Current prospective
sample: 0.

## Staging

Create `.env.staging` from `.env.staging.example`, use only a dedicated test Discord guild/channel
if desired, then run on a Linux staging host:

```bash
docker compose -f docker-compose.staging.yml up -d --build
SHADOW_SEND_ALERTS=false python scripts/staging_acceptance.py \
  --health-url http://127.0.0.1:18080/health \
  --operational data/staging/operational/memecoin.db \
  --warehouse data/staging/historical/warehouse.db \
  --approved data/staging/approved/approved_features.db
```

Teardown removes containers while preserving isolated volumes:

```bash
docker compose -f docker-compose.staging.yml down
```

Use `down --volumes` only when intentionally discarding all staging data. No production mutable
volume is referenced by the staging Compose file.

## Production deployment gate

Do not use the production block until this report shows DATA COMPLETE, RESEARCH COMPLETE,
STAGING COMPLETE and PRODUCTION READY. When those states are genuinely satisfied, the single
host-side release entry point is:

```bash
export GAMBIT_RELEASE_SHA='<manually-approved-immutable-sha>'
export GAMBIT_PRODUCTION_DIR='/opt/gambit-jr'
cd "$GAMBIT_PRODUCTION_DIR"
bash scripts/release_v15.sh
```

The script verifies the remote SHA, backs up the operational database, builds, deploys, checks
health, runs production acceptance and rolls back on deployment or health failure. Discord live
acceptance remains a separate human-observed gate using `scripts/live_discord_acceptance.py`.

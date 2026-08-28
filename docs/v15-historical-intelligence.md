# V1.5 historical intelligence and production feature governance

V1.5 separates three trust and performance domains:

1. `DATABASE_PATH` is the live operational SQLite database.
2. `HISTORICAL_WAREHOUSE_PATH` plus `HISTORICAL_ARCHIVE_PATH` are offline research storage.
3. `APPROVED_FEATURE_STORE_PATH` is the small, indexed live context store.

The live bot never queries raw historical evidence or research tables. Its only historical read is a
point-in-time query against explicitly approved, versioned feature snapshots. The default lookup
budget is 25 ms. A database error, invalid timestamp, missing row, expired row, or budget overrun
falls back to live evidence and is written to `production_context_audit`.

## Historical coverage map: shipped state

No third-party historical export, paid subscription, or five-year dataset is present in this
repository. Therefore the honest shipped coverage is zero imported external observations. Jr does
not manufacture history and does not relabel current state as point-in-time history.

The first legitimate source is Jr's own accumulated `token_snapshots`. Transfer it with
`gambit-history ingest-operational`. These rows are marked first-party observed history, start at the
earliest actual operational snapshot, and explicitly record the missing range before that date.
Provider exports enter through `gambit-history ingest-jsonl`; each dataset manifest must state the
provider, chain, acquisition and refresh methods, precision, reliability, completeness estimate,
history kind, point-in-time safety, missing ranges, rate limit, and cost.

`gambit-history coverage` reports actual earliest/latest timestamps, distinct entities, observations,
missing ranges, reliability, completeness, and storage metadata. Counts only change after real rows
are ingested.

## Pipeline and invariants

The pipeline is:

`raw archive → normalized events → canonical entities → PIT features → outcomes → research catalog → manual approval → live feature snapshots`

Raw payloads are canonicalized, SHA-256 hashed, content-addressed, and preserved outside the query
database. Raw evidence, normalized events, PIT features, outcomes, approvals, and published live
snapshots are immutable. Backfills are paginated, deduplicated, retryable, checkpointed, and resumable.

At decision time `T`, a feature is selectable only when both `observed_at <= T` and
`available_at <= T`. Outcomes must be measured after the decision and cannot become available before
their measurement completes. Walk-forward windows must be strictly ordered and disjoint. Any future
feature presented to the research engine raises `LeakageError` and the run is not catalogued.

UNKNOWN remains distinct from zero or negative evidence. Wallet and creator reputations filter their
input by `available_at`, require matured outcomes, retain sample size, and shrink small samples toward
neutral. Graph results use observed-relationship language and never infer common ownership.

## Research and promotion

The repeatable research engine catalogs dataset, feature, rules and code versions; provider set;
chronological train/validation/test windows; methodology; right-tail denominators and rates; runner
fingerprints; simple baselines; ablation inventory; limitations; and leakage state. Results remain
descriptive until sufficient prospective scoring is available.

Research cannot modify production. Promotion requires a new immutable approval record containing a
manual approver, research run, positive sample, walk-forward evidence, limitations, target stage and
target feature. Production merge policies are:

- `EXPLANATION_ONLY`: expose context without changing a decision input.
- `FILL_UNKNOWN`: fill a missing live feature only.
- `BOUNDED_BLEND`: mix an approved historical prior into a known live value.

The historical contribution is schema-limited to 25%. Shadow challenger decisions are stored
separately and never route alerts. Drift observations are segmented and versioned rather than used to
rewrite thresholds automatically.

## Data acquisition order

The information-gain/cost order is:

1. Existing Jr observations: $0 and highest provenance confidence, but only from Jr's first observed
   date.
2. Dune: useful for research SQL over raw/decoded and curated Solana/EVM history; its catalog includes
   DEX trades and minute/hour/day prices, but long-tail pricing applies coverage filters. Start with
   free exploration and price the API only after a corpus query is proven.
3. Helius: Solana raw/enhanced transaction reconstruction. As of the documented plan page, Free is
   $0/1M credits, Developer $49/10M, Business $499/100M and Professional $999/200M. Buy Developer only
   after estimating transactions per launch cohort.
4. Birdeye: direct token/pair OHLCV, historical price, and liquidity history. Endpoint access and
   compute-unit cost vary by package; validate inception depth for the exact long-tail universe before
   purchase.
5. Bitquery: highest-value candidate for older DEX trades, pair creation, pool and account-level
   reconstruction. Its Solana aggregate archive is documented from May 2024, with separate older
   transfer access and field restrictions, so it is not honestly a universal five-year Solana market
   dataset.
6. Moralis: convenient Solana OHLC, historical token score/holders, swaps, snipers and launch lists.
   It is a lower engineering-cost alternative; compare compute-unit cost and observed universe
   coverage against Birdeye before subscribing.

Provider references: [Dune catalog](https://docs.dune.com/data-catalog/overview),
[Helius pricing](https://www.helius.dev/pricing),
[Birdeye historical market endpoints](https://docs.birdeye.so/reference/price-ohlcv),
[Birdeye compute units](https://docs.birdeye.so/docs/compute-unit-cost),
[Bitquery Solana historical limits](https://docs.bitquery.io/docs/blockchain/Solana/historical-aggregate-data/),
and [Moralis Solana API costs](https://docs.moralis.com/data-api/pricing).

Historical X, Telegram, Discord, insider, bundler and proprietary wallet-label archives remain UNKNOWN
unless the operator supplies a legitimate licensed source with availability timestamps. Static social
metadata is never converted into historical velocity.

## Discord runtime closure

Fast slash commands use Discord's initial interaction response, eliminating an unnecessary webhook
edit from `/menu`, `/help`, `/status`, `/performance`, `/radar`, and `/watchlist`. Provider-bound
`/scan` and `/compare` defer inside the three-second acknowledgement window. Deferred command results,
menu navigation, and Refresh use an exact original-response PATCH containing only present content,
embeds, components, and disabled mentions; generic send-only defaults and null placeholders are not
serialized. This follows Discord's [interaction response lifecycle](https://docs.discord.com/developers/interactions/receiving-and-responding)
and [webhook edit contract](https://docs.discord.com/developers/resources/webhook).

Signal cards put token/name, V1.5 tier, chain and the full contract first, followed by market cap,
liquidity, entry, runner/failure, survival, why-now, risks and truthful historical context. The action
row is Copy CA, DexScreener, GMGN, Explorer, Watch. Discord does not expose a bot-side clipboard API;
Copy CA returns the exact address alone in an ephemeral code block so mobile/desktop users can copy it
without extracting it from the card. Persistent stateless routers restore Copy, Watch, menu, Refresh
and scan Watch after a restart.

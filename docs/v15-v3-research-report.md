# Intelligence V3 evidence-led research checkpoint

Generated 2026-08-28. This is a research-only checkpoint. `CONTROL_V15` and
`INTELLIGENCE_V2` are frozen; no V3 public notifier route exists; production deployment remains
prohibited.

## 1. Code and decision-path audit

The live path has four separate manual scoring/threshold families and ends in a legacy-AND-V1.5
signal gate. V1.5 averages only known features, so missingness changes effective weights. V2 uses
an 80,000-row positive-preserving/negative-striding fitting sample, validation-selected percentile
blends, isotonic calibration after the changed fitting prevalence, and one short split. Social
currently has link presence but no velocity provider; narrative has keyword identity but no measured
trend. USD liquidity level is not real-reserve velocity.

V3 now has one explicit `V3DecisionEnvelope` containing nomination, competing hazards,
actionability, utility, confidence, coverage, uncertainty, abstention, positive/negative/hard-risk
evidence, model/feature versions, evidence time, legacy comparison, V1.5 comparison, risk cap, and
precision-gate result. Legacy and V1.5 values are comparison fields and cannot veto V3 research.

No active CONTROL threshold, V2 source, live signal condition, or Discord public route was changed.

## 2. Evidence and data acquired

The existing ignored research store contains:

- Pump.fun corpus: 798,430 launches, 33,581,765 transactions, 26,934,864 pre-graduation
  snapshots, 166,751 creators and 5,689 graduations.
- MELT local research subset: 41,470 migrated launches, 3,328,964 coordination edges and
  74,183,825 bytes of feature Parquet; restricted to research by CC BY-NC 4.0.
- Trenches forward corpus: 730,850 point-in-time observations with its documented 69.09-hour
  outage.
- Newly acquired RED-PUMP-2026-v1.4: raw May 8–June 10 2026 launch/outcome files plus v1.4
  corrigendum, limitations, licence and checksums. Launch SHA-256 is
  `042940379e8c897ac97403e6b25a5b302fb32b6902a8fc0cef4ab70ac11e8f84`; outcome SHA-256 is
  `c0a327ea442d91c6f970b2bad9a2a9b778e163d8c3eb38f71eccd3e92209a974`.

No read-only production database copy, broad BNB universe, Catching-the-Rug release, proprietary
social history, or Solana Tracker API key was supplied. These remain absent, not simulated.

## 3. Primary-source reproduction

Using the v1.4 raw files and the authors' documented malformed-row/deduplication rules reproduced:

| Quantity | Reproduced result |
|---|---:|
| Raw launches | 860,213 |
| Unique launches | 860,194 |
| Outcome CSV rows | 833,171 |
| Malformed / blank / invalid-duration outcomes | 140 / 75 / 1 |
| Valid unique joined outcomes | 832,941 |
| Observed fast-window graduations | 1,651 |
| Observed rate | 0.198213% |
| Telegram absent | 1,350 / 812,671 = 0.166119% |
| Telegram present | 301 / 20,270 = 1.484953% |

This does **not** reproduce a 24-hour rate. The corrected source proves the newest-50 polling
collector lost visibility after roughly six minutes. The Telegram result is association, not causal
attention velocity. The withdrawn virtual-market-cap correction is not used.

Marino data/code, Catching-the-Rug data/code, the order-flow panel and proprietary social panels
cannot be reproduced locally. Their results remain design evidence only. Full source, licence and
applicability details are in the source-feature matrix.

## 4. Implemented research components

- Point-in-time values fail closed on future availability.
- A regularized discrete-time competing-risk baseline supports time-varying covariates and six
  event types. It emits uncalibrated/unvalidated state until sealed calibration is supplied.
- Actionable outcomes support explicit provider/model/Discord/user delay, fees, constant-product
  impact, sellability, severe drawdown, liquidity collapse, terminal failure and censoring.
- Real reserve, virtual reserve, market cap, liquidity and price are separate. Native 15/30/60/90s
  and 3/5/10/30m order-flow windows never interpolate unavailable observations.
- Raw and independent buyers, wash-adjusted volume, creator-linked flow, bundle-linked flow,
  repeated tiny buys and buy/sell recycling remain continuous before adversarial flags.
- Wallet selection rejects invalid PnL, developers/insiders/hackers/arbitrage/pools, thin samples,
  stale activity, concentrated one-token PnL and excessive drawdown. Copyability requires
  15/30/60/120-second follower outcomes. Linked wallets collapse to one independent component.
- Social architecture separates infrastructure, independent attention, velocity, sentiment,
  bot-adjusted sentiment, official activity and investor activity with evidence timestamps.
- Narrative history exposes identity, birth, capital-flow velocity, leader, saturation, revival,
  catalyst and decay components but deliberately emits no composite score before validation.
- Nested walk-forward utilities enforce maturity embargoes, creator/funder/wallet-cluster group
  isolation, natural-prevalence sampling weights, calibration reports, Wilson intervals and the
  required precision/frequency frontier grid.

## 5. Shadow integration and invariants

For each completed real service evaluation, V3 persists CONTROL/legacy, V1.5, a named V2-not-run
state, V3 envelope, available features, latency fields and veto reasons. With no sealed V3 model,
the envelope abstains as `UNVALIDATED_RESEARCH_MODEL` or `NO_V3_MODEL_FORECAST`.

The shadow table has `CHECK(public_route = 0)`, a rejecting update trigger, no foreign key to
signals or outbox, and Store checks that the outbox row count does not change during persistence.
V3 therefore cannot notify the public surface.

## 6. Actual research results

No V3 model was fit or calibrated on the incomplete evidence, so no V3 Premium, Strong, 2x/5x/10x,
20x/50x recall, failure, drawdown, frequency, utility, copyability or calibration result exists.
Counts are zero prospective V3 signals and zero approved V3 features. This is intentional
abstention, not a zero-percent performance estimate.

Frozen earlier results remain unchanged: the latest real-corpus checkpoint tested 112,859 launches
in three retired 48-hour diagnostic windows. CONTROL reconstruction produced 14.27% 2x precision
(95% Wilson 12.35–16.44%); the earlier candidate produced 16.22% (14.19–18.49%). Neither qualifies
as sealed evidence or meets the public gate. No new comparison is claimed from those retired windows.

Liquidity velocity, adjusted order flow, wallet copyability, creator/funder, social, sentiment,
narrative, regime, failure and event-time findings are therefore **not validated**. The implementation
and tests establish contracts, not edge.

## 7. Validation truth

| Gate | Result |
|---|---:|
| Code complete against the full mission | **FAIL** — real provider/backfill/model evidence remains missing |
| Data complete | **FAIL** |
| Liquidity velocity validated | **FAIL** |
| Order flow validated | **FAIL** |
| Wallet copyability validated | **FAIL** |
| Social/narrative validated | **FAIL** |
| Failure model validated | **FAIL** |
| Public core 2x >=60% | **FAIL / NOT TESTED** |
| Premium >=70% | **FAIL / NOT TESTED** |
| Strong >=55% | **FAIL / NOT TESTED** |
| 5x / 10x / right-tail targets | **FAIL / NOT TESTED** |
| Sealed validation | **FAIL** |
| Challenger ready | **FAIL** |
| Production ready | **NO** |

## 8. Conditions before challenger and production

Before challenger: acquire the read-only production copy; establish legitimate PIT real-reserve,
pool/liquidity, wallet/funder/bundle, social provenance and latency coverage; build multiple
platform/regime outer windows; fit on natural prevalence; calibrate only on inner untouched data;
run group-purged sealed tests; meet coverage, actionability and calibration criteria; and reproduce
incremental family ablations on later evidence.

Before production: at least 250 matured core shadow signals, three later sealed seven-day windows,
two regimes, the stated Wilson-interval precision gates, controlled catastrophic-failure rate,
non-concentrated narratives/entities, useful frequency, green chaos/load/restart/staging, explicit
licensing approval for every training source, and a separate human production approval. None of
those conditions is currently satisfied.

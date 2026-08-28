# V1.5 real historical evidence checkpoint

Generated on 2026-08-28 from checksum-pinned public releases. Raw corpora, the SQLite
warehouse, archives, and generated JSON reports are ignored and are not committed. This work does
not change live scoring, feature approvals, the signals-first Discord UX, or production state.

## Decision

| Gate | State | Evidence |
|---|---:|---|
| Code complete | PASS | Reproducible verifier, materializer, cohort summaries, baselines, ablations, drift and walk-forward runner. |
| Data complete | FAIL | No production Jr DB copy, no broad BNB launch corpus, limited calendar coverage, and no raw trades for the 798k-launch corpus. |
| Research complete | FAIL | Two short Solana walk-forwards and three differently sampled public releases are insufficient for approval. |
| Historical features approved | **0** | Every candidate remains `RESEARCH_ONLY`; corrupt, stale and look-ahead fields are rejected. |
| Challenger ready | FAIL | No approved feature and no prospective shadow decisions. |
| Production ready | FAIL | Offline evidence work only; production deployment remains prohibited. |

## Verified source inventory

| Public release | Selected files | Verified bytes | Coverage/use | Material limitation |
|---|---:|---:|---|---|
| [Trenches forward corpus](https://huggingface.co/datasets/Tr4m0ryp/trenches-pumpfun-forward-2026-08) | 9 | 104,580,833 | 730,850 point-in-time observations; decoded trade and funder memory | Vendor-enriched sample and documented 69.09-hour outage |
| [MELT](https://huggingface.co/datasets/Zinteck/MELT) | 4 | 293,856,476 | 41,470 launches and 3,328,964 coordination edges | CC BY-NC 4.0 research use; migration-time features; creator column is constant |
| [Pump.fun Memecoin Corpus](https://huggingface.co/datasets/Slinky21/Pumpfun_Memecoin_Corpus) | 5 | 383,280,266 | Exhaustive launch rows for 2026-06-05 through 2026-07-14 | Solana/Pump.fun only; July 3 outage; raw trade partitions not acquired |
| **Total selected** | **18** | **781,717,575** | All files SHA-256 verified before research or materialization | Source releases remain subject to their published limitations |

The direct Pump.fun frontend API was Cloudflare-denied and was not bypassed. Existing Jr history is
still blocked on an operator-supplied, read-only copy of the production SQLite database. The
allowlisted `ingest-operational-all` importer already exists; fixture and test databases were not
misrepresented as production evidence.

## Actual evidence counts

The primary launch universe contains 798,430 launches, 166,751 creators, 5,689 graduations,
170,003 source-published zombies and 109,561 mayhem-mode launches. It includes 792,741
non-graduations and excludes 1,486 concentration-contaminated rows from modeling.

The 5,669 mature post-graduation outcomes contain 2,692 `dead`, 1,072 `pump_dump`, 784
`slow_bleed`, 607 `major_pump`, 320 `minor_pump` and 194 `sustained` rows. This produces 4,548
broad graduated failures and 181 direct source-published rugs. Raw peak cohorts contain 2,134 at
1.5x, 1,671 at 2x, 1,221 at 3x, 808 at 5x, 441 at 10x, 254 at 20x and 113 at 50x. The stricter
one-minute-entry cohort has 3,445 usable outcomes: 1,186 at 2x, 534 at 5x and 275 at 10x.

Other materialized or summarized evidence:

- 1,016,374 wallet identities; stale published wallet activity totals were rejected.
- 342,341 decoded trades, 48,561 wallets, 137,398 wallet–mint first entries and 19,267 repeat wallets.
- 67,722 token–funder relationships across 7,997 funders.
- 46,519 repeat creators and 15,998 creators with at least five launches.
- 1,392,133 liquidity snapshots for 5,701 mints; 1,002,445 have known liquidity and 48 are incomplete.
- 5,701 migration rows, of which only 2,721 contain non-sentinel pool addresses.
- 869,805 MELT co-purchase, 1,370,286 fund-flow and 1,088,873 Jito-bundle relationships.

The local warehouse audit contains 18 raw file evidence records, 2,079,809 canonical entities,
1,801,980 normalized events, 1,801,978 point-in-time feature snapshots and 798,430 outcomes.
Two simultaneous wallet entries consolidate to one valid wallet-memory state at each timestamp, so
137,398 entry events correctly correspond to 137,396 distinct decision-time wallet snapshots.

## Walk-forward findings

Both evaluations are strictly chronological with gaps between train, validation and test. The
first transfers a June 5–20 model to July 5–13 after a regime embargo. The second trains on June
24–July 2 and tests July 10–12 after the documented outage. Models use launch-time fields only.

For July 5–13 graduation ranking, the full model selected no winners (0% precision), while random
was 2.67%, volume-only 26.74%, momentum-only 24.92%, and safety-filtered momentum 48.52%.
For 5x graduates, the full model reached 11.68% precision versus 11.14% random, 14.95% market-cap
only, and 26.42% safety-filtered momentum. The apparent full-model result does not constitute edge.

For July 10–12 graduation ranking, the full model reached 0.79% precision versus 2.62% random,
14.81% volume-only, 25.16% momentum-only, and 49.12% safety-filtered momentum. For 5x graduates,
the full model reached 13.92% versus 7.59% random and 15.38% safety-filtered momentum. Removing
concentration reduced full-model precision by 7.59 percentage points in this one two-day window,
but the result is too narrow, unstable, and weaker than the simple filtered baseline.

Graduated-failure precision was 91.30% in the first test and 83.54% in the second, but failure base
rates were extremely high and the second full model underperformed random/volume (88.61%). This is
not approval evidence. No tested launch feature receives automatic or manual approval.

## Point-in-time and adversarial controls

Excluded as future or outcome-dependent: 20-second, 30-second and one-minute entry prices,
graduation time, seconds-to-graduation, peaks, trade totals and all outcome fields. Uncorrected
concentration values are rejected because of the published 2B-supply error; 1,486 suspect rows are
excluded. MELT's constant creator identity and stale wallet aggregate counts are rejected. The
remaining launch fields lack per-field availability stamps, so even positive offline findings remain
ineligible for approval from these releases alone.

## Cost, storage and blockers

All three selected releases were free to acquire. The compact Pump.fun files use approximately
480.04 bytes per launch, projecting about 48.0 MB per 100,000 launches or 480.0 MB per million;
raw transaction history scales separately. The materialized SQLite warehouse is intentionally a
local research artifact and is materially larger because it stores normalized JSON provenance and
indexes.

Remaining evidence blockers are: the read-only production Jr DB copy; a broad, non-survivor-only
BNB launch/failure universe; longer time coverage and independent providers; full transaction-level
history for the large launch corpus; field-level availability timestamps; and enough subsequent
data for prospective shadow validation. Until those exist, the only defensible decision is
`RESEARCH_ONLY`, zero approved features, no challenger, and no production deployment.

A bounded BNB provider pass confirmed that [Dune](https://dune.com/data) exposes historical BNB
raw/decoded and DEX tables behind its query/API access, while
[Bitquery](https://docs.bitquery.io/docs/cloud/bsc/) documents block-ranged BSC Parquet exports for
DEX pools, trades, transfers, calls and events. Bitquery's anonymous S3 prefix listing returned 403;
its public repository provides only a 50-block tooling sample, not an unbiased research universe.
The [BNB Greenfield archiver](https://github.com/bnb-chain/greenfield-bsc-archiver) exposes raw
historical blocks, but constructing correctly decoded launch, trade, liquidity and failure tables
from it is a separate high-volume backfill. None of these was falsely counted as acquired BNB
history. The next credible BNB acquisition therefore requires reviewed factory/event definitions
plus Dune/Bitquery credentials or a budgeted archive-decoding backfill.

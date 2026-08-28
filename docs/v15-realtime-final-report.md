SECTION 1 — MODEL RESULTS

All comparable rows below are fixed-frequency 1% selections. The new transaction-trajectory
window is June 28–July 15, 2026 (236,141 outer tokens); it was already touched by earlier V3
diagnostics and is therefore retired, not sealed validation.

| Model | 2X | 5X | 10X | 20X recall | 50X recall | Failure | Signals | Frequency |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| CONTROL reconstruction, exact-band corpus | 15.21% | 3.60% | 1.48% | 5.98% | 4.17% | 13.05% | 2,361 | 157.4/day |
| Previous V3 Quick 2X checkpoint | 13.92% | 3.90% | 1.48% | 8.87% | 9.77% | 3.58% | not restated | 1% |
| Previous V3 Mid 5X checkpoint | 14.61% | 5.63% | 2.51% | 14.89% | 15.79% | not restated | not restated | 1% |
| Realtime transaction trajectory | 11.22% | 2.92% | 1.02% | 5.13% | 5.00% | 10.50% | 2,361 | 157.4/day |
| CONTROL x realtime failure filter | 10.93% | 2.84% | 0.97% | 4.70% | 4.17% | 10.42% | 2,361 | 157.4/day |
| Best self-learning challenger | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable | 0 approved | no public route |

The new CONTROL 2X Wilson 95% interval is 13.81–16.71%; the realtime candidate interval is
10.01–12.56%. The candidate and hybrid are both `REJECTED_NO_FIXED_FREQUENCY_LIFT`.
The earlier CONTROL x V3/two-stage precision gate also remains rejected; it was not re-tuned on
the retired outer data. No self-learning challenger is eligible for promotion.

SECTION 2 — WHAT CHANGED INTELLIGENCE

The system now observes and persists ordered trades, real reserve values, buyer arrival, sell
timing, actor identities, migration state, provider timing, and exact early time bands rather
than compressing everything into sparse snapshots. This produced a real risk signal but not a
runner-ranking lift: versus CONTROL, the new trajectory model lowered terminal failure by 2.54
percentage points and improved median adverse excursion from -66.58% to -42.35%, while losing
3.98 points of 2X precision, 0.68 points of 5X precision, and 0.85 points of 20X recall. The
hybrid improved drawdown further to -35.06% but lost still more precision and recall. No feature
was approved.

SECTION 3 — REAL DATA COVERAGE

The licensed local transaction corpus contains 24 Parquet files (6,695,411,376 bytes),
20,400,058 valid ordered events, and 594,163 tokens from 2026-06-05 10:12:28 BST through
2026-07-14 16:02:54 BST. It contains 573,412 tokens with a sell and 460,933 with buyer arrival.
The exact event-band counts are:

| Band (seconds) | Events | Tokens | Raw buyers |
|---|---:|---:|---:|
| 0–15 | 4,693,370 | 451,888 | 2,240,094 |
| 15–30 | 3,162,458 | 339,214 | 1,396,672 |
| 30–60 | 3,369,999 | 291,413 | 1,397,299 |
| 60–90 | 1,760,801 | 176,304 | 703,769 |
| 90–120 | 1,218,394 | 117,603 | 454,235 |
| 120–180 | 1,697,480 | 113,833 | 554,239 |
| 180–300 | 1,634,881 | 125,917 | 547,692 |
| 300–600 | 1,770,666 | 87,851 | 544,541 |
| 600–1800 | 1,092,009 | 68,258 | 317,554 |

The current-code 60-second live native probe produced 66 canonical events: 18 trades, 13 wallet
buys, 5 wallet sells, 18 curve/reserve observations, and 12 health events across 9 tokens. It had
18/18 real-SOL observations but zero creates/creators, migrations, social observations, or exact
bundle identities in that bounded window. Historical real-reserve account states, provider
arrival latency, funder edges, wallet linkage, exact bundles, and social/narrative observations
are explicitly unavailable; they are not imputed from market cap.

SECTION 4 — LOW-PERFORMANCE SELF-REFLECTION

ROOT_CAUSE_1: the trajectory model learned lower-risk flow, not better runner selection. Failure
fell 2.54 points, but 2X precision fell 3.98 points and 20X recall fell 0.85 points versus CONTROL.

ROOT_CAUSE_2: the 1% candidate still selected 2,096 false 2X positives and 248 terminal failures,
while missing 835 5X runners. Missed 5X tokens had a 5.604 lower transformed 120–180-second net
flow value and 3.033 fewer buyers after first sell than selected true winners.

ROOT_CAUSE_3: a single delayed-sell scalar is non-monotonic. False positives sold 3.98 seconds
later than selected true winners, while missed 5X runners sold 10.405 seconds later. Delayed
selling alone therefore cannot distinguish durable demand from low-activity stagnation.

ROOT_CAUSE_4: the historical candidate cannot see the live-only families most likely to separate
organic capital from coordinated activity: real reserve accounts, point-in-time funder/linkage
graphs, exact bundles, and provider latency. The legitimate correction is later prospective data,
not repeated tuning against this retired outer window.

SECTION 5 — CONTROL AUTOPSY

CONTROL retains materially better broad runner nomination: +3.98 points 2X, +0.68 points 5X,
+0.47 points 10X, and +0.85 points 20X recall over the realtime candidate. Its cost is higher
terminal failure (+2.54 points) and substantially worse median adverse excursion (-66.58%). The
new hybrid shows that using the realtime score mainly as a failure filter does not solve the
trade-off: drawdown improves, but all precision and recall metrics fall below CONTROL. The prior
V3 ablations still identify buyer/order flow and entry/actionability as useful families; naive
wallet skill, social presence, and some creator/regime/market-cap combinations did not add stable
edge. Stage B remains rejected because it removes useful candidates without fixed-frequency lift.

SECTION 6 — MISSED RUNNER AUTOPSY

At 1%, the automatic lab identified 835 missed 5X runners. Relative to selected true winners,
their transformed differences were -5.604 for 120–180-second net flow, -3.033 buyers after first
sell, -2.526 trades at 120–180 seconds, -2.398 new buyers at both 90–120 and 120–180 seconds, and
+10.405 seconds to first sell. This supports a sequence/participation deficit diagnosis, but the
window is retired and cannot approve a fingerprint.

SECTION 7 — FALSE POSITIVE AUTOPSY

The realtime candidate selected 2,361 tokens: 265 reached 2X, 2,096 did not, and 248 ended as
terminal failures. Versus selected 2X winners, false positives had -4.406 transformed
120–180-second net flow, -1.949 60–90-second net flow, -1.586 buyers after first sell, and +3.98
seconds to first sell. Lower failure is useful, but weak late participation still dominates the
false-positive cohort.

SECTION 8 — REAL RESERVE FINDINGS

Current and legacy Pump bonding-curve layouts are decoded with virtual and real token/quote
reserves kept separate; SOL aliases are emitted only for SOL/WSOL quote layouts. The current-code
live probe populated 18 real-SOL observations for 9 tokens from native trade events. The runtime
also performs initial `getAccountInfo` plus dynamic `accountSubscribe` reads for active curve
accounts. The historical corpus lacks account-state history, so historical reserve findings remain
`UNAVAILABLE`, not market-cap proxies. Sustained account-subscription coverage is still required.

SECTION 9 — WALLET FINDINGS

The live probe preserved identities for 13 buys and 5 sells, and the historical replay contains
460,933 tokens with buyer arrival. Runtime profiles are stage-, objective-, regime-, and
copy-delay-conditioned and reject one-hit samples with Wilson bounds. However, the replay corpus
lacks point-in-time linkage/funder data and Helius is not configured; no Wallet Copyability V2
profile is approved. Wallet strategy/copyability therefore remains a real implementation with
insufficient real validation, not an intelligence PASS.

SECTION 10 — FUNDER/BUNDLE/WASH FINDINGS

Funder edges and linked-wallet components are persisted when evidence exists. Jito tip-account
transfers are labelled probabilistic evidence only; they never become an exact bundle ID or a rug
verdict. An earlier bounded capture observed one tip event; the current-code capture observed
none. The historical corpus has no funder graph or exact bundle identity. Adjusted buyer/volume
values remain `UNKNOWN` when linkage is unavailable, so funder, independent consensus, bundle,
and wash intelligence all remain FAIL for real validation.

SECTION 11 — MIGRATION FINDINGS

Migration started/completed/pool events, pre/post flow survival, buyer retention, sell shock, and
liquidity continuity are implemented and restart-tested. No migration occurred in the current
live probe, and PumpPortal redundancy is not configured. Real migration continuity coverage is
therefore insufficient and remains FAIL.

SECTION 12 — SOCIAL/NARRATIVE FINDINGS

The canonical bus and PIT stores accept social and narrative observations and preserve causal
ordering (lead, follow, confirm), narrative velocity, saturation, decay, and revival. No legitimate
dynamic provider was configured and the transaction corpus contains neither family. Dynamic
social is BLOCKED and dynamic narrative is FAIL; no zero values or fake observations are emitted.

SECTION 13 — SELF-LEARNING FINDINGS

The lab evaluates CONTROL, V3/realtime candidates, and hybrids at nine fixed frequencies from
0.01% through 5%, generates autopsies, records hypotheses, detects sustained drift, and enforces
human-only promotion. This run generated 20 research hypotheses, approved zero features, and set
`public_route=false`. The strongest effects remain retired-window hypotheses only. Both new
challenger decisions are explicit rejections.

SECTION 14 — REALTIME LATENCY

For 54 non-health live events, source-to-receipt provider latency was p50 4,413 ms, p90 5,737 ms,
p95 5,824 ms, and p99 5,824 ms. Source-to-feature latency was p50 4,418 ms, p90 5,741 ms, p95
5,827 ms, and p99 5,833 ms. Availability latency was p50 0 ms and p95 1.63 ms. The probe bypassed
the full model and Discord paths intentionally, so model/decision/Discord latency is unavailable;
the integrated service persists all checkpoints and its E2E test exercises them. The public RPC
ended disconnected after four recorded 429s and four reconnects, so these figures are evidence of
functionality, not a production latency SLA.

SECTION 15 — E2E / CHAOS

Covered paths include native-plus-PumpPortal dedupe, semantic conflicts, malformed/wrong-program
Anchor logs, failed transactions, non-mapping parsed instructions found live, out-of-order curves,
Helius-driven candidate creation, BNB websocket conversion, provider stale health, processing
lease recovery, migration continuity, dynamic market batching, HOT-candidate restart, Discord
restart behavior, and a 10,050-event duplicate storm. The soak finished with equal total/distinct
canonical keys, `quick_check=ok`, and no public signal. Existing repository tests also cover queue
backpressure, multi-guild delivery identity, provider degradation, and restart-safe alerts.

Not every requested chaos injection has real E2E evidence: database-full/locked recovery, every
provider simultaneously unavailable, slow-model saturation, process kill during real sockets,
and sustained Discord delivery are not proven here. BRUTAL E2E and CHAOS therefore remain FAIL.

SECTION 16 — TEST / CI

Local final evidence before push:

- full pytest: 269 passed plus 5 subtests, one third-party `audioop` deprecation warning;
- focused migration/restart/Discord/load/realtime regression: 90 passed;
- Ruff: all checks passed;
- compileall: passed;
- package: wheel and source distribution built successfully;
- current-code live probe DB: `quick_check=ok`, zero FK violations, 66 total/66 distinct keys.

Docker is not installed in this Windows workspace. Docker build, isolated staging Compose, staging
health, and staging acceptance must be proven by the existing push-triggered GitHub workflow. CI
status is pending until the implementation commit is pushed; production approval remains manual.

SECTION 17 — BLOCKERS

1. The public Solana RPC repeatedly returns HTTP 429; a production-grade RPC is required for
   sustained native transaction enrichment and gap recovery.
2. No live create/creator or migration occurred during the bounded current-code probe.
3. PumpPortal and Helius credentials/accounts are not configured; BNB factory identities were not
   supplied for live coverage.
4. There is no legitimate dynamic social/narrative provider or historical social corpus.
5. Historical native reserve accounts, PIT funder/linkage graphs, exact bundle identity, provider
   latency, and post-observation copy delays are unavailable in the real corpus.
6. The new candidate has negative fixed-frequency lift; its outer window is retired, not sealed.
7. No sustained prospective shadow period, live Discord path, or matured live outcome cohort exists.
8. Full chaos coverage is incomplete. Production deployment is prohibited.

Evidence-backed self-grades:

| Dimension | Score | Evidence |
|---|---:|---|
| IMPLEMENTATION_DEPTH | 88/100 | 18 event types, direct/redundant/selective sources, reserve/timeline/actor/lab paths; several real providers unfilled |
| REAL_DATA_COVERAGE | 58/100 | 20.4M historical events and a real live probe; no reserve history, linkage, migration, social, or sustained capture |
| INTEGRATION_HARMONY | 82/100 | source→bus→DB→feature→shadow/Discord fields connected; no full live source→matured-outcome proof |
| TEST_DEPTH | 88/100 | full suite, real replay, parser regressions, restart, 10K soak; requested chaos matrix incomplete |
| RUNTIME_RESILIENCE | 74/100 | leases, watchdogs, reconnect/backfill, health and 429 tracking; public native feed still unstable |
| INTELLIGENCE_LIFT | 12/100 | failure/drawdown improved, but all primary fixed-frequency edge targets deteriorated |
| PRODUCTION_EVIDENCE | 28/100 | bounded no-trading live population only; no sustained VPS/Discord/outcome acceptance |

SECTION 18 — FINAL TRUTH

| Requirement | Truth |
|---|---|
| REALTIME EVENT FABRIC | PASS |
| NATIVE PUMPFUN | FAIL |
| PUMPPORTAL REDUNDANCY | FAIL |
| HELIUS CURATED FEED | NOT_CONFIGURED |
| REAL SOL RESERVES | PASS |
| REAL RESERVE TRAJECTORY | PASS |
| HIGH-RES EARLY TIMELINE | PASS |
| BUYER ARRIVAL INTELLIGENCE | PASS |
| FIRST-SELL INTELLIGENCE | PASS |
| HOT/WARM/COLD | PASS |
| CREATOR DISCOVERY | FAIL |
| WALLET DISCOVERY | PASS |
| WALLET STRATEGY MODEL | FAIL |
| COPYABILITY V2 | FAIL |
| INDEPENDENT CONSENSUS | FAIL |
| FUNDER GRAPH | FAIL |
| BUNDLE INTELLIGENCE | FAIL |
| WASH ADJUSTMENT | FAIL |
| MIGRATION CONTINUITY | FAIL |
| DYNAMIC SOCIAL | BLOCKED |
| DYNAMIC NARRATIVE | FAIL |
| CONTROL AUTOPSY | PASS |
| CONTROL x V3 HYBRID | REJECTED |
| MISSED-RUNNER LAB | PASS |
| FALSE-POSITIVE LAB | PASS |
| HYPOTHESIS REGISTRY | PASS |
| CHAMPION/CHALLENGER | PASS |
| CONCEPT DRIFT | FAIL |
| LOW-PERFORMANCE SELF-REFLECTION | PASS |
| FULL SOURCE→OUTCOME HARMONY | FAIL |
| BRUTAL E2E | FAIL |
| CHAOS | FAIL |
| RESTART RECOVERY | PASS |
| 10K SOAK | PASS |
| CI | PENDING PUSH |
| 2X CORE >=60% | FAIL |
| PREMIUM >=70% | FAIL |
| STRONG >=55% | FAIL |
| 5X TARGET | FAIL |
| 10X TARGET | FAIL |
| 20X TARGET | FAIL |
| 50X TARGET | FAIL |
| PRODUCTION READY | NO |

Final completion state: **PARTIAL — BLOCKERS REMAIN**. No production deployment or automatic
feature/model promotion was performed.

# Gambit Jr V1.5 Intelligence V3 measured execution report

## Model results

| Model | Horizon | Frequency | Signals | 2X precision | 5X precision | 10X precision | 20X recall | 50X recall | Terminal failure | MAE |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Random | 48h | 1.00% | 3,182 | 1.73% | 0.28% | 0.09% | 0.71% | 0.75% | 0.85% | 0.00% |
| Market-cap/stage | 48h | 1.00% | 3,182 | 5.22% | 1.57% | 0.82% | 6.03% | 5.26% | 1.73% | 0.00% |
| Buyer/order flow | 48h | 1.00% | 3,182 | 5.31% | 1.82% | 0.91% | 4.61% | 6.77% | 3.27% | -0.23% |
| Market-cap + buyer flow | 48h | 1.00% | 3,182 | 3.55% | 1.48% | 0.75% | 5.67% | 6.02% | 1.79% | 0.00% |
| CONTROL reconstruction | 48h | 1.00% | 3,182 | 18.67% | 3.61% | 1.73% | 6.38% | 3.01% | 11.22% | -33.63% |
| Exact CONTROL, if available | NOT_AVAILABLE | NOT_AVAILABLE | NOT_AVAILABLE | NOT_AVAILABLE | NOT_AVAILABLE | NOT_AVAILABLE | NOT_AVAILABLE | NOT_AVAILABLE | NOT_AVAILABLE | NOT_AVAILABLE |
| V2 combined policy | 48h | 1.00% | 1,774 | 3.66% | 1.07% | 0.56% | 1.67% | 0.90% | 0.79% | 0.00% |
| V3 target-specific gradient (2X specialist rank) | 48h | 1.00% | 3,182 | 13.92% | 3.90% | 1.48% | 8.87% | 9.77% | 3.58% | 0.00% |
| V3 two-stage | 48h | 1.00% | 3,182 | 3.36% | 1.13% | 0.63% | 3.90% | 4.51% | 6.03% | 0.00% |
| V3 Premium | 48h | inner-calibrated threshold | 2 | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% | -0.40% |
| V3 Strong | 48h | inner-calibrated threshold | 184 | 7.61% | 1.09% | 0.54% | 0.00% | 0.00% | 13.59% | 0.00% |

All rows except V2 use the same 318,214-token T+60 outer universe from 2026-06-28 through 2026-07-14. V2 is the frozen policy on its retired 177,352-token T+60 diagnostic window, 2026-07-05 through 2026-07-13; it is shown as a frozen comparator, not falsely presented as an identical-window rerun. MAE is median maximum adverse excursion and has 93.62% to 100% coverage depending on selection. Median time-to-target, liquidity-collapse rate, and general-candidate copyability are `NOT_MEASURED`; the source does not contain correctly ordered fields for them.

Verdict: the available-data V3 gradient candidate has real ranking lift over random and simple baselines, but it does not beat the reconstructed CONTROL at 1%, does not meet the 60% precision requirement, and is not production-ready. The two-stage gate fails badly out of sample. No threshold was weakened.

## Data, splits, targets, and prevalence

- Primary source: Pumpfun Memecoin Corpus, detected 2026-06-05 through 2026-07-14, 798,430 token rows, 33,581,765 raw trades, 26,934,864 pre-graduation snapshots, and 5,669 post-graduation outcomes.
- Corrected available-data universe: 742,793 unique mints and 4,448,550 evaluated native landmark observations at 30s, 60s, 3m, 5m, 10m, and 30m.
- Inner train: `[2026-06-05, 2026-06-21)`, 943,831 observations after future-creator purge.
- Inner calibration: `[2026-06-21, 2026-06-28)`, 350,448 observations after outer-creator purge; the fixed T+60 threshold subset is 58,443 rows.
- Outer 1: `[2026-06-28, 2026-07-05)`, 128,304 T+60 rows.
- Outer 2: `[2026-07-05, 2026-07-12)`, 162,397 T+60 rows.
- Outer 3 partial: `[2026-07-12, 2026-07-15)`, 27,513 T+60 rows.
- Combined outer target counts: 5,012 2X, 2,721 3X, 1,384 5X, 612 10X, 282 20X, and 133 50X.
- Combined outer natural prevalence: 1.5750% 2X, 0.8551% 3X, 0.4349% 5X, 0.1923% 10X, 0.0886% 20X, and 0.0418% 50X.
- Sampling and weighting: none. Fit, calibration, and test retain natural prevalence.
- Outcome horizon: 48 hours. Seven-day target-bearing outcomes are `NOT_AVAILABLE`.
- Sealed dates: `NOT_AVAILABLE`. All June/July labels had already been inspected. The later Trenches corpus covers 2026-08-06 through 2026-08-13 and contains 730,850 PIT observations over 85,442 mints, but no 2X/5X/10X/20X labels. It cannot be called a target-bearing sealed test.

The later-data acquisition blocker is therefore specific: a later PIT feature corpus was acquired, but it lacks mature price-multiple paths. It was not relabeled or called sealed. Sources: [Pumpfun Memecoin Corpus](https://huggingface.co/datasets/Slinky21/Pumpfun_Memecoin_Corpus) and [Trenches forward corpus](https://huggingface.co/datasets/Tr4m0ryp/trenches-pumpfun-forward-2026-08).

## Mathematical and implementation corrections

- 2X, 5X, 10X, and 20X are independent target milestones; they no longer compete with each other. Stop risks are terminal failure, liquidity collapse, unsellable state, severe actionable drawdown, and censoring.
- The fitted available-data implementation uses target-specific discrete-time landmark models at six native windows. It applies cross-target monotonic correction so `P(20X) <= P(10X) <= P(5X) <= P(2X)`.
- `RIGHT_TAIL_10X_PLUS` is exactly the 10X probability. `EXTREME_RIGHT_TAIL_20X_PLUS` is separate; they are never summed.
- Nomination can originate from Quick 2X, Mid 5X, 10X+, 20X+, revival, or survival. A missing Quick 2X value no longer blocks another nominator.
- Coverage and data quality are separate from model disagreement, calibration uncertainty, regime distance, OOD score, and bounded predictive uncertainty.
- Actionable path assessment records raw/actionable peak, all nested milestone times, MAE, terminal event, and censor reason. A target reached before a later collapse remains reached.
- The two-stage gate consumes temporal out-of-fold Stage-A predictions plus the independently fitted terminal-failure probability and actionability/coverage/regime fields. It does not use an in-sample percentile blend.
- CONTROL and V2 remain comparison outputs; neither vetoes the V3 research envelope. The live CONTROL path was not modified.

## Feature audit and liquidity/order flow

The fit uses 28 columns across piecewise time, market/stage, cleaned buyer/order flow, momentum, creator history, concentration, entry/actionability proxy, and regime. Every field has row/token coverage, dates, PIT validity, unit, source, live reproducibility, licence, and informative-missingness metadata in the machine result.

Rejected/corrupt rows include 6,690,901 system-program wallet trades, 1,136,321 raw trades whose `SOL / (token * price)` ratio falls outside 0.01–100, suspect top-10 concentration, and corrected top-10 values above 100%. These rows are not silently repaired.

Exact real SOL reserve is `NOT_AVAILABLE`: the trade records do not contain an authoritative reserve-state series, and the virtual curve/market-cap fields are not interchangeable with real reserves, USD liquidity, or price. The truthful substitutes are cleaned valid-trade counts, buy/sell counts, independent buyers, buy pressure, net SOL flow, and trade velocity. Buyer/order flow alone produces 5.31% 2X precision at 1% versus 1.73% random. Exact liquidity collapse is `NOT_AVAILABLE` pre-graduation.

Same-family logistic ablation deltas are reduced-model PR-AUC minus full-model PR-AUC:

| Family removed | Delta | Decision |
|---|---:|---|
| Buyer/order flow | -0.00669 | RESEARCH_ONLY; positive incremental contribution, not sealed |
| Entry/actionability | -0.00821 | RESEARCH_ONLY; positive incremental contribution, not sealed |
| Concentration | -0.00306 | RESEARCH_ONLY; positive incremental contribution, not sealed |
| Momentum | -0.00112 | RESEARCH_ONLY; small positive contribution, not sealed |
| Piecewise time | -0.00006 | RESEARCH_ONLY; mathematically required, incremental result negligible |
| Creator | +0.00071 | REJECT in next inner-only redesign |
| Regime | +0.00109 | REJECT in next inner-only redesign |
| Market cap/stage | +0.02694 | REJECT from this logistic combination; retain only as frozen comparator until new data |
| Real reserve/liquidity velocity | NOT_AVAILABLE | UNAVAILABLE |
| Wallet consensus/funder/bundle/wash/narrative | NOT_AVAILABLE | UNAVAILABLE |

Approved V3 features remain zero because a `KEEP` decision requires later sealed stability, live reproducibility, and licensing compatibility together. Positive retrospective ablations are `RESEARCH_ONLY`, not auto-approved.

## Target specialists, regimes, and precision gate

At 1% frequency the gradient specialists are:

| Ranker | 2X precision | 5X precision | 10X precision | 20X recall | 50X recall | Terminal failure |
|---|---:|---:|---:|---:|---:|---:|
| Quick 2X | 13.92% | 3.90% | 1.48% | 8.87% | 9.77% | 3.58% |
| Mid 5X | 14.61% | 5.63% | 2.51% | 14.89% | 15.79% | 10.50% |
| 10X+ | 16.25% | 4.62% | 2.01% | 8.87% | 9.77% | 7.54% |
| 20X+ | 17.54% | 3.17% | 1.54% | 6.03% | 3.76% | 7.51% |

At a broad 5% frontier, the Mid 5X ranker reaches 48.94% 20X recall and 51.88% 50X recall, but only 4.00% 5X precision, 1.82% 10X precision, and 7.69% terminal failure. That is not a core-policy pass.

The Quick 2X gradient candidate's 1% 2X precision is 12.16% in Outer 1, 15.27% in Outer 2, and 12.36% in partial Outer 3. Terminal failure rises from 0.47% to 3.88% to 5.45%. This drift is one reason not to promote it.

The inner calibration gate produced 65.33% Premium precision on exactly 75 calibration signals and 47.54% Strong precision on 509 signals; both miss their fixed targets before outer evaluation. On outer data the same thresholds emit only 2 Premium signals with 0 successes and 184 Strong signals at 7.61%. Combined Premium + Strong is far below 60%; `FAIL TARGET`.

## Calibration, uncertainty, drawdown, and latency

Gradient target calibration on 318,214 outer rows:

| Target | PR-AUC | ROC-AUC | Brier | Bootstrap Brier 95% | ECE | Slope | Intercept |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2X | 0.09497 | 0.89366 | 0.015264 | [0.014919, 0.015687] | 0.011776 | 0.7317 | 0.2630 |
| 5X | 0.03632 | 0.88721 | 0.004318 | [0.004100, 0.004539] | 0.003833 | 0.6982 | 0.3573 |
| 10X | 0.01328 | 0.88143 | 0.001920 | [0.001807, 0.002037] | 0.001771 | 0.9106 | 1.8681 |
| 20X | 0.00302 | 0.79234 | 0.000886 | [0.000810, 0.000990] | 0.000838 | 0.6772 | -0.0622 |

Every row has reliability-bin support; the full probability support is 318,214 per target. Rare-event Brier scores are not treated as success. The 1% V3 Quick 2X precision Wilson interval is 12.76%–15.17%.

Median model disagreement is 0.00018 (p90 0.00244), measured OOD rate is 7.38%, median OOD score is 0.00070 (p90 0.4412), median regime distance is 0.625, and median bounded predictive uncertainty is 0.1294 (p90 0.2253). This is a transparent diagnostic, not Bayesian posterior uncertainty.

The versioned primary stop policy is terminal-failure-only. On the two-stage 1% set, conservative 2X precision is 1.55% at -30%, 1.61% at -50%, 1.96% at -70%, and 3.36% terminal-only. MAE timing relative to target is unavailable, so the price-stop sensitivities are conservative and no favorable ordering is invented.

Local retrospective batch inference for all outer target specialists took 10.1524 seconds, or 0.03190 ms per candidate. Provider, Discord, and user latency are `NOT_MEASURED` for the general corpus.

## Wallet and copyability evidence

The internal study uses 72,688 valid pre-graduation delayed entries across a deterministic subset of 387 wallets. It excludes the system program, token creators, unit-corrupt trades, wallets with fewer than 20 or more than 500 launches, and histories with fewer than five matured training entries. Insider, arbitrage, market-maker, funder, bundle, and complete linkage labels are unavailable.

| Follower delay | Test entries | Selected entries | Overall 2X | Skilled-wallet copy 2X | 5X | 10X+ | 2X Wilson 95% | Median MAE |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 15s | 10,282 | 724 | 18.24% | 15.61% | 1.10% | 0.00% | 13.15%–18.43% | -43.87% |
| 30s | 10,059 | 798 | 16.95% | 16.29% | 1.00% | 0.00% | 13.89%–19.01% | -43.81% |
| 60s | 9,646 | 622 | 14.57% | 13.99% | 1.13% | 0.00% | 11.48%–16.93% | -40.03% |
| 120s | 8,908 | 688 | 12.54% | 11.48% | 0.44% | 0.00% | 9.31%–14.08% | -30.67% |

Prior wallet “skill” underperforms the same-delay population at every tested latency. Wallet skill and copyability are `REJECT`. Independent-wallet consensus remains `UNAVAILABLE`; no complete cluster/funder/copy graph exists, so correlated wallets are not counted as independent evidence.

## Social infrastructure evidence

RED-PUMP contributes 832,941 natural-prevalence rows from 2026-05-08 through 2026-06-10. Train is 2026-05-08–24, calibration 2026-05-25–31, and test 2026-06-01–10. Overall prevalence is 0.1982%; raw Telegram-present tokens run at 1.4850% versus 0.1661% without Telegram.

Controlled test PR-AUC is 0.02696 for the base model and 0.02580 for base + Telegram, a -0.00116 decrement. Base + X is 0.03988, base + website 0.03272, and base + multi-platform 0.03627, but creator identity is absent and the collector only exposes a newest-50 window of roughly six minutes. Telegram presence is `REJECT`; X/website/multi-platform remain `RESEARCH_ONLY_COLLECTOR_BIASED`. Presence is infrastructure, not measured attention velocity, sentiment, or causality.

## Failure and shadow replay

The separate terminal-failure gradient model has outer PR-AUC 0.01823, ROC-AUC 0.59756, Brier 0.008305, ECE 0.007807, slope 0.6164, and intercept -0.0782. Its weak ranking/calibration makes it research-only. The Quick 2X candidate's selected terminal-failure rate is 3.58%, below the 5% ambition at 1%, but the model does not satisfy the other core targets.

The complete outer replay persisted 318,214 distinct real candidates to an external, ignored research SQLite database. Every row stores available features, 2X/5X/10X/20X probabilities, terminal-failure probability, actionability proxy, uncertainty components, nominator, tier, gate, latency, model version, and feature version. Nomination counts are 14 Quick 2X, 1,208 Mid 5X, 1,582 10X+, 2,913 20X+, and 312,497 not nominated. All 318,214 rows are gated `ABSTAIN_UNSEALED_VALIDATION`; public-route rows are exactly zero. The database SHA-256 is `da0bc9f8a52d1b15520ffdb673c0fe1a28e262b715b7a5f3f6c12e0e4856e2fc` and is deliberately not committed.

The operator/test-guild preview displays separate 2X, 5X, 10X+, 20X+, terminal/liquidity failure, entry actionability, copyability, coverage, uncertainty, nominator, gate reason, why-now evidence, and risks. Its footer states `V3 RESEARCH ONLY • NO PUBLIC ROUTE • NO EXECUTION`. No public signal, Discord event, outbox row, live veto, or threshold changed.

## Defects found and fixed

1. Nested targets were modeled as mutually exclusive competing outcomes. Fixed with target-specific risk sets and nested path accounting.
2. 10X and 20X overlapping probabilities were summed. Fixed with separate 10X+ and 20X+ fields.
3. Candidate generation depended on Quick 2X. Fixed with multi-objective nominators and stored primary/secondary reasons.
4. Uncertainty collapsed into missing coverage. Fixed with separate components and a disclosed bounded formula.
5. Path evaluation returned at the first milestone and lost later nested targets. Fixed to scan the complete ordered path and preserve targets reached before later failure.
6. A single linear interval represented time. Fixed with six native piecewise landmark indicators.
7. The first execution draft evaluated only T+60. Fixed by fitting/calibrating on all native landmarks while keeping all published comparisons at T+60.
8. The first report loop redundantly recomputed outer predictions five times. Fixed with one-pass outer inference and deterministic window slicing.
9. Raw flow included system-program and unit-inconsistent trades. Fixed with explicit exclusions and coverage reporting.
10. Stage B initially lacked a separately fitted hard-failure probability. Fixed and cross-fitted; its poor measured result is retained.

## Validation state and blockers

The nested-milestone, monotonicity, right-tail, nomination, uncertainty separation, future-evidence rejection, actionability, collapse-after-target, censoring, group purge, natural prevalence, calibration helpers, wallet copyability, shadow isolation, and operator-preview tests are included in the automated suite.

- Full pytest: 241 passed, 5 subtests passed, 1 `audioop` deprecation warning, 12.02s.
- Focused V3 suite: 31 passed, 1 `audioop` deprecation warning, 1.26s.
- Ruff: all checks passed.
- Package: `solana_memecoin_intelligence-1.5.0.tar.gz` and `solana_memecoin_intelligence-1.5.0-py3-none-any.whl` built successfully.
- Isolated production-acceptance contract: PASS; 10/10 operational migrations, quick check `ok`, WAL, reconciliation difference 0, 24 commands, 14 card builders, 6 views, and one automatic-alert payload.
- Load/restart soak: PASS; 10,000 events plus 3x burst, 10,256 persisted, 10,000 duplicate replays suppressed, zero duplicate keys, zero foreign-key violations, reconciliation difference 0, and 1,452.70 persisted events/second.
- Real runner-autopsy reuse: PASS; 763,697 source rows, 742,917 valid analysis rows, truth state `DIAGNOSTIC_ONLY_RETIRED_WINDOWS_NOT_SEALED`.
- CI Docker build, staging contract, staging health, and staging acceptance are verified on the containing push. Production approval remains manual and is not invoked.

Remaining blockers are: no target-bearing sealed later period; no seven-day labels; no exact CONTROL vectors; no authoritative real-reserve/liquidity-collapse path; no complete funder/wallet-cluster/bundle linkage; no general-candidate actionable price path after all latency/cost components; collector-biased social data; failed Premium/Strong thresholds; and no validated V3 feature approvals.

## Final truth

| Claim | Result |
|---|---|
| NESTED-MILESTONE MATH CORRECTED | PASS |
| RIGHT-TAIL DOUBLE COUNTING FIXED | PASS |
| MID/RIGHT-TAIL NOMINATION FIXED | PASS |
| UNCERTAINTY CORRECTED | PASS |
| AVAILABLE-DATA V3 FITTED | PASS |
| AVAILABLE-DATA V3 CALIBRATED | PASS |
| CONTROL COMPARISON COMPLETE | PASS — reconstruction; exact CONTROL explicitly unavailable |
| V2 COMPARISON COMPLETE | PASS — frozen retired-window comparator |
| 48-HOUR RESULTS COMPLETE | PASS |
| SEVEN-DAY RESULTS COMPLETE | FAIL |
| LIQUIDITY VELOCITY MEASURED | FAIL — cleaned net SOL/order flow measured; exact reserve/liquidity velocity unavailable |
| ORDER FLOW MEASURED | PASS |
| WALLET COPYABILITY MEASURED | PASS — result rejected |
| SOCIAL INCREMENTAL VALUE MEASURED | PASS — Telegram rejected; other fields research-only |
| PUBLIC CORE 2X >=60% | FAIL |
| PREMIUM >=70% | FAIL |
| STRONG >=55% | FAIL |
| CORE 5X >=20% | FAIL |
| CORE 10X >=8% | FAIL |
| 20X RECALL >=35% | FAIL for the 1% core policy; broad 5% research frontier only passes |
| 50X RECALL >=40% | FAIL for the 1% core policy; broad 5% research frontier only passes |
| SEALED VALIDATION | FAIL |
| SHADOW CANDIDATE READY | FAIL — replay complete, but no sealed validation and no release gate |
| PRODUCTION READY | NO |

No V4 was created. No production deployment was attempted.

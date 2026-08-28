# Gambit Jr V1.5 runner-intelligence failure autopsy

This is a diagnostic-only reconstruction. It does not modify `CONTROL_V15`, approve a feature, create a challenger, or authorize production deployment. June/July rows are retired diagnostics and are not sealed evidence.

## Evidence boundary and outcome-label correction

- T+3m source rows: **763,697**
- Quality-bounded analysis rows: **742,917**
- Exact historical production provider vectors: **unavailable**
- Outcome maturity: **48 hours**, not the required seven days
- Rejected prior fields: `edge_3m.peak_48h` and `tokens.peak_market_cap_sol` contained impossible unit/reserve outliers
- Replacement: dimensionless point-in-time market-cap and post-graduation price ratios

The previously reported 34.22% market-cap-only figure used the rejected derived peak field. It is not retained as valid autopsy evidence. The corrected chronological diagnostic result is reported below.

## 1–6. Runner attrition funnels at T+3m

Counts in the main columns are independent diagnostics; `strict final` applies discovery, coverage, score, OPEN entry, failure and public-tier gates in sequence.

| Cohort | Total | Early | Coverage | Score ≥60 | Score ≥75 | Entry OPEN | Failure <40 | Core tier | Strict final |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2x | 11,204 | 11,163 | 10,999 | 2,335 | 887 | 9,821 | 10,833 | 1,545 | 1,252 |
| 3x | 9,021 | 8,996 | 8,867 | 1,340 | 486 | 8,298 | 8,829 | 928 | 816 |
| 5x | 1,293 | 1,277 | 1,194 | 464 | 168 | 937 | 1,192 | 254 | 203 |
| 10x | 542 | 531 | 489 | 200 | 70 | 380 | 502 | 107 | 83 |
| 20x | 286 | 284 | 260 | 102 | 35 | 211 | 266 | 59 | 47 |
| 50x | 138 | 137 | 129 | 39 | 10 | 107 | 130 | 21 | 18 |

`reconstructed_signaled` means the observable-field reconstruction reached PREMIUM or STRONG. It is not a claim that a historical live Discord alert exists.

## 7–12. Exact miss attribution

| Cohort | Missed | Discovery | Intelligence/score | Entry | Failure gate | Coverage | State/provider |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2x | 9,659 | 0.40% | 89.07% | 7.71% | 0.00% | 1.86% | 0.95% |
| 3x | 8,093 | 0.30% | 92.61% | 4.65% | 0.00% | 1.69% | 0.75% |
| 5x | 1,039 | 1.54% | 71.13% | 16.46% | 0.00% | 8.37% | 2.50% |
| 10x | 435 | 2.53% | 68.97% | 16.32% | 0.00% | 10.57% | 1.61% |
| 20x | 227 | 0.88% | 72.25% | 15.42% | 0.00% | 10.57% | 0.88% |
| 50x | 117 | 0.85% | 76.07% | 16.24% | 0.00% | 6.84% | 0.00% |

The dominant measured loss is intelligence: RunnerScore remains below 60 despite the evidence being present. CHASING is the second-largest decisive loss. Failure penalties are not the decisive reason for any reconstructed 5x+ miss, although the failure result is only a lower bound because several live risk inputs are absent.

## Multi-timestamp runner replay

### 2x

| Time | Observed | Median score | Core tier | Coverage ≥75 | OPEN | EXTENDED | CHASING | UNKNOWN |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| T+30s | 11,204 | 51.88 | 650 | 11,178 | 10,041 | 526 | 592 | 45 |
| T+60s | 11,204 | 51.88 | 1,945 | 11,129 | 9,922 | 436 | 737 | 109 |
| T+180s | 11,204 | 51.88 | 1,545 | 10,999 | 9,821 | 339 | 745 | 299 |
| T+300s | 11,204 | 51.88 | 816 | 10,951 | 9,204 | 370 | 1,164 | 466 |
| T+600s | 11,204 | 51.88 | 995 | 10,947 | 9,017 | 365 | 989 | 833 |
| T+1800s | 11,204 | 51.88 | 759 | 10,996 | 8,952 | 355 | 651 | 1,246 |
| T+3600s | 11,204 | 51.88 | 464 | 11,015 | 8,771 | 474 | 592 | 1,367 |

### 3x

| Time | Observed | Median score | Core tier | Coverage ≥75 | OPEN | EXTENDED | CHASING | UNKNOWN |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| T+30s | 9,021 | 51.88 | 314 | 9,001 | 8,456 | 223 | 306 | 36 |
| T+60s | 9,021 | 51.88 | 1,278 | 8,954 | 8,364 | 185 | 380 | 92 |
| T+180s | 9,021 | 51.88 | 928 | 8,867 | 8,298 | 131 | 376 | 216 |
| T+300s | 9,021 | 51.88 | 401 | 8,821 | 7,862 | 126 | 683 | 350 |
| T+600s | 9,021 | 51.88 | 451 | 8,818 | 7,781 | 100 | 513 | 627 |
| T+1800s | 9,021 | 51.88 | 303 | 8,868 | 7,697 | 84 | 314 | 926 |
| T+3600s | 9,021 | 51.88 | 203 | 8,884 | 7,607 | 120 | 276 | 1,018 |

### 5x

| Time | Observed | Median score | Core tier | Coverage ≥75 | OPEN | EXTENDED | CHASING | UNKNOWN |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| T+30s | 1,293 | 54.57 | 177 | 1,283 | 1,020 | 110 | 144 | 19 |
| T+60s | 1,293 | 57.37 | 327 | 1,252 | 974 | 85 | 180 | 54 |
| T+180s | 1,293 | 55.05 | 254 | 1,194 | 937 | 60 | 171 | 125 |
| T+300s | 1,293 | 54.92 | 141 | 1,166 | 652 | 54 | 376 | 211 |
| T+600s | 1,293 | 54.44 | 142 | 1,165 | 585 | 45 | 274 | 389 |
| T+1800s | 1,293 | 51.88 | 104 | 1,197 | 523 | 42 | 146 | 582 |
| T+3600s | 1,293 | 51.07 | 72 | 1,212 | 486 | 53 | 119 | 635 |

### 10x

| Time | Observed | Median score | Core tier | Coverage ≥75 | OPEN | EXTENDED | CHASING | UNKNOWN |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| T+30s | 542 | 55.14 | 84 | 537 | 435 | 38 | 62 | 7 |
| T+60s | 542 | 57.51 | 134 | 519 | 399 | 33 | 82 | 28 |
| T+180s | 542 | 55.10 | 107 | 489 | 380 | 27 | 71 | 64 |
| T+300s | 542 | 55.39 | 59 | 469 | 246 | 20 | 164 | 112 |
| T+600s | 542 | 55.01 | 53 | 480 | 191 | 15 | 118 | 218 |
| T+1800s | 542 | 51.88 | 35 | 497 | 147 | 16 | 54 | 325 |
| T+3600s | 542 | 49.13 | 23 | 504 | 132 | 18 | 44 | 348 |

### 20x

| Time | Observed | Median score | Core tier | Coverage ≥75 | OPEN | EXTENDED | CHASING | UNKNOWN |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| T+30s | 286 | 55.32 | 42 | 282 | 243 | 11 | 27 | 5 |
| T+60s | 286 | 56.19 | 68 | 278 | 226 | 13 | 36 | 11 |
| T+180s | 286 | 55.05 | 59 | 260 | 211 | 12 | 35 | 28 |
| T+300s | 286 | 55.16 | 30 | 245 | 139 | 8 | 78 | 61 |
| T+600s | 286 | 55.09 | 30 | 249 | 103 | 4 | 58 | 121 |
| T+1800s | 286 | 51.61 | 14 | 259 | 73 | 6 | 26 | 181 |
| T+3600s | 286 | 49.27 | 13 | 265 | 63 | 6 | 20 | 197 |

### 50x

| Time | Observed | Median score | Core tier | Coverage ≥75 | OPEN | EXTENDED | CHASING | UNKNOWN |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| T+30s | 138 | 54.53 | 18 | 136 | 123 | 4 | 9 | 2 |
| T+60s | 138 | 54.29 | 26 | 136 | 123 | 3 | 10 | 2 |
| T+180s | 138 | 54.29 | 21 | 129 | 107 | 3 | 19 | 9 |
| T+300s | 138 | 55.08 | 14 | 122 | 74 | 1 | 39 | 24 |
| T+600s | 138 | 54.21 | 14 | 121 | 60 | 0 | 15 | 63 |
| T+1800s | 138 | 50.01 | 7 | 128 | 43 | 0 | 8 | 87 |
| T+3600s | 138 | 47.78 | 6 | 129 | 36 | 0 | 6 | 96 |

The earliest aggregate identification point is usually T+60s, not T+3m. Core-tier counts then decay, while UNKNOWN entry states rise sharply after migration because the source lacks a unit-consistent SOL/USD call-market-cap bridge.

## 13–17. Feature diagnostics

| Rank | Feature | Coverage | 2x effect | 5x effect | 10x effect | 20x effect | Stability/class |
|---:|---|---:|---:|---:|---:|---:|---|
| 1 | `concentration_score` | 89.94% | -1.342 | -0.501 | -0.419 | -0.306 | NEGATIVE |
| 2 | `buy_pressure` | 14.13% | 0.897 | 0.845 | 0.810 | 0.754 | INSUFFICIENT DATA |
| 3 | `log_buyer_count` | 100.00% | 0.853 | 1.202 | 1.199 | 1.153 | STRONG POSITIVE |
| 4 | `buyer_growth_score` | 100.00% | 0.527 | 1.379 | 1.627 | 1.860 | STRONG POSITIVE |
| 5 | `market_cap_growth` | 99.85% | 0.348 | 0.419 | 0.184 | 0.073 | WEAK POSITIVE |
| 6 | `log_market_cap` | 100.00% | 0.312 | -0.094 | -0.431 | -0.991 | UNSTABLE |
| 7 | `momentum_score` | 100.00% | 0.301 | -0.286 | -0.558 | -0.744 | UNSTABLE |
| 8 | `creator_score` | 100.00% | 0.272 | 0.389 | 0.348 | 0.299 | WEAK POSITIVE |
| 9 | `log_trade_count` | 100.00% | -0.219 | 1.078 | 1.357 | 1.385 | UNSTABLE |
| 10 | `curve_progress` | 100.00% | 0.214 | -0.197 | -0.484 | -0.752 | WEAK POSITIVE |
| 11 | `log_buy_volume` | 100.00% | 0.094 | 0.984 | 1.131 | 1.063 | UNSTABLE |

Buyer count/growth are the strongest stable positive right-tail descriptors. Volume and trade count have positive right-tail effects but reverse direction across weeks. Corrected concentration score is negatively associated with runners: the current intuition that lower concentration is always better is misleading in this corpus and should not be promoted without causal/safety review.

## 18. Discovery versus intelligence

For 5x misses, discovery accounts for 1.54%, intelligence/score for 71.13%, entry for 16.46%, coverage for 8.37%, and state/provider gaps for 2.50%. The intelligence share is 72.25% at 20x and 76.07% at 50x.

## 19–20. Replay cards

Each card contains what the reconstruction knew and decided at every available timestamp. Full machine-readable cards remain in the JSON evidence artifact.

### Missed 2x examples

- `2Tj5JcBzR7dPj7e5yCQB3hTn5Aw9j2Gpb98CPfdopump` — peak 58417.57x; miss `LOW_RUNNER_SCORE`. T+30s score=54.3 entry=OPEN tier=SILENT_WATCH; T+60s score=54.3 entry=OPEN tier=SILENT_WATCH; T+180s score=54.3 entry=OPEN tier=SILENT_WATCH; T+300s score=54.3 entry=OPEN tier=SILENT_WATCH; T+600s score=54.3 entry=OPEN tier=SILENT_WATCH; T+1800s score=54.3 entry=OPEN tier=SILENT_WATCH; T+3600s score=54.3 entry=OPEN tier=SILENT_WATCH
- `8Hiamh8BFvaJNvwd2h5FjS1AuQPtYssK9hXvhXh8pump` — peak 19373.32x; miss `ENTRY_CHASING`. T+30s score=54.3 entry=OPEN tier=SILENT_WATCH; T+60s score=54.3 entry=OPEN tier=SILENT_WATCH; T+180s score=54.3 entry=CHASING tier=SILENT_WATCH; T+300s score=54.3 entry=OPEN tier=SILENT_WATCH; T+600s score=51.6 entry=UNKNOWN tier=SILENT_WATCH; T+1800s score=46.3 entry=UNKNOWN tier=SILENT_WATCH; T+3600s score=47.8 entry=UNKNOWN tier=SILENT_WATCH
- `8GXBUgVfRzqy1iHjjGgDKYxUWECV62fRftXmPLXupump` — peak 13691.93x; miss `LOW_RUNNER_SCORE`. T+30s score=53.9 entry=OPEN tier=SILENT_WATCH; T+60s score=61.0 entry=OPEN tier=STRONG; T+180s score=55.5 entry=OPEN tier=SILENT_WATCH; T+300s score=55.5 entry=OPEN tier=SILENT_WATCH; T+600s score=55.5 entry=OPEN tier=SILENT_WATCH; T+1800s score=55.5 entry=OPEN tier=SILENT_WATCH; T+3600s score=55.5 entry=OPEN tier=SILENT_WATCH
- `MZFtU51fzpKZkRZFMEqR7H4ffub5XXQhu95H1Kspump` — peak 6440.22x; miss `LOW_RUNNER_SCORE`. T+30s score=54.4 entry=OPEN tier=SILENT_WATCH; T+60s score=54.4 entry=OPEN tier=SILENT_WATCH; T+180s score=54.4 entry=OPEN tier=SILENT_WATCH; T+300s score=54.4 entry=OPEN tier=SILENT_WATCH; T+600s score=54.4 entry=OPEN tier=SILENT_WATCH; T+1800s score=54.4 entry=OPEN tier=SILENT_WATCH; T+3600s score=54.4 entry=OPEN tier=SILENT_WATCH
- `AQT769WT6nB9i777V8h29CU3ryicbsvyUDP8tahqpump` — peak 4829.04x; miss `LOW_RUNNER_SCORE`. T+30s score=54.2 entry=OPEN tier=SILENT_WATCH; T+60s score=54.2 entry=OPEN tier=SILENT_WATCH; T+180s score=54.2 entry=OPEN tier=SILENT_WATCH; T+300s score=54.2 entry=OPEN tier=SILENT_WATCH; T+600s score=54.2 entry=OPEN tier=SILENT_WATCH; T+1800s score=54.2 entry=OPEN tier=SILENT_WATCH; T+3600s score=54.2 entry=OPEN tier=SILENT_WATCH
- `EyDJReVw6621WEuJPAeT7Yx2TRaZ7wgiGoQ1CPZDpump` — peak 4606.18x; miss `LOW_RUNNER_SCORE`. T+30s score=55.3 entry=OPEN tier=SILENT_WATCH; T+60s score=55.3 entry=OPEN tier=SILENT_WATCH; T+180s score=55.3 entry=OPEN tier=SILENT_WATCH; T+300s score=55.3 entry=OPEN tier=SILENT_WATCH; T+600s score=55.3 entry=OPEN tier=SILENT_WATCH; T+1800s score=51.2 entry=UNKNOWN tier=SILENT_WATCH; T+3600s score=45.3 entry=UNKNOWN tier=SILENT_WATCH
- `7yuv8Mif4osAi3qp5aAfpyiece37DFHbBG4h2oyzpump` — peak 3789.01x; miss `LOW_RUNNER_SCORE`. T+30s score=55.2 entry=OPEN tier=SILENT_WATCH; T+60s score=55.2 entry=OPEN tier=SILENT_WATCH; T+180s score=55.2 entry=OPEN tier=SILENT_WATCH; T+300s score=55.2 entry=OPEN tier=SILENT_WATCH; T+600s score=55.2 entry=OPEN tier=SILENT_WATCH; T+1800s score=55.2 entry=OPEN tier=SILENT_WATCH; T+3600s score=55.2 entry=OPEN tier=SILENT_WATCH
- `7nXxNWSevB7H4P6E6X6DjjfjYbbaYLQvBy2v7i6hpump` — peak 3777.91x; miss `ENTRY_CHASING`. T+30s score=52.1 entry=OPEN tier=SILENT_WATCH; T+60s score=52.7 entry=OPEN tier=SILENT_WATCH; T+180s score=60.3 entry=CHASING tier=SILENT_WATCH; T+300s score=52.7 entry=OPEN tier=SILENT_WATCH; T+600s score=57.6 entry=UNKNOWN tier=SILENT_WATCH; T+1800s score=36.1 entry=UNKNOWN tier=SILENT_WATCH; T+3600s score=43.3 entry=UNKNOWN tier=SILENT_WATCH
- `9vHAS6wbQcvFrdS4nczoS2dBy3qHfeB9r9BjLT5Wpump` — peak 2968.57x; miss `LOW_RUNNER_SCORE`. T+30s score=57.4 entry=OPEN tier=SILENT_WATCH; T+60s score=53.8 entry=OPEN tier=SILENT_WATCH; T+180s score=53.8 entry=OPEN tier=SILENT_WATCH; T+300s score=53.8 entry=CHASING tier=SILENT_WATCH; T+600s score=48.5 entry=UNKNOWN tier=SILENT_WATCH; T+1800s score=44.7 entry=UNKNOWN tier=SILENT_WATCH; T+3600s score=46.0 entry=UNKNOWN tier=SILENT_WATCH
- `ApxcepWaN7YgDExeBDy7m5zQ6E4DVh4jFQtfCHj4pump` — peak 1803.35x; miss `LOW_RUNNER_SCORE`. T+30s score=54.0 entry=OPEN tier=SILENT_WATCH; T+60s score=54.0 entry=OPEN tier=SILENT_WATCH; T+180s score=54.0 entry=OPEN tier=SILENT_WATCH; T+300s score=54.0 entry=OPEN tier=SILENT_WATCH; T+600s score=40.7 entry=UNKNOWN tier=SILENT_WATCH; T+1800s score=45.1 entry=UNKNOWN tier=SILENT_WATCH; T+3600s score=46.6 entry=UNKNOWN tier=SILENT_WATCH

### Missed 5x examples

- `2Tj5JcBzR7dPj7e5yCQB3hTn5Aw9j2Gpb98CPfdopump` — peak 58417.57x; miss `LOW_RUNNER_SCORE`. T+30s score=54.3 entry=OPEN tier=SILENT_WATCH; T+60s score=54.3 entry=OPEN tier=SILENT_WATCH; T+180s score=54.3 entry=OPEN tier=SILENT_WATCH; T+300s score=54.3 entry=OPEN tier=SILENT_WATCH; T+600s score=54.3 entry=OPEN tier=SILENT_WATCH; T+1800s score=54.3 entry=OPEN tier=SILENT_WATCH; T+3600s score=54.3 entry=OPEN tier=SILENT_WATCH
- `8Hiamh8BFvaJNvwd2h5FjS1AuQPtYssK9hXvhXh8pump` — peak 19373.32x; miss `ENTRY_CHASING`. T+30s score=54.3 entry=OPEN tier=SILENT_WATCH; T+60s score=54.3 entry=OPEN tier=SILENT_WATCH; T+180s score=54.3 entry=CHASING tier=SILENT_WATCH; T+300s score=54.3 entry=OPEN tier=SILENT_WATCH; T+600s score=51.6 entry=UNKNOWN tier=SILENT_WATCH; T+1800s score=46.3 entry=UNKNOWN tier=SILENT_WATCH; T+3600s score=47.8 entry=UNKNOWN tier=SILENT_WATCH
- `8GXBUgVfRzqy1iHjjGgDKYxUWECV62fRftXmPLXupump` — peak 13691.93x; miss `LOW_RUNNER_SCORE`. T+30s score=53.9 entry=OPEN tier=SILENT_WATCH; T+60s score=61.0 entry=OPEN tier=STRONG; T+180s score=55.5 entry=OPEN tier=SILENT_WATCH; T+300s score=55.5 entry=OPEN tier=SILENT_WATCH; T+600s score=55.5 entry=OPEN tier=SILENT_WATCH; T+1800s score=55.5 entry=OPEN tier=SILENT_WATCH; T+3600s score=55.5 entry=OPEN tier=SILENT_WATCH
- `MZFtU51fzpKZkRZFMEqR7H4ffub5XXQhu95H1Kspump` — peak 6440.22x; miss `LOW_RUNNER_SCORE`. T+30s score=54.4 entry=OPEN tier=SILENT_WATCH; T+60s score=54.4 entry=OPEN tier=SILENT_WATCH; T+180s score=54.4 entry=OPEN tier=SILENT_WATCH; T+300s score=54.4 entry=OPEN tier=SILENT_WATCH; T+600s score=54.4 entry=OPEN tier=SILENT_WATCH; T+1800s score=54.4 entry=OPEN tier=SILENT_WATCH; T+3600s score=54.4 entry=OPEN tier=SILENT_WATCH
- `AQT769WT6nB9i777V8h29CU3ryicbsvyUDP8tahqpump` — peak 4829.04x; miss `LOW_RUNNER_SCORE`. T+30s score=54.2 entry=OPEN tier=SILENT_WATCH; T+60s score=54.2 entry=OPEN tier=SILENT_WATCH; T+180s score=54.2 entry=OPEN tier=SILENT_WATCH; T+300s score=54.2 entry=OPEN tier=SILENT_WATCH; T+600s score=54.2 entry=OPEN tier=SILENT_WATCH; T+1800s score=54.2 entry=OPEN tier=SILENT_WATCH; T+3600s score=54.2 entry=OPEN tier=SILENT_WATCH
- `EyDJReVw6621WEuJPAeT7Yx2TRaZ7wgiGoQ1CPZDpump` — peak 4606.18x; miss `LOW_RUNNER_SCORE`. T+30s score=55.3 entry=OPEN tier=SILENT_WATCH; T+60s score=55.3 entry=OPEN tier=SILENT_WATCH; T+180s score=55.3 entry=OPEN tier=SILENT_WATCH; T+300s score=55.3 entry=OPEN tier=SILENT_WATCH; T+600s score=55.3 entry=OPEN tier=SILENT_WATCH; T+1800s score=51.2 entry=UNKNOWN tier=SILENT_WATCH; T+3600s score=45.3 entry=UNKNOWN tier=SILENT_WATCH
- `7yuv8Mif4osAi3qp5aAfpyiece37DFHbBG4h2oyzpump` — peak 3789.01x; miss `LOW_RUNNER_SCORE`. T+30s score=55.2 entry=OPEN tier=SILENT_WATCH; T+60s score=55.2 entry=OPEN tier=SILENT_WATCH; T+180s score=55.2 entry=OPEN tier=SILENT_WATCH; T+300s score=55.2 entry=OPEN tier=SILENT_WATCH; T+600s score=55.2 entry=OPEN tier=SILENT_WATCH; T+1800s score=55.2 entry=OPEN tier=SILENT_WATCH; T+3600s score=55.2 entry=OPEN tier=SILENT_WATCH
- `7nXxNWSevB7H4P6E6X6DjjfjYbbaYLQvBy2v7i6hpump` — peak 3777.91x; miss `ENTRY_CHASING`. T+30s score=52.1 entry=OPEN tier=SILENT_WATCH; T+60s score=52.7 entry=OPEN tier=SILENT_WATCH; T+180s score=60.3 entry=CHASING tier=SILENT_WATCH; T+300s score=52.7 entry=OPEN tier=SILENT_WATCH; T+600s score=57.6 entry=UNKNOWN tier=SILENT_WATCH; T+1800s score=36.1 entry=UNKNOWN tier=SILENT_WATCH; T+3600s score=43.3 entry=UNKNOWN tier=SILENT_WATCH
- `9vHAS6wbQcvFrdS4nczoS2dBy3qHfeB9r9BjLT5Wpump` — peak 2968.57x; miss `LOW_RUNNER_SCORE`. T+30s score=57.4 entry=OPEN tier=SILENT_WATCH; T+60s score=53.8 entry=OPEN tier=SILENT_WATCH; T+180s score=53.8 entry=OPEN tier=SILENT_WATCH; T+300s score=53.8 entry=CHASING tier=SILENT_WATCH; T+600s score=48.5 entry=UNKNOWN tier=SILENT_WATCH; T+1800s score=44.7 entry=UNKNOWN tier=SILENT_WATCH; T+3600s score=46.0 entry=UNKNOWN tier=SILENT_WATCH
- `ApxcepWaN7YgDExeBDy7m5zQ6E4DVh4jFQtfCHj4pump` — peak 1803.35x; miss `LOW_RUNNER_SCORE`. T+30s score=54.0 entry=OPEN tier=SILENT_WATCH; T+60s score=54.0 entry=OPEN tier=SILENT_WATCH; T+180s score=54.0 entry=OPEN tier=SILENT_WATCH; T+300s score=54.0 entry=OPEN tier=SILENT_WATCH; T+600s score=40.7 entry=UNKNOWN tier=SILENT_WATCH; T+1800s score=45.1 entry=UNKNOWN tier=SILENT_WATCH; T+3600s score=46.6 entry=UNKNOWN tier=SILENT_WATCH

### Missed 10x examples

- `2Tj5JcBzR7dPj7e5yCQB3hTn5Aw9j2Gpb98CPfdopump` — peak 58417.57x; miss `LOW_RUNNER_SCORE`. T+30s score=54.3 entry=OPEN tier=SILENT_WATCH; T+60s score=54.3 entry=OPEN tier=SILENT_WATCH; T+180s score=54.3 entry=OPEN tier=SILENT_WATCH; T+300s score=54.3 entry=OPEN tier=SILENT_WATCH; T+600s score=54.3 entry=OPEN tier=SILENT_WATCH; T+1800s score=54.3 entry=OPEN tier=SILENT_WATCH; T+3600s score=54.3 entry=OPEN tier=SILENT_WATCH
- `8Hiamh8BFvaJNvwd2h5FjS1AuQPtYssK9hXvhXh8pump` — peak 19373.32x; miss `ENTRY_CHASING`. T+30s score=54.3 entry=OPEN tier=SILENT_WATCH; T+60s score=54.3 entry=OPEN tier=SILENT_WATCH; T+180s score=54.3 entry=CHASING tier=SILENT_WATCH; T+300s score=54.3 entry=OPEN tier=SILENT_WATCH; T+600s score=51.6 entry=UNKNOWN tier=SILENT_WATCH; T+1800s score=46.3 entry=UNKNOWN tier=SILENT_WATCH; T+3600s score=47.8 entry=UNKNOWN tier=SILENT_WATCH
- `8GXBUgVfRzqy1iHjjGgDKYxUWECV62fRftXmPLXupump` — peak 13691.93x; miss `LOW_RUNNER_SCORE`. T+30s score=53.9 entry=OPEN tier=SILENT_WATCH; T+60s score=61.0 entry=OPEN tier=STRONG; T+180s score=55.5 entry=OPEN tier=SILENT_WATCH; T+300s score=55.5 entry=OPEN tier=SILENT_WATCH; T+600s score=55.5 entry=OPEN tier=SILENT_WATCH; T+1800s score=55.5 entry=OPEN tier=SILENT_WATCH; T+3600s score=55.5 entry=OPEN tier=SILENT_WATCH
- `MZFtU51fzpKZkRZFMEqR7H4ffub5XXQhu95H1Kspump` — peak 6440.22x; miss `LOW_RUNNER_SCORE`. T+30s score=54.4 entry=OPEN tier=SILENT_WATCH; T+60s score=54.4 entry=OPEN tier=SILENT_WATCH; T+180s score=54.4 entry=OPEN tier=SILENT_WATCH; T+300s score=54.4 entry=OPEN tier=SILENT_WATCH; T+600s score=54.4 entry=OPEN tier=SILENT_WATCH; T+1800s score=54.4 entry=OPEN tier=SILENT_WATCH; T+3600s score=54.4 entry=OPEN tier=SILENT_WATCH
- `AQT769WT6nB9i777V8h29CU3ryicbsvyUDP8tahqpump` — peak 4829.04x; miss `LOW_RUNNER_SCORE`. T+30s score=54.2 entry=OPEN tier=SILENT_WATCH; T+60s score=54.2 entry=OPEN tier=SILENT_WATCH; T+180s score=54.2 entry=OPEN tier=SILENT_WATCH; T+300s score=54.2 entry=OPEN tier=SILENT_WATCH; T+600s score=54.2 entry=OPEN tier=SILENT_WATCH; T+1800s score=54.2 entry=OPEN tier=SILENT_WATCH; T+3600s score=54.2 entry=OPEN tier=SILENT_WATCH
- `EyDJReVw6621WEuJPAeT7Yx2TRaZ7wgiGoQ1CPZDpump` — peak 4606.18x; miss `LOW_RUNNER_SCORE`. T+30s score=55.3 entry=OPEN tier=SILENT_WATCH; T+60s score=55.3 entry=OPEN tier=SILENT_WATCH; T+180s score=55.3 entry=OPEN tier=SILENT_WATCH; T+300s score=55.3 entry=OPEN tier=SILENT_WATCH; T+600s score=55.3 entry=OPEN tier=SILENT_WATCH; T+1800s score=51.2 entry=UNKNOWN tier=SILENT_WATCH; T+3600s score=45.3 entry=UNKNOWN tier=SILENT_WATCH
- `7yuv8Mif4osAi3qp5aAfpyiece37DFHbBG4h2oyzpump` — peak 3789.01x; miss `LOW_RUNNER_SCORE`. T+30s score=55.2 entry=OPEN tier=SILENT_WATCH; T+60s score=55.2 entry=OPEN tier=SILENT_WATCH; T+180s score=55.2 entry=OPEN tier=SILENT_WATCH; T+300s score=55.2 entry=OPEN tier=SILENT_WATCH; T+600s score=55.2 entry=OPEN tier=SILENT_WATCH; T+1800s score=55.2 entry=OPEN tier=SILENT_WATCH; T+3600s score=55.2 entry=OPEN tier=SILENT_WATCH
- `7nXxNWSevB7H4P6E6X6DjjfjYbbaYLQvBy2v7i6hpump` — peak 3777.91x; miss `ENTRY_CHASING`. T+30s score=52.1 entry=OPEN tier=SILENT_WATCH; T+60s score=52.7 entry=OPEN tier=SILENT_WATCH; T+180s score=60.3 entry=CHASING tier=SILENT_WATCH; T+300s score=52.7 entry=OPEN tier=SILENT_WATCH; T+600s score=57.6 entry=UNKNOWN tier=SILENT_WATCH; T+1800s score=36.1 entry=UNKNOWN tier=SILENT_WATCH; T+3600s score=43.3 entry=UNKNOWN tier=SILENT_WATCH
- `9vHAS6wbQcvFrdS4nczoS2dBy3qHfeB9r9BjLT5Wpump` — peak 2968.57x; miss `LOW_RUNNER_SCORE`. T+30s score=57.4 entry=OPEN tier=SILENT_WATCH; T+60s score=53.8 entry=OPEN tier=SILENT_WATCH; T+180s score=53.8 entry=OPEN tier=SILENT_WATCH; T+300s score=53.8 entry=CHASING tier=SILENT_WATCH; T+600s score=48.5 entry=UNKNOWN tier=SILENT_WATCH; T+1800s score=44.7 entry=UNKNOWN tier=SILENT_WATCH; T+3600s score=46.0 entry=UNKNOWN tier=SILENT_WATCH
- `ApxcepWaN7YgDExeBDy7m5zQ6E4DVh4jFQtfCHj4pump` — peak 1803.35x; miss `LOW_RUNNER_SCORE`. T+30s score=54.0 entry=OPEN tier=SILENT_WATCH; T+60s score=54.0 entry=OPEN tier=SILENT_WATCH; T+180s score=54.0 entry=OPEN tier=SILENT_WATCH; T+300s score=54.0 entry=OPEN tier=SILENT_WATCH; T+600s score=40.7 entry=UNKNOWN tier=SILENT_WATCH; T+1800s score=45.1 entry=UNKNOWN tier=SILENT_WATCH; T+3600s score=46.6 entry=UNKNOWN tier=SILENT_WATCH

### Missed 20x examples

- `2Tj5JcBzR7dPj7e5yCQB3hTn5Aw9j2Gpb98CPfdopump` — peak 58417.57x; miss `LOW_RUNNER_SCORE`. T+30s score=54.3 entry=OPEN tier=SILENT_WATCH; T+60s score=54.3 entry=OPEN tier=SILENT_WATCH; T+180s score=54.3 entry=OPEN tier=SILENT_WATCH; T+300s score=54.3 entry=OPEN tier=SILENT_WATCH; T+600s score=54.3 entry=OPEN tier=SILENT_WATCH; T+1800s score=54.3 entry=OPEN tier=SILENT_WATCH; T+3600s score=54.3 entry=OPEN tier=SILENT_WATCH
- `8Hiamh8BFvaJNvwd2h5FjS1AuQPtYssK9hXvhXh8pump` — peak 19373.32x; miss `ENTRY_CHASING`. T+30s score=54.3 entry=OPEN tier=SILENT_WATCH; T+60s score=54.3 entry=OPEN tier=SILENT_WATCH; T+180s score=54.3 entry=CHASING tier=SILENT_WATCH; T+300s score=54.3 entry=OPEN tier=SILENT_WATCH; T+600s score=51.6 entry=UNKNOWN tier=SILENT_WATCH; T+1800s score=46.3 entry=UNKNOWN tier=SILENT_WATCH; T+3600s score=47.8 entry=UNKNOWN tier=SILENT_WATCH
- `8GXBUgVfRzqy1iHjjGgDKYxUWECV62fRftXmPLXupump` — peak 13691.93x; miss `LOW_RUNNER_SCORE`. T+30s score=53.9 entry=OPEN tier=SILENT_WATCH; T+60s score=61.0 entry=OPEN tier=STRONG; T+180s score=55.5 entry=OPEN tier=SILENT_WATCH; T+300s score=55.5 entry=OPEN tier=SILENT_WATCH; T+600s score=55.5 entry=OPEN tier=SILENT_WATCH; T+1800s score=55.5 entry=OPEN tier=SILENT_WATCH; T+3600s score=55.5 entry=OPEN tier=SILENT_WATCH
- `MZFtU51fzpKZkRZFMEqR7H4ffub5XXQhu95H1Kspump` — peak 6440.22x; miss `LOW_RUNNER_SCORE`. T+30s score=54.4 entry=OPEN tier=SILENT_WATCH; T+60s score=54.4 entry=OPEN tier=SILENT_WATCH; T+180s score=54.4 entry=OPEN tier=SILENT_WATCH; T+300s score=54.4 entry=OPEN tier=SILENT_WATCH; T+600s score=54.4 entry=OPEN tier=SILENT_WATCH; T+1800s score=54.4 entry=OPEN tier=SILENT_WATCH; T+3600s score=54.4 entry=OPEN tier=SILENT_WATCH
- `AQT769WT6nB9i777V8h29CU3ryicbsvyUDP8tahqpump` — peak 4829.04x; miss `LOW_RUNNER_SCORE`. T+30s score=54.2 entry=OPEN tier=SILENT_WATCH; T+60s score=54.2 entry=OPEN tier=SILENT_WATCH; T+180s score=54.2 entry=OPEN tier=SILENT_WATCH; T+300s score=54.2 entry=OPEN tier=SILENT_WATCH; T+600s score=54.2 entry=OPEN tier=SILENT_WATCH; T+1800s score=54.2 entry=OPEN tier=SILENT_WATCH; T+3600s score=54.2 entry=OPEN tier=SILENT_WATCH
- `EyDJReVw6621WEuJPAeT7Yx2TRaZ7wgiGoQ1CPZDpump` — peak 4606.18x; miss `LOW_RUNNER_SCORE`. T+30s score=55.3 entry=OPEN tier=SILENT_WATCH; T+60s score=55.3 entry=OPEN tier=SILENT_WATCH; T+180s score=55.3 entry=OPEN tier=SILENT_WATCH; T+300s score=55.3 entry=OPEN tier=SILENT_WATCH; T+600s score=55.3 entry=OPEN tier=SILENT_WATCH; T+1800s score=51.2 entry=UNKNOWN tier=SILENT_WATCH; T+3600s score=45.3 entry=UNKNOWN tier=SILENT_WATCH
- `7yuv8Mif4osAi3qp5aAfpyiece37DFHbBG4h2oyzpump` — peak 3789.01x; miss `LOW_RUNNER_SCORE`. T+30s score=55.2 entry=OPEN tier=SILENT_WATCH; T+60s score=55.2 entry=OPEN tier=SILENT_WATCH; T+180s score=55.2 entry=OPEN tier=SILENT_WATCH; T+300s score=55.2 entry=OPEN tier=SILENT_WATCH; T+600s score=55.2 entry=OPEN tier=SILENT_WATCH; T+1800s score=55.2 entry=OPEN tier=SILENT_WATCH; T+3600s score=55.2 entry=OPEN tier=SILENT_WATCH
- `7nXxNWSevB7H4P6E6X6DjjfjYbbaYLQvBy2v7i6hpump` — peak 3777.91x; miss `ENTRY_CHASING`. T+30s score=52.1 entry=OPEN tier=SILENT_WATCH; T+60s score=52.7 entry=OPEN tier=SILENT_WATCH; T+180s score=60.3 entry=CHASING tier=SILENT_WATCH; T+300s score=52.7 entry=OPEN tier=SILENT_WATCH; T+600s score=57.6 entry=UNKNOWN tier=SILENT_WATCH; T+1800s score=36.1 entry=UNKNOWN tier=SILENT_WATCH; T+3600s score=43.3 entry=UNKNOWN tier=SILENT_WATCH
- `9vHAS6wbQcvFrdS4nczoS2dBy3qHfeB9r9BjLT5Wpump` — peak 2968.57x; miss `LOW_RUNNER_SCORE`. T+30s score=57.4 entry=OPEN tier=SILENT_WATCH; T+60s score=53.8 entry=OPEN tier=SILENT_WATCH; T+180s score=53.8 entry=OPEN tier=SILENT_WATCH; T+300s score=53.8 entry=CHASING tier=SILENT_WATCH; T+600s score=48.5 entry=UNKNOWN tier=SILENT_WATCH; T+1800s score=44.7 entry=UNKNOWN tier=SILENT_WATCH; T+3600s score=46.0 entry=UNKNOWN tier=SILENT_WATCH
- `ApxcepWaN7YgDExeBDy7m5zQ6E4DVh4jFQtfCHj4pump` — peak 1803.35x; miss `LOW_RUNNER_SCORE`. T+30s score=54.0 entry=OPEN tier=SILENT_WATCH; T+60s score=54.0 entry=OPEN tier=SILENT_WATCH; T+180s score=54.0 entry=OPEN tier=SILENT_WATCH; T+300s score=54.0 entry=OPEN tier=SILENT_WATCH; T+600s score=40.7 entry=UNKNOWN tier=SILENT_WATCH; T+1800s score=45.1 entry=UNKNOWN tier=SILENT_WATCH; T+3600s score=46.6 entry=UNKNOWN tier=SILENT_WATCH

### Missed 50x examples

- `2Tj5JcBzR7dPj7e5yCQB3hTn5Aw9j2Gpb98CPfdopump` — peak 58417.57x; miss `LOW_RUNNER_SCORE`. T+30s score=54.3 entry=OPEN tier=SILENT_WATCH; T+60s score=54.3 entry=OPEN tier=SILENT_WATCH; T+180s score=54.3 entry=OPEN tier=SILENT_WATCH; T+300s score=54.3 entry=OPEN tier=SILENT_WATCH; T+600s score=54.3 entry=OPEN tier=SILENT_WATCH; T+1800s score=54.3 entry=OPEN tier=SILENT_WATCH; T+3600s score=54.3 entry=OPEN tier=SILENT_WATCH
- `8Hiamh8BFvaJNvwd2h5FjS1AuQPtYssK9hXvhXh8pump` — peak 19373.32x; miss `ENTRY_CHASING`. T+30s score=54.3 entry=OPEN tier=SILENT_WATCH; T+60s score=54.3 entry=OPEN tier=SILENT_WATCH; T+180s score=54.3 entry=CHASING tier=SILENT_WATCH; T+300s score=54.3 entry=OPEN tier=SILENT_WATCH; T+600s score=51.6 entry=UNKNOWN tier=SILENT_WATCH; T+1800s score=46.3 entry=UNKNOWN tier=SILENT_WATCH; T+3600s score=47.8 entry=UNKNOWN tier=SILENT_WATCH
- `8GXBUgVfRzqy1iHjjGgDKYxUWECV62fRftXmPLXupump` — peak 13691.93x; miss `LOW_RUNNER_SCORE`. T+30s score=53.9 entry=OPEN tier=SILENT_WATCH; T+60s score=61.0 entry=OPEN tier=STRONG; T+180s score=55.5 entry=OPEN tier=SILENT_WATCH; T+300s score=55.5 entry=OPEN tier=SILENT_WATCH; T+600s score=55.5 entry=OPEN tier=SILENT_WATCH; T+1800s score=55.5 entry=OPEN tier=SILENT_WATCH; T+3600s score=55.5 entry=OPEN tier=SILENT_WATCH
- `MZFtU51fzpKZkRZFMEqR7H4ffub5XXQhu95H1Kspump` — peak 6440.22x; miss `LOW_RUNNER_SCORE`. T+30s score=54.4 entry=OPEN tier=SILENT_WATCH; T+60s score=54.4 entry=OPEN tier=SILENT_WATCH; T+180s score=54.4 entry=OPEN tier=SILENT_WATCH; T+300s score=54.4 entry=OPEN tier=SILENT_WATCH; T+600s score=54.4 entry=OPEN tier=SILENT_WATCH; T+1800s score=54.4 entry=OPEN tier=SILENT_WATCH; T+3600s score=54.4 entry=OPEN tier=SILENT_WATCH
- `AQT769WT6nB9i777V8h29CU3ryicbsvyUDP8tahqpump` — peak 4829.04x; miss `LOW_RUNNER_SCORE`. T+30s score=54.2 entry=OPEN tier=SILENT_WATCH; T+60s score=54.2 entry=OPEN tier=SILENT_WATCH; T+180s score=54.2 entry=OPEN tier=SILENT_WATCH; T+300s score=54.2 entry=OPEN tier=SILENT_WATCH; T+600s score=54.2 entry=OPEN tier=SILENT_WATCH; T+1800s score=54.2 entry=OPEN tier=SILENT_WATCH; T+3600s score=54.2 entry=OPEN tier=SILENT_WATCH
- `EyDJReVw6621WEuJPAeT7Yx2TRaZ7wgiGoQ1CPZDpump` — peak 4606.18x; miss `LOW_RUNNER_SCORE`. T+30s score=55.3 entry=OPEN tier=SILENT_WATCH; T+60s score=55.3 entry=OPEN tier=SILENT_WATCH; T+180s score=55.3 entry=OPEN tier=SILENT_WATCH; T+300s score=55.3 entry=OPEN tier=SILENT_WATCH; T+600s score=55.3 entry=OPEN tier=SILENT_WATCH; T+1800s score=51.2 entry=UNKNOWN tier=SILENT_WATCH; T+3600s score=45.3 entry=UNKNOWN tier=SILENT_WATCH
- `7yuv8Mif4osAi3qp5aAfpyiece37DFHbBG4h2oyzpump` — peak 3789.01x; miss `LOW_RUNNER_SCORE`. T+30s score=55.2 entry=OPEN tier=SILENT_WATCH; T+60s score=55.2 entry=OPEN tier=SILENT_WATCH; T+180s score=55.2 entry=OPEN tier=SILENT_WATCH; T+300s score=55.2 entry=OPEN tier=SILENT_WATCH; T+600s score=55.2 entry=OPEN tier=SILENT_WATCH; T+1800s score=55.2 entry=OPEN tier=SILENT_WATCH; T+3600s score=55.2 entry=OPEN tier=SILENT_WATCH
- `7nXxNWSevB7H4P6E6X6DjjfjYbbaYLQvBy2v7i6hpump` — peak 3777.91x; miss `ENTRY_CHASING`. T+30s score=52.1 entry=OPEN tier=SILENT_WATCH; T+60s score=52.7 entry=OPEN tier=SILENT_WATCH; T+180s score=60.3 entry=CHASING tier=SILENT_WATCH; T+300s score=52.7 entry=OPEN tier=SILENT_WATCH; T+600s score=57.6 entry=UNKNOWN tier=SILENT_WATCH; T+1800s score=36.1 entry=UNKNOWN tier=SILENT_WATCH; T+3600s score=43.3 entry=UNKNOWN tier=SILENT_WATCH
- `9vHAS6wbQcvFrdS4nczoS2dBy3qHfeB9r9BjLT5Wpump` — peak 2968.57x; miss `LOW_RUNNER_SCORE`. T+30s score=57.4 entry=OPEN tier=SILENT_WATCH; T+60s score=53.8 entry=OPEN tier=SILENT_WATCH; T+180s score=53.8 entry=OPEN tier=SILENT_WATCH; T+300s score=53.8 entry=CHASING tier=SILENT_WATCH; T+600s score=48.5 entry=UNKNOWN tier=SILENT_WATCH; T+1800s score=44.7 entry=UNKNOWN tier=SILENT_WATCH; T+3600s score=46.0 entry=UNKNOWN tier=SILENT_WATCH
- `ApxcepWaN7YgDExeBDy7m5zQ6E4DVh4jFQtfCHj4pump` — peak 1803.35x; miss `LOW_RUNNER_SCORE`. T+30s score=54.0 entry=OPEN tier=SILENT_WATCH; T+60s score=54.0 entry=OPEN tier=SILENT_WATCH; T+180s score=54.0 entry=OPEN tier=SILENT_WATCH; T+300s score=54.0 entry=OPEN tier=SILENT_WATCH; T+600s score=40.7 entry=UNKNOWN tier=SILENT_WATCH; T+1800s score=45.1 entry=UNKNOWN tier=SILENT_WATCH; T+3600s score=46.6 entry=UNKNOWN tier=SILENT_WATCH

### False-positive examples

- `3i6JXygrsAedj3be2vJxCRqQXhqXq1bPRAXbXJPrpump` — peak 2.00x; T+3m score=66.0, failure=35.0, tier=STRONG.
- `puALNkgCCgTk7GXoK75CWymoGytKXSMpRicMgtvpump` — peak 2.00x; T+3m score=76.5, failure=0.0, tier=PREMIUM.
- `BEZdmYZeXGBySkrjPunbqwKnVKRXxxgNkm2Fwen6pump` — peak 2.00x; T+3m score=71.3, failure=0.0, tier=STRONG.
- `927nqgNkvTSZMrsDfi2Xv7D6xxTbqS2kTvCJSpAHpump` — peak 2.00x; T+3m score=67.7, failure=35.0, tier=STRONG.
- `93snFcNRfx4PjC1KYD12p5qjbKqxdHdh2RCFrGxWpump` — peak 2.00x; T+3m score=61.8, failure=0.0, tier=STRONG.
- `Bp3g9LubwvScCAWzCfsEdpuoeu1wppZUWKJHwiZ1pump` — peak 2.00x; T+3m score=69.5, failure=35.0, tier=STRONG.
- `9sexhKtozGBkAF7EQdXzxCK1DjZqxfT8sXUWwxRipump` — peak 2.00x; T+3m score=63.5, failure=35.0, tier=STRONG.
- `4rdtuBiwQPr61QfvBjFUbVhdRasf4kJyjSLxXmpJpump` — peak 2.00x; T+3m score=72.9, failure=0.0, tier=STRONG.
- `9rVvhorhVN8nVzWcYpJW1kJ95SnRwhUJykexsxYGLEok` — peak 1.99x; T+3m score=72.5, failure=35.0, tier=STRONG.
- `CcVsYLCk9WVGmPbYwtQypLUPEFWWztoXzxzXbsM6pump` — peak 1.99x; T+3m score=64.2, failure=0.0, tier=STRONG.
- `9iUxSrywb3KcrBA2srRuwrmqXso1fmmeXcWZ8KZYpump` — peak 1.99x; T+3m score=68.5, failure=35.0, tier=STRONG.
- `56Ujiwqw8fPzGWCWKh69waJg7k3eFF9S6RxzDVKRpump` — peak 1.99x; T+3m score=69.8, failure=0.0, tier=STRONG.
- `D448x3LmrzbRCuyKFVUKv9HSG6yuf5i1MZXFtHq2pump` — peak 1.99x; T+3m score=65.6, failure=0.0, tier=STRONG.
- `2nSYUC3Y8pQ9oN8bGbU4STX8FsT2bA5kW5BYMdWipump` — peak 1.99x; T+3m score=67.6, failure=0.0, tier=STRONG.
- `2e7cB8kXpD35notrwh3enaVbsfnFarif4LRq2itmpump` — peak 1.99x; T+3m score=83.1, failure=0.0, tier=PREMIUM.
- `7394WJ3Dxr1syMYs2vU28pZcaaSMFgwPNEVHcUScpump` — peak 1.99x; T+3m score=60.7, failure=0.0, tier=STRONG.
- `9wbZid7fsqkxnVXh7jnHGcqEvL2TtEuJ82b2wP9Apump` — peak 1.99x; T+3m score=77.3, failure=0.0, tier=PREMIUM.
- `HgFA7YvF79sozisYapyJg5jr19PU8osgw4RxBnxDpump` — peak 1.99x; T+3m score=61.7, failure=0.0, tier=STRONG.
- `7QQRKmgxW7sXBBGXG113WuPahmSZAvbPVozZWtwQpump` — peak 1.99x; T+3m score=66.0, failure=0.0, tier=STRONG.
- `8Fh5vRoPeG9UvXuA6UnjppQtsqgA3wcST8ZktHoepump` — peak 1.99x; T+3m score=67.4, failure=35.0, tier=STRONG.

## 21. Diagnostic-window protection

Feature ordering was fitted on 296,327 rows from June 5–20. Models were evaluated on 176,708 later rows from July 5–13. Both are retired diagnostics; neither is sealed or eligible for approval.

## 22–25. Experimental model comparison

All models emit the top 1% of the identical corrected, pre-graduation SOL-denominated diagnostic universe.

| Model | 2x | 3x | 5x | 10x | 20x recall | 50x recall | Failure |
|---|---:|---:|---:|---:|---:|---:|---:|
| RANDOM | 1.47% | 1.08% | 0.51% | 0.28% | 1.47% | 0.94% | 1.02% |
| MARKET_CAP_ONLY | 14.15% | 11.09% | 8.26% | 5.21% | 32.84% | 42.45% | 11.66% |
| VOLUME_ONLY | 13.64% | 6.68% | 2.60% | 0.96% | 3.43% | 0.94% | 14.54% |
| MOMENTUM_ONLY | 11.83% | 5.72% | 2.72% | 0.96% | 3.92% | 4.72% | 11.15% |
| SAFETY_FILTERED_MOMENTUM | 11.88% | 5.72% | 2.72% | 0.96% | 3.92% | 4.72% | 11.26% |
| CONTROL_AVAILABLE_MEAN | 17.26% | 8.26% | 4.36% | 1.75% | 5.39% | 3.77% | 15.17% |
| WEIGHTED_FEATURES | 8.83% | 5.60% | 2.43% | 0.74% | 3.92% | 4.72% | 14.04% |
| TOP_1_FEATURES | 6.62% | 3.85% | 2.04% | 1.30% | 6.86% | 6.60% | 8.89% |
| TOP_2_FEATURES | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |
| TOP_3_FEATURES | 0.06% | 0.06% | 0.00% | 0.00% | 0.00% | 0.00% | 0.06% |
| TOP_5_FEATURES | 3.06% | 1.64% | 0.85% | 0.40% | 1.96% | 1.89% | 1.41% |
| TOP_11_FEATURES | 11.38% | 6.96% | 3.23% | 1.02% | 4.90% | 5.66% | 16.64% |
| MC_PRIOR_PLUS_TOP3 | 15.56% | 11.71% | 8.49% | 5.15% | 30.88% | 38.68% | 13.13% |
| INTERACTION_MC_MOMENTUM | 1.58% | 0.96% | 0.57% | 0.23% | 0.49% | 0.00% | 0.91% |
| INTERACTION_MC_BUYER_GROWTH | 5.49% | 3.79% | 1.87% | 0.79% | 3.43% | 2.83% | 4.41% |
| INTERACTION_MC_CONCENTRATION | 2.83% | 1.92% | 1.25% | 0.91% | 5.88% | 7.55% | 2.21% |
| INTERACTION_MOMENTUM_BUYER_GROWTH | 1.53% | 1.08% | 0.62% | 0.28% | 0.00% | 0.00% | 1.08% |
| INTERACTION_CREATOR_BUYER_GROWTH | 6.23% | 3.51% | 1.87% | 0.79% | 2.94% | 0.94% | 4.47% |
| CALIBRATED_HISTOGRAM | 0.68% | 0.45% | 0.17% | 0.06% | 0.49% | 0.00% | 0.51% |
| STAGE_SPECIFIC | 1.70% | 0.96% | 0.62% | 0.11% | 0.49% | 0.94% | 1.25% |

No experiment is approvable. `CONTROL_AVAILABLE_MEAN` has the highest corrected 2x precision (17.26%); `MC_PRIOR_PLUS_TOP3` has the highest 5x precision (8.49%); and `MARKET_CAP_ONLY` has the highest 10x precision (5.21%). Market-cap priors dominate the corrected right tail, while the observable CONTROL mean remains strongest for 2x. The objectives conflict, so a single uncalibrated mean dilutes cohort-specific evidence.

## Market-cap prior and sweet spots

| T+3m MC (SOL) | Population | 2x | 5x | 10x | 20x | 50x | Failure | Median MAE |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.01-1 | 5,337 | 10.29% | 5.08% | 2.96% | 1.87% | 1.16% | 10.61% | -0.008 |
| 1-5 | 28,577 | 1.60% | 0.59% | 0.28% | 0.15% | 0.09% | 1.54% | 0.000 |
| 5-10 | 88,695 | 0.32% | 0.10% | 0.05% | 0.02% | 0.01% | 0.19% | 0.000 |
| 10-20 | 21,670 | 1.35% | 0.37% | 0.15% | 0.06% | 0.02% | 0.47% | 0.000 |
| 20-30 | 528,526 | 1.48% | 0.05% | 0.01% | 0.01% | 0.00% | 0.02% | 0.000 |
| 30-50 | 43,075 | 1.13% | 0.16% | 0.06% | 0.02% | 0.00% | 0.22% | -0.002 |
| 50-100 | 20,114 | 2.30% | 0.39% | 0.17% | 0.08% | 0.03% | 0.74% | 0.000 |
| 100-250 | 4,976 | 8.76% | 1.99% | 0.76% | 0.34% | 0.14% | 6.51% | -0.250 |
| 250-1000 | 750 | 13.73% | 3.33% | 0.80% | 0.27% | 0.00% | 24.67% | -0.838 |
| >=1000 | 57 | 1.75% | 1.75% | 1.75% | 1.75% | 1.75% | 5.26% | -0.000 |

The <1 SOL revival/extreme-low bucket is the strongest right-tail prior but carries 10.61% terminal failure. The 250–1000 SOL bucket has 13.73% 2x but 24.67% failure and a -0.838 median adverse excursion. Both are high-risk specialist regimes, not general PREMIUM evidence. The 20–30 SOL mass is a dead zone.

## Runner-score calibration

| Score bucket | Sample | 2x | 5x | 10x | 20x |
|---|---:|---:|---:|---:|---:|
| 0-10 | 0 | N/A | N/A | N/A | N/A |
| 10-20 | 0 | N/A | N/A | N/A | N/A |
| 20-30 | 15 | 73.33% | 53.33% | 20.00% | 6.67% |
| 30-40 | 16,994 | 0.54% | 0.14% | 0.05% | 0.02% |
| 40-50 | 131,666 | 1.24% | 0.24% | 0.10% | 0.04% |
| 50-60 | 520,326 | 1.37% | 0.09% | 0.04% | 0.02% |
| 60-70 | 49,621 | 2.15% | 0.47% | 0.21% | 0.11% |
| 70-80 | 21,537 | 4.27% | 0.66% | 0.28% | 0.16% |
| 80-90 | 2,758 | 12.73% | 3.15% | 1.34% | 0.51% |
| 90-100 | 0 | N/A | N/A | N/A | N/A |

The score is not calibrated: most rows collapse into 50–60, and the tiny 20–30 bucket has much higher outcomes than adjacent buckets. Thresholds 60 and 75 separate some 2x probability, but not a monotonic calibrated probability scale.

## Failure-score calibration and penalty effectiveness

| Failure score | Sample | 2x | Terminal failure |
|---|---:|---:|---:|
| 0-20 | 118,844 | 2.12% | 0.71% |
| 20-40 | 361,607 | 2.30% | 0.43% |
| 40-60 | 43,314 | 0.04% | 0.00% |
| 60-80 | 211,878 | 0.17% | 0.26% |
| 80-100 | 7,249 | 0.00% | 0.00% |

| Observable penalty | Sample | 2x | 5x | Terminal failure |
|---|---:|---:|---:|---:|
| CONCENTRATION_UNKNOWN | 74,715 | 0.04% | 0.01% | 0.00% |
| TOXIC_CREATOR | 518,905 | 1.63% | 0.14% | 0.37% |
| POOR_TRADEABILITY | 899 | 23.69% | 11.35% | 70.86% |
| BUYER_COLLAPSE_PROXY | 299,294 | 0.12% | 0.03% | 0.05% |
| LIQUIDITY_DETERIORATION | 0 | N/A | N/A | N/A |

The observed failure score is a lower bound. Missing sell-restriction, connected cluster, terminal-safety and liquidity-deterioration histories prevent a definitive claim that the live FailureScore is or is not killing runners.

## Buyer, creator, wallet and interaction findings

- Buyer count/growth/acceleration are observed and highly informative; retention, seller replacement, actor independence and cluster concentration are not.
- The live 85/55/20 buyer compression discards magnitude and acceleration. Buyer count and growth show strong positive 5x/10x/right-tail descriptive effects.
- Source-reported point-in-time creator counts are positive descriptively, but no aligned funder history or point-in-time wallet-quality vector exists for control tests.
- No tested interaction grid beats the additive observable CONTROL mean. Feature interactions are therefore not shown necessary by this evidence.

## Stage and regime results

NEW has higher reconstructed core precision than BONDING; MIGRATED has no public-core signals because unit-consistent entry evidence is missing. Weekly precision varies materially, confirming regime instability. SOL-volatility segmentation is unavailable and was not fabricated; launch-intensity results are present in the JSON artifact.

## 26. Intelligence-path code audit

1. RunnerScore is the unweighted mean of whatever numeric stage features happen to be known; missingness silently changes feature weights per token.
2. Market cap is absent from RunnerScore and only influences entry/payoff indirectly.
3. `survival_engine` can emit score 100 with fewer than three known inputs even while grading the same evidence merely ACCEPTABLE.
4. `migration_continuity` is always populated as `None` in the live service.
5. `liquidity_deterioration` has a failure weight but is never populated by the live feature builder.
6. Buyer trajectory compresses evidence to 85/55/20, losing magnitude and timing.
7. The autopsy itself found and rejected the prior corrupted peak field, salted random ranking, same-window feature ordering, and non-causal miss precedence before reporting.

No live scoring change was made: changing these items would alter frozen CONTROL and requires later independent validation.

## 27. Evidence-backed proposed fixes

1. **Replace outcome plumbing first.** Use the dimensionless PIT outcome builder; risk is label drift; validate against later seven-day data and manual path samples.
2. **Make market-cap regime explicit.** Separate extreme-low revival/high-risk and overextended specialist paths; risk is rug concentration; require terminal-failure and drawdown gates on a new holdout.
3. **Preserve raw buyer dimensions.** Test buyer count, growth and acceleration instead of 85/55/20; risk is provider/gameability drift; validate chronologically by regime.
4. **Calibrate per cohort.** Separate QUICK_2X, MID_5X and RIGHT_TAIL_20X objectives; risk is signal fragmentation; compare at fixed frequency on later data.
5. **Align survival score with evidence confidence.** Do not award 100 for sparse acceptable evidence; risk is reduced recall; ablate prospectively.
6. **Acquire missing vectors.** Production DB, aligned wallet/funder/cluster/liquidity and seven-day outcomes are prerequisites for a definitive failure-gate autopsy.

## 28–30. Best model, tests and final truth

- Best corrected diagnostic 2x precision: **17.26%** (`CONTROL_AVAILABLE_MEAN`).
- Best corrected diagnostic 5x precision: **8.49%** (`MC_PRIOR_PLUS_TOP3`).
- Best corrected diagnostic 10x precision: **5.21%** (`MARKET_CAP_ONLY`).
- Best 20x recall: **32.84%** (`MARKET_CAP_ONLY`); best 50x recall: **42.45%** (`MARKET_CAP_ONLY`).
- Approved features: **0**. Challenger decisions: **0**.

### Final truth

- **DO WE KNOW WHY JR MISSES RUNNERS? YES**, within the observable reconstruction.
- **PRIMARY FAILURE SOURCE: INTELLIGENCE**, followed by ENTRY.
- **IS CURRENT RUNNER SCORE STRUCTURALLY FLAWED? YES.**
- **IS MARKET CAP BEING UNDERUSED? YES**, but market-cap-only is not the corrected best 2x model and its strongest regimes are high failure-risk.
- **IS FAILURE SCORE KILLING GOOD RUNNERS? INCONCLUSIVE.** Observable penalties are not the decisive 5x+ miss, but the exact live risk vector is unavailable.
- **ARE FEATURE INTERACTIONS NECESSARY? NO evidence of necessity.**
- **DOES ANY EXPERIMENT BEAT MARKET-CAP-ONLY? YES**, on retired diagnostics only.
- **PRODUCTION READY: NO.**

## Validation record

- Full pytest: **183 passed**, **5 subtests passed**, one upstream `audioop` deprecation warning.
- Ruff: **all checks passed**.
- Replay/persistence/Discord/system-hardening regression group: **65 passed**.
- Deterministic autopsy replay: **PASS**, two independent artifacts both SHA-256 `B042494D1997DB53A0DA2FEEEC59BDEEAB1F96DFD96F8692C50282D0B07EEC53`.
- Bounded restart/load soak: **PASS** at 10,000 events; 10,256 persisted, 10,000 duplicate replays suppressed, `quick_check=ok`, zero foreign-key violations, and reconciled state.
- Package build: **PASS**, wheel (217,385 bytes) and sdist (245,090 bytes).
- Live CONTROL changes: **none**. Approved features: **0**. Challenger decisions: **0**. Production deployment: **not authorized**.

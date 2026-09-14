# E4 older-trade sizing / exit confirmation

Scope: 501 normal-size reconstructed E4 positions (`1 <= cost_sol < 50`) from the pinned older same-window evidence covering 2026-09-03 through 2026-09-14. This is source-wallet behavioral analysis only; it does not expose E4's private decision logic and must not be treated as a copy strategy.

## Aggregate sample

- Positions: 501
- Wins/losses: 344 / 157
- Win rate: 68.66%
- Reconstructed net P&L: +241.7504 SOL
- Profit factor: ~4.384
- Mean winning return on deployed SOL: +23.73%
- Mean losing return on deployed SOL: -9.25%
- Mean win / mean loss magnitude: ~2.56:1
- Every one of the 501 positions had exactly one reconstructed BUY.

Nine records were only 65-88% sold within the evidence capture; 492 were >=98% sold. The exit timing conclusions below are essentially unchanged when restricted to those 492 fully exited positions.

## First-partial rule is highly structured

First sell fraction across all 501 positions:

| First token fraction sold | Count | Share |
|---:|---:|---:|
| 30% | 399 | 79.64% |
| 20% | 80 | 15.97% |
| 17% | 7 | 1.40% |
| 100% | 7 | 1.40% |
| 25% | 5 | 1.00% |
| 15% | 2 | 0.40% |
| 5% | 1 | 0.20% |

Thus 479/501 = **95.61%** of older positions used either a 30% or 20% initial token trim.

There is a striking size boundary inside those dominant cohorts:

- every reconstructed 30% first-partial position had observed cost between **1.218 and 4.697 SOL**;
- every reconstructed 20% first-partial position had observed cost between **4.728 and 21.070 SOL**.

This strongly supports a mechanical size/conviction regime switch around the ~4.7 SOL observed-cost area. It does not reveal the hidden variable or exact internal threshold E4 uses, but the wallet behavior itself is not random.

## Position-size cohorts

| Observed E4 entry cost | Trades | WR | Dominant first partial | Winner median hold | Loser median hold |
|---|---:|---:|---:|---:|---:|
| 1-2 SOL | 147 | 57.14% | 30% (95.2%) | 4s | 2s |
| 2-3 SOL | 128 | 67.97% | 30% (96.9%) | 5s | 2s |
| 3-5 SOL | 143 | 79.02% | 30% (94.4%) | 6s | 3s |
| 5-10 SOL | 66 | 71.21% | 20% (93.9%) | 9s | 5s |
| 10-50 SOL | 17 | 76.47% | 20% (100%) | 11s | 4.5s |

Observed position size clearly contains information: source WR rises from ~57% in the 1-2 SOL cohort to ~79% in the 3-5 SOL cohort. The relationship is not perfectly monotonic, so larger size must not be interpreted as certainty.

## Exit asymmetry persists in the older sample

Across the full 501-position set:

- winner median hold: **6 seconds**;
- loser median hold: **3 seconds**;
- winner mean hold: ~7.29 seconds;
- loser mean hold: ~3.35 seconds;
- winner median number of sells: **4**;
- loser median number of sells: **2**;
- **86.62%** of losers were fully exited within 5 seconds;
- only **46.80%** of winners were fully exited within 5 seconds.

The exit policy also scales with source position size. On winning positions:

- 1-2 SOL: median 3 sells, 4s hold;
- 2-3 SOL: median 3 sells, 5s hold;
- 3-5 SOL: median 4 sells, 6s hold;
- 5-10 SOL: median 6 sells, 9s hold;
- 10-50 SOL: median 6 sells, 11s hold.

This is strong evidence of an initial-plus-runner / progressive scale-out architecture rather than a fixed-time 100% exit.

## Representative older trades

These examples pre-date the newest same-window 13-trade study and show the same behavior.

| Timestamp UTC | Result | Entry | Return | Hold | Sells | First partial |
|---|---|---:|---:|---:|---:|---:|
| 2026-09-03 11:30:50 | W | 2.712 SOL | +7.26% | 4s | 3 | 30% |
| 2026-09-03 12:01:48 | W | 1.660 SOL | +37.75% | 6s | 4 | 30% |
| 2026-09-03 12:25:34 | L | 1.322 SOL | -3.83% | 3s | 2 | 30% |
| 2026-09-03 15:04:07 | L | 4.216 SOL | -6.85% | 3s | 2 | 30% |
| 2026-09-03 15:11:01 | W | 5.559 SOL | +20.24% | 9s | 7 | 20% |
| 2026-09-03 15:26:36 | W | 6.627 SOL | +16.69% | 10s | 7 | 20% |
| 2026-09-03 16:20:03 | W | 6.520 SOL | +119.76% | 14s | 11 | 20% |
| 2026-09-03 17:09:20 | W | 7.659 SOL | +46.48% | 21s | 11 | 20% |
| 2026-09-03 22:22:13 | L | 8.155 SOL | -24.74% | 9s | 4 | 20% |
| 2026-09-03 23:10:33 | L | 14.478 SOL | -50.94% | 2s | 2 | 20% |
| 2026-09-03 23:32:43 | L | 6.465 SOL | -3.81% | 5s | 4 | 20% |
| 2026-09-03 23:34:38 | L | 5.660 SOL | -11.57% | 3s | 3 | 20% |

The very large older losses are important: E4's management reduces losses on average but does not guarantee a small loss. Some launches can move/gap violently enough that even its normal management produces -20% to -50% outcomes.

## What can be confirmed vs inferred

### Confirmed from wallet behavior

1. **One-shot entry:** 501/501 positions had one BUY; no averaging-in pattern appears in this sample.
2. **Variable position size:** entry size is not constant and is associated with materially different source win rates.
3. **Two dominant initial regimes:** 30% first trim below the high-size regime and 20% first trim in the high-size regime; together they cover 95.61% of positions.
4. **Fast loser management:** losers are closed materially sooner and with fewer sells.
5. **Winner runners:** winners receive more sell legs and remain open longer, especially at larger position sizes.
6. **Eventual flattening:** the overwhelming majority of the reconstructed positions are sold effectively 100% by the end of the observed position lifecycle.

### Strong inference, not private-rule proof

The wallet behavior is consistent with a structure like:

- choose a position-size/conviction tier at entry;
- one-shot BUY;
- take an early initial, normally 30% on standard positions and 20% on high-size positions;
- reassess the remaining position dynamically;
- cut weak trades relatively quickly;
- keep strong trades as runners and distribute exits across more legs;
- fully flatten eventually.

We cannot yet claim the hidden triggers are fixed P&L percentages, fixed time thresholds, reserve/order-flow rules, or a specific scoring formula. Exact trigger inference requires market-path data around every sell.

## Implication for Golden

The older 501-trade sample independently confirms the same broad architecture seen in the newest 13 E4 trades. This makes E4's partial/runner behavior a legitimate management benchmark for Golden research. It still should not be copied blindly: Golden must prospectively test its own stop/initial/runner policies against its own selected trades.

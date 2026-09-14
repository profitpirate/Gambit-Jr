# E4 V12 — wallet-conviction Golden Thesis lane

Status: exploratory research only. This document does **not** alter, overwrite, or certify the existing Golden Thesis candidates.

## Sources

- Recent live wallet/copy capture: GitHub Actions run `34809168521` (`E4 V12 selection certification`, run #66), 3,000 newly launched tokens, 305,804 decoded live events, plus the most recent 1,000 E4-wallet signatures.
- Frozen all-out corpus: run `34262315056`, 66,000 launches. Corpus SHA256: `6f41376cfee3d54d57774b4368ec8b50c9e59becbbd04650cf56a20ef48dce6c`.
- Source wallet: `E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz`.

## Main conclusion

The evidence says Golden Thesis research should stop treating **every observed E4 selection as one homogeneous target**.

E4 position size, launch-to-entry delay and exit behaviour form materially different cohorts. The strongest cohort is not "all E4 trades"; it is the wallet's **normal/high-conviction, ultra-early entries**. The source wallet also cuts losing positions materially faster than it holds many winners.

The new lane therefore treats source size/delay as **label construction only**. They must never be used as pre-entry model inputs because they are observed only after E4 has acted.

## Wallet history findings

Across the pinned same-window E4 evidence, 518 closed E4 positions were reconstructed. The unfiltered sample is contaminated by at least one obvious non-Pump/stablecoin position (202 SOL on `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`), so source-wallet studies must restrict to same-window Pump launches and/or validated Pump trade-program transactions before calculating economic statistics.

After restricting to normal-size same-window E4 positions with `1 <= cost_sol < 50`:

- 501 trades
- 344 wins / 157 losses
- 68.66% win rate
- +241.7504 SOL reconstructed P&L
- profit factor ≈ 4.38
- median hold ≈ 5 seconds

Position size is strongly associated with the source wallet's observed win rate:

| E4 source cost | Trades | Observed WR |
|---|---:|---:|
| 1.0–2.0 SOL | 155 | ~59% |
| 2.0–3.0 SOL | 128 | ~68% |
| 3.0–5.0 SOL | 143 | ~79% |
| 5.0–10.0 SOL | 66 | ~71% |

Using simple minimum-size cohorts across the same evidence:

- `cost >= 1 SOL`: 68.66% WR
- `cost >= 2 SOL`: 73.45% WR
- `cost >= 2.5 SOL`: 77.01% WR
- `cost >= 3 SOL`: 76.55% WR

This is evidence that source size should be treated as a **post-entry conviction label**, not ignored.

## Ultra-early entries

In the frozen 66k launch corpus, normal E4 landed positions (`1 <= cost_sol < 50`) show a strong relation between launch-to-first-E4-buy delay and outcome:

- E4 first buy <= 1 ms: 63 trades, 84.1% WR, +37.07 SOL
- <= 2 ms: 88 trades, 75.0% WR, +40.87 SOL
- <= 5 ms: 112 trades, 72.3% WR, +46.21 SOL
- <= 10 ms: 145 trades, 70.3% WR, +58.70 SOL

Combining source conviction and speed gives an especially interesting target cohort:

- `cost >= 2 SOL` and first E4 buy `<= 5 ms`: 75 trades, 78.67% WR
- `cost >= 2 SOL` and first E4 buy `<= 10 ms`: 98 trades, 76.53% WR
- `cost >= 2.5 SOL` and first E4 buy `<= 5 ms`: 60 trades, 80.0% WR
- `cost >= 2.5 SOL` and first E4 buy `<= 10 ms`: 78 trades, 78.2% WR

These figures are descriptive source-wallet statistics, **not a certified tradable strategy**.

## Why direct copy is not the answer

The latest exact 3,000-launch capture contained five closed source positions that were also filled by the direct-copy replay.

Source E4:

- 5 closed
- 2 wins / 3 losses
- +0.55098 SOL
- PF 1.51

Direct-copy replay:

- 5 closed
- 0 wins / 5 losses
- -0.02220 SOL
- PF 0

Median source-to-copy fill delay was ~299 ms. In the individual matched trades, source E4 buys arrived roughly 0.25–6.18 ms after CREATE, while the replay fills arrived ~267–504 ms after observing E4. Some fill drift exceeded 1,500 bps.

Therefore Golden Thesis research should aim to predict the qualifying launch from CREATE / early non-E4 market features, not wait for E4's transaction and then chase it.

## Exit behaviour

On the 501 normal-size same-window E4 positions:

- winners: median hold ~6 s
- losers: median hold ~3 s
- 86.6% of losers were fully exited within 5 s
- only 46.8% of winners were fully exited within 5 s
- winners had median 4 sells; losers median 2 sells

This is inconsistent with treating the source strategy as a fixed `hold_1000ms` or `hold_2000ms` system. Entry research and exit research should be separated. The source appears to cut losers quickly while scaling out of winners.

## Pre-entry signals worth researching

A 0 ms / CREATE-time comparison of the `cost >= 2 SOL, <=10ms E4 entry` cohort versus the rest of the 66k corpus shows unusually strong differences in:

- creator prior E4 selection count
- creator prior landed count
- creator prior known wins / win rate
- creator prior failed-fill count
- creator buy SOL in the CREATE transaction
- cashback-enabled launches
- avoidance of mayhem mode

By 10 ms (still excluding the E4 wallet itself from market features), the strongest cohort also shows:

- multiple distinct buy signatures
- more unique outside buyers
- more outside-buy SOL
- known E4-linked outside buyers
- high buyer-prior-selection and buyer-prior-win sums
- higher 10 ms FDV / reserve movement

This suggests two separate research lanes:

1. **0 ms pre-E4 conviction lane** — creator/history/CREATE-transaction structure only.
2. **10 ms confirmation lane** — adds non-E4 buyer-network and early-flow confirmation. This lane must be evaluated with realistic 10 ms+ execution, not described as pre-E4 if E4 has already entered.

## Exploratory rule check

An interpretable rule motivated by the source history:

- non-mayhem
- cashback enabled
- creator prior selection count >= 3
- creator prior known win rate >= 25%
- 2-second paper hold

produced ~83.3% worst-latency validation WR over 42 validation signals across 0/1/2/5/10 ms paper entries, but only ~61.6% worst-latency WR over 112 holdout signals. Profit factor remained positive/high.

This is **not** a Golden Thesis pass. It is useful because it shows the wallet-derived features contain signal, while also proving that the simple hand rule is not robust enough.

## Required vNext research contract

1. Keep existing 75% / 86.96% candidates frozen and separate.
2. Build a new target label around high-conviction, ultra-early E4 selections, starting with `landed && cost >= 2 SOL && first_buy_delay <= 10 ms`.
3. Use source size and source delay **only as labels/audit fields**, never entry features.
4. Search 0/1/2/5/10 ms causal feature horizons separately.
5. Exclude the E4 wallet itself from market-flow inputs.
6. Remove non-Pump/stablecoin wallet reconstruction contamination before source-performance summaries.
7. Maintain chronological train / validation / untouched holdout separation.
8. After research selection, freeze exactly one candidate and test it prospectively on newly arriving launches with the Operations Supervisor.
9. Separately research an asymmetric exit controller inspired by E4's observed loser-cut / winner-scale-out behaviour; do not use future hold time or future P&L as entry features.
10. Do not lower live thresholds merely to force trades.

The research hypothesis is now: **predict E4's high-conviction ultra-early cohort, not E4 activity in general.**

# E4 same-window trade forensics

Scope: E4 wallet activity reconstructed during the first Golden 5 SOL prospective session. E4 is a benchmark only and is not an entry signal for Golden.

## Headline

- Same-window positions opened: 14
- Closed positions used for performance analysis: 13
- Wins/losses: 8 / 5
- Win rate: 61.54%
- Realized P&L: +4.513252551 SOL
- Profit factor: 5.1120
- Mean winning return on entry cost: +19.41%
- Mean losing return on entry cost: -6.00%
- Mean win / mean loss return magnitude: about 3.24:1
- Median winner hold: 4.5 s
- Median loser hold: 2.0 s
- Median winner entry size: about 2.98 SOL
- Median loser entry size: about 1.37 SOL

The average Golden first-11 winner (+20.95%) was not materially smaller than the average E4 winner (+19.41%). The major difference was loss containment: Golden's average loser was -20.65% while E4's was about -6.00%.

## Trade-level behavior

| # | Result | Entry SOL | Return | Buy count | Sell split by token % | Sell timing after first buy | Total observed hold |
|---|---|---:|---:|---:|---|---|---:|
| 1 | W | 1.322 | +3.92% | 1 | 30 / 70 | +1s, +2s | 2s |
| 2 | W | 3.310 | +14.38% | 1 | 30 / 10.5 / 59.5 | ~0s, +2s, +4s | 4s |
| 3 | W | 1.711 | +8.29% | 1 | 30 / 70 | ~0s, +3s | 3s |
| 4 | L | 1.322 | -2.53% | 1 | 30 / 35 / 35 | ~0s, +2s, +2s | 2s |
| 5 | L | 1.334 | -5.04% | 1 | 30 / 70 | ~0s, +2s | 2s |
| 6 | W | 5.378 | +42.65% | 1 | 20 / 13.6 / 13.28 / 13.28 / 13.15 / 13.35 / 13.35 | ~0s, +4s, +5s, +8s, +10s, +13s, +16s | 16s |
| 7 | W | 3.294 | +6.62% | 1 | 30 / 11.9 / 58.1 | ~0s, +3s, +5s | 5s |
| 8 | W | 3.239 | +57.69% | 1 | 30 / 11.9 / 11.62 / 46.48 | ~0s, +6s, +9s, +10s | 10s |
| 9 | W | 1.370 | +2.17% | 1 | 30 / 70 | ~0s, +2s | 2s |
| 10 | L | 2.187 | -6.14% | 1 | 30 / 70 | +1s, +3s | 3s |
| 11 | L | 1.371 | -5.18% | 1 | 30 / 70 | ~0s, +2s | 2s |
| 12 | L | 7.136 | -11.09% | 1 | 17 / 66.4 / 16.6 | +3s, +5s, +5s | 5s |
| 13 | W | 2.720 | +19.54% | 1 | 30 / 11.9 / 58.1 | +1s, +4s, +5s | 5s |

Block time has one-second granularity, so `~0s` means the buy and first sell share the same recorded block-time second; slot order still confirms the sell followed the buy.

## Initials / partials

All 13 closed positions had exactly one buy and then multiple sell transactions. E4 did not average into any of these positions after entry.

Every closed position was eventually sold fully, but none was closed in one single sell transaction. Each used 2-7 sell legs.

The strongest repeated pattern is the first trim:

- 11/13 trades sold exactly 30% of tokens on the first sell;
- one large winner sold 20% first;
- the largest loser sold only 17% first;
- 12/13 trades took the first trim within roughly one second of entry;
- the largest loser was again the exception, waiting roughly three seconds for its first trim.

Across all 13, the first sell returned roughly 31.75% of initial SOL cost on average. This is consistent with an early capital-recovery / risk-reduction behavior, though the on-chain record cannot reveal E4's private reason for placing the sell.

## Winner management

E4 clearly does not use a universal two-second exit.

The large +42.65% winner was held for about 16 seconds. E4 sold 20% immediately and then distributed the remaining 80% across six further exits at approximately +4, +5, +8, +10, +13 and +16 seconds.

The +57.69% winner was held for about 10 seconds: 30% was taken immediately, then approximately 11.9% at +6s, 11.6% at +9s and the final 46.5% at +10s.

This demonstrates a genuine initial-plus-runner structure. The chain alone cannot tell whether later sells were triggered by P&L thresholds, order-flow changes, reserve behavior, time rules or a mixture.

## Loss management

The five realized losing returns were approximately:

- -2.53%
- -5.04%
- -6.14%
- -5.18%
- -11.09%

Four of five losses were contained between roughly -2.5% and -6.2%. This contrasts strongly with Golden's two -40%+ losses under a mandatory two-second hold.

The outlier is especially informative: E4's largest position in this sample, about 7.136 SOL, became its largest loss (-11.09%). It also broke the common management pattern: instead of trimming 30% immediately/within one second, the first sell was only 17% and occurred about three seconds after the buy. It then liquidated the remaining 83% around five seconds.

A mathematical -10% cap would only have improved this sample by about 0.078 SOL because E4 already kept the other losses smaller than 10%. A -5% cap would have improved the realized sample by about 0.463 SOL if every stop were fillable exactly and if no eventual winners were stopped first. Those are counterfactual upper-level calculations only: without full intratrade MFE/MAE paths, they do not prove such stops would improve the strategy.

## Sizing behavior

Sizing is variable and appears to encode some conviction, but it is not perfectly predictive.

- Median winner size: ~2.98 SOL
- Median loser size: ~1.37 SOL
- 2.5-4 SOL positions in this sample: 4/4 winners
- The 5.378 SOL position won +42.65%
- The 7.136 SOL position lost -11.09%

Thus larger size often accompanied stronger outcomes, but the largest bet was wrong. Any Golden sizing model should therefore use conviction as a bounded multiplier rather than treating high conviction as certainty.

## Entry timing limitation

The wallet reconstruction tells us when E4's first on-chain BUY occurred, but not when E4's private requirements became satisfied. We do not have E4's internal signal timestamp or hidden decision rule, so `signal -> execution` latency cannot be measured honestly from wallet transactions alone.

The block-time field is also second-resolution, although slot ordering lets us see that many initial sells happened in the next slot after entry. Historical E4 research on the frozen corpus showed that ultra-early entry timing is strongly associated with E4 success, but that should not be substituted for exact signal-to-buy timing on these 13 same-window trades.

## Exit-reason limitation

The chain proves *what E4 did*, not *why it did it*.

Observed behavior strongly supports:

1. one-shot entry rather than averaging in;
2. very early initial/partial profit or risk reduction;
3. quicker liquidation of losers;
4. longer and more fragmented scale-outs on strong winners;
5. eventual full closure of the token position.

But it cannot prove the private trigger for each exit. To learn that, future E4 benchmark telemetry needs same-mint market-state paths around every sell: reserve changes, order flow, P&L before each leg, MFE/MAE, buyer/seller flow, and post-final-exit continuation.

## Main lesson for Golden

The most important difference in this sample is not that E4's winners were much larger than Golden's. They were not: average returns on winning trades were approximately +19.4% for E4 versus +20.95% for Golden's first 11.

The difference is that E4's losing trades averaged only about -6.0%, compared with roughly -20.65% for Golden. E4 then combines that loss containment with partial exits, longer runners on exceptional winners and variable sizing.

So the immediate management research priority for Golden is:

- preserve the entry thesis while measuring intratrade paths;
- test early 20-30% initial trims;
- test rapid invalidation / loss containment;
- allow the remainder to run when flow remains favorable;
- scale out progressively rather than forcing a 100% two-second exit;
- keep conviction sizing bounded because E4's largest same-window position was also its largest loss.

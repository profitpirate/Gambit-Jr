# Golden Buyer Reputation v1 — First 11 Trade Forensics

Scope: first completed 5 SOL prospective live-paper session only. Entry thesis remains frozen. This document analyzes execution/management behavior; it does not retune the selection rule.

## Session headline

- Closed trades: 11
- Wins/losses: 6 / 5
- Win rate: 54.55%
- Net P&L: +0.0201532606 SOL
- Ending equity: 5.0201532606 SOL
- Profit factor: 1.2100
- Gross winning P&L: +0.1161147012 SOL
- Gross losing P&L: -0.0959614406 SOL
- Mean winning return on stake: +20.95%
- Mean losing return on stake: -20.65%
- Current average-win / average-loss magnitude is therefore approximately 1.01:1, not the desired asymmetric payoff profile.

## Trade-level forensic table

| # | Result | Return on stake | Buyers | Prior appearances | Prior WR | CREATE→decision | CREATE→entry | decision→entry | Actual hold |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | L | -3.51% | 4 | 25 | 72.00% | 10.329 ms | 21.045 ms | 10.717 ms | 2000.450 ms |
| 2 | L | -40.19% | 4 | 29 | 75.86% | 10.298 ms | 20.814 ms | 10.517 ms | 2000.566 ms |
| 3 | W | +4.85% | 5 | 133 | 72.18% | 11.931 ms | 22.579 ms | 10.649 ms | 2001.004 ms |
| 4 | L | -9.50% | 3 | 29 | 86.21% | 10.163 ms | 20.922 ms | 10.759 ms | 2000.817 ms |
| 5 | W | +47.90% | 2 | 132 | 72.73% | 10.521 ms | 21.132 ms | 10.611 ms | 2000.713 ms |
| 6 | W | +7.92% | 3 | 12 | 75.00% | 10.263 ms | 21.021 ms | 10.758 ms | 2000.231 ms |
| 7 | W | +21.81% | 3 | 15 | 80.00% | 10.348 ms | 21.019 ms | 10.670 ms | 2000.335 ms |
| 8 | W | +36.14% | 4 | 12 | 75.00% | 10.260 ms | 20.458 ms | 10.197 ms | 2000.729 ms |
| 9 | W | +7.07% | 4 | 31 | 70.97% | 10.555 ms | 20.708 ms | 10.153 ms | 2000.675 ms |
| 10 | L | -42.30% | 4 | 26 | 76.92% | 10.703 ms | 21.642 ms | 10.939 ms | 2000.295 ms |
| 11 | L | -7.74% | 3 | 31 | 74.19% | 10.509 ms | 20.948 ms | 10.440 ms | 2000.744 ms |

Mean CREATE→decision: 10.535 ms. Mean decision→entry: 10.583 ms. Mean CREATE→entry: 21.117 ms. Range CREATE→entry: 20.458–22.579 ms.

Important: the frozen historical reputation labels use the corpus field `paper_10ms_hold_2000ms_pnl_sol`, i.e. a 10 ms paper entry. The prospective runner intentionally adds a 10 ms execution-delay floor after the 10 ms decision horizon, making the live paper entry approximately 20–22 ms after CREATE. Therefore the prospective experiment is a stricter latency test than the historical 10 ms paper labels and this latency gap must be measured rather than silently conflated.

## What actually happened on exits

The current live runner does not have an adaptive exit engine.

For every trade it:

1. computes the Golden qualification at approximately CREATE +10 ms;
2. waits the explicit execution-delay floor;
3. paper-buys one position;
4. waits `HOLD_MS = 2000`;
5. calls `quote_sell(tokens, exit_state)` on the entire token balance;
6. closes 100% of the position.

There are no initials, no partial profit taking, no stop loss, no take-profit ladder, no trailing stop, no scale-out logic and no runner. Every trade is a 100% exit after a fixed two-second sleep.

This means the exit reason for all 11 trades was not a market signal. It was simply: `fixed 2,000 ms research hold expired`.

## Loss analysis

The five losing returns were approximately:

- -3.51%
- -40.19%
- -9.50%
- -42.30%
- -7.74%

The two catastrophic losses (#2 and #10) contributed approximately 0.076800 SOL of the 0.095961 SOL total gross losses — about 80.0% of all losing P&L in the session.

A purely mathematical -10% loss cap, if it had been executable exactly at that level and changed nothing else, would have increased session net P&L from +0.02015 SOL to approximately +0.07834 SOL. This is not evidence that a -10% stop was actually fillable; the first-session telemetry did not preserve the intratrade state path needed to prove when a stop level was crossed, whether the move gapped through it, or what slippage would have occurred.

Therefore the correct conclusion is not “use a -10% stop.” It is that loss containment is a high-value research target because two outsized losses dominated the entire loss distribution.

## Winner analysis

The six winning returns were approximately:

- +4.85%
- +47.90%
- +7.92%
- +21.81%
- +36.14%
- +7.07%

The fixed two-second exit forcibly closed the +47.9%, +36.1% and +21.8% winners regardless of whether momentum was strengthening or weakening. The first-session telemetry stopped at the research exit and did not preserve a sufficiently long post-exit path, so it cannot truthfully answer how much larger those winners could have become at +3s, +5s, +10s or with a trailing runner.

That missing telemetry should be treated as an observability defect for exit research, not filled with assumptions.

## Repeated buyer-cluster evidence

The first 11 trades reveal that treating wallet histories as independent evidence may be too crude.

### Losing cluster A

Trades #1 and #2 had the exact same four early buyers. Trade #11 contained three of those same four buyers. All three trades lost:

- #1: -3.51%
- #2: -40.19%
- #11: -7.74%

The frozen rule aggregates reputation by summing appearances and wins across wallets. If several wallets repeatedly operate as one correlated cluster, their evidence can be counted multiple times even though it may represent one behavioral entity. This is a serious candidate explanation for false confidence.

### Winning cluster B

Trades #6 and #7 had the exact same three early buyers. Trade #9 contained those same three buyers plus one additional wallet. All three won:

- #6: +7.92%
- #7: +21.81%
- #9: +7.07%

This suggests buyer-cluster identity itself may contain persistent signal. The correct adaptation is not automatically to blacklist losing wallets or whitelist winning wallets; it is to model correlated clusters causally and test whether cluster-level reputation is more informative than naïvely summing wallet-level evidence.

### Mixed cluster C

Trades #8 and #10 shared three of four buyers. #8 won +36.14%; #10 lost -42.30%. This is useful evidence against simplistic cluster whitelisting.

## Selection-rule observations

The losses were not simply the weakest-looking qualifiers. In fact:

- loss #4 had the highest prior aggregate WR of the entire 11-trade sample: 86.21%;
- loss #10 qualified at 76.92%;
- loss #2 qualified at 75.86%.

Meanwhile some winners qualified close to the 70% floor.

So merely raising the 70% aggregate-WR threshold is not justified by this sample. The more promising questions are correlation-adjusted reputation, sample quality, regime decay, execution latency and post-entry market behavior.

The two trades with very deep aggregate prior evidence (132 and 133 prior appearances) were both winners. However 11 trades is insufficient to promote “high appearances = larger size” into a live sizing rule. It is appropriate as a shadow conviction feature only.

## Timing and market-state observations

The rule did not buy immediately when qualification became knowable. Qualification became available at roughly +10.535 ms on average, followed by another +10.583 ms average delay before simulated entry. Average CREATE→entry was therefore +21.117 ms.

This is deliberate in the current live test, but it creates a critical diagnostic question: which outcomes changed between the 10 ms decision state and the approximately 20–22 ms execution state? The first-session ledger did not preserve both executable state snapshots, so this cannot be reconstructed exactly from the ledger alone.

The next telemetry version must preserve decision-state quote, entry-state quote and the full post-entry state path so the cost of execution latency can be measured per trade.

## Required prospective trade-path telemetry

Without changing the frozen entry selection, future qualified trades should record a shadow path long enough to answer execution and exit questions objectively:

- decision-state price/curve and hypothetical fill at +10 ms;
- actual paper-entry state and fill at ~+20 ms;
- every market-changing state while the position is open;
- MFE and MAE, including time-to-MFE and time-to-MAE;
- counterfactual mark/exit at +50, +100, +250, +500, +750, +1000, +1500, +2000, +3000, +5000 and +10000 ms after entry;
- first crossing and executable quote for loss bands -5%, -7.5%, -10%, -15%, -20%, -30%;
- first crossing and executable quote for profit bands +5%, +10%, +20%, +30%, +50%, +100%;
- shadow trailing-stop policies after positive excursions;
- shadow partial-exit policies such as 25/25/50 and 50/25/25;
- post-2s continuation path so fixed exit can be compared with runners;
- buyer-cluster/co-occurrence IDs and cluster-adjusted reputation;
- E4 same-mint participation as benchmark metadata only, never as an entry input.

This will allow every future loss to be classified as one or more of: selection error, correlated-reputation error, latency error, adverse excursion that could have been stopped, unavoidable gap/slippage, or exit-management error. Every winner can similarly be classified by captured-vs-available upside.

## Current forensic verdict

The first 11 trades do not mainly say “Golden selection is bad.” They say the current research harness is intentionally primitive after entry.

The largest immediate findings are:

1. 100% fixed 2-second exits create almost no payoff asymmetry in this sample: average winner +20.95% vs average loser -20.65%.
2. Two large losses account for about 80% of gross losses, making loss containment potentially very valuable.
3. The live entry occurs ~21 ms after CREATE even though historical labels are based on a 10 ms paper entry; this latency delta needs explicit attribution.
4. Repeated correlated buyer clusters are visible on both winning and losing trades, and naïve summed reputation may overstate independent evidence.
5. The old telemetry cannot answer MFE/MAE or post-2s upside, so adaptive exit claims should not be made until path telemetry is collected prospectively.

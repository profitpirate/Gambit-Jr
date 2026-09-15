# Golden Thesis — horizon + execution-cost tournament

## Purpose
Test whether extending the two leading Golden exit policies beyond 2 seconds improves or damages profitability on an unseen, true-online cohort of freshly observed Solana launches.

## Frozen candidates
The archived V2_CURRENT and FLOW_V2_1 models are excluded from this experiment. The two retained candidate families are:

1. FULL — no structural initial; flatten 100% at the specified horizon.
2. PARTIALS — preserve the proven structural initial policy, then flatten all remaining inventory at the specified horizon.

## Matched variants
Every accepted Golden signal must be delivered atomically to every arm on the same live market path.

FULL_2S, FULL_3S, FULL_4S, FULL_5S, FULL_7S, FULL_10S
PARTIALS_2S, PARTIALS_3S, PARTIALS_4S, PARTIALS_5S, PARTIALS_7S, PARTIALS_10S

Each arm starts with an independent 3.0 SOL paper bankroll and compounds independently. Entry selection and conviction/sizing are frozen; only exit horizon/family may differ.

## Market-data requirement
No historical replay. Consume freshly arriving Solana launch/trade state in real time. If Axiom-authoritative market data or an authenticated Axiom feed is available in the runtime, record it separately. Do not claim an Axiom fill unless an Axiom order actually executes. This experiment must not broadcast real-money transactions.

## Realistic execution-cost model
Paper P&L must report both gross theoretical P&L and net executable-model P&L. The net model must explicitly debit, per attempted transaction where applicable:
- Solana base/network transaction fee;
- configured priority fee / compute-unit price;
- configured builder/Jito/Axiom-style tip or routing fee where applicable;
- platform fee if the venue/route charges one;
- buy and sell price impact/slippage using the live quote/reserve state at the execution timestamp rather than mid-price marking;
- failed/dropped/expired transaction-attempt costs when the failure class would still consume a fee/tip;
- partial-exit transaction costs independently for each sell leg.

Never silently invent a fee. Every cost source must be labelled either OBSERVED, CONFIGURED_FROM_DOCUMENTED_ROUTE, or SENSITIVITY_ASSUMPTION. If exact Axiom account/preset fees cannot be observed, report a sensitivity grid instead of calling one assumption 'actual'.

## Execution telemetry
For every entry and exit attempt record: signal/decision timestamp, quote timestamp, build/sign/dispatch-model timestamp, modelled acknowledgement timestamp, fill-state timestamp, latency components, requested SOL/tokens, filled SOL/tokens, slippage/price impact, each fee component, success/failure reason, retries, and duplicate protection. Paper transport benchmarks must not be labelled on-chain confirmation latency.

## Completion gate
At least 100 aligned CLOSED Golden signals for every one of the 12 arms, all post-exit observations complete, no unresolved exposure, no task errors that invalidate alignment, and final reports persisted.

## Required final analysis
For every arm: 100-trade W/L and WR, gross P&L, total costs by category, net P&L, ending 3-SOL bankroll, ROI, PF, expectancy, max drawdown, execution latency distribution, failed transaction attempts, average/median winner and loser, payoff ratio, MFE/MAE, concentration of P&L, tier/sizing performance, and cost sensitivity.

For every coin/arm: entry reason, conviction/tier, stake, entry quote/fill model, every partial, exit reason/time, gross/net trade P&L, all costs, MFE/MAE, post-exit path, whether price subsequently traded above/below the exit, and hindsight analysis clearly separated from executable evidence.

## Research conclusion
The report must compare exit horizons within FULL and PARTIALS families, determine whether added holding time improved gross and net economics on this sample, quantify the marginal effect of each extra second/horizon, identify whether gains came from a few outliers, and state limitations. Do not select a production candidate solely by highest sample P&L; preserve the evidence for the next brutal multi-day stress/forward-live phase.

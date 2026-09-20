# V12 Pre-Armed Axiom-costed paper-live test

Status: **PAPER_LIVE_GOLDEN_GATE_PASSED**

This is an untouched forward paper test. It does not place real trades or modify production V12.

## Progress

- Closed trades: 50 / 50
- Wins: 35
- Win rate: 70.00%
- Wilson lower bound: 56.25%
- Net PnL: +2.086015102 SOL
- Ending bankroll: 5.086015102 SOL
- Profit factor: 6.0063
- Maximum closed-equity drawdown: 2.94%
- Capture windows: 16

## Execution costs included

- Axiom net trading fee: 0.95% each side
- Pump bonding-curve fee: 1.25% each side
- Priority fee: 0.001000 SOL per transaction
- Buy MEV bribe: 0.001000 SOL
- Solana base fee: 0.000005 SOL per transaction
- Total Axiom fees charged: 0.408768428 SOL
- Total Pump fees charged: 0.537853195 SOL
- Total fixed execution costs charged: 0.188690000 SOL

## Integrity

- Frozen model SHA-256: `4792187bfb7cd47f2708dc9fbac247faab9650e103005a57782bdc1ad8aaa7d0`
- Corrected full replay workflow: 35507467607
- Corrected replay artifact: 10604247755
- Impossible pre-fill exits after replay: 0
- Performance metrics changed by chronology correction: none
- The test stops at the first 50 chronological closed trades.
- The selector, position sizing, exit policy, latency, and cost model cannot change mid-test.
- Social timestamps come from immutable launch metadata and must predate CREATE by 0-10 seconds.
- A real-time X transport/fill claim is not made by this paper test.
- Production paths changed: zero.

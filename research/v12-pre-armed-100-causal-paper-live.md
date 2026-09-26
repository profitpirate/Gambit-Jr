# V12 Pre-Armed Axiom-costed paper-live test

Status: **PAPER_LIVE_SAMPLE_COMPLETE_GATE_FAILED**

This is an untouched forward paper test. It does not place real trades or modify production V12.

## Progress

- Closed trades: 100 / 100
- Wins: 59
- Win rate: 59.00%
- Wilson lower bound: 49.20%
- Net PnL: +4.348315557 SOL
- Ending bankroll: 7.348315557 SOL
- Profit factor: 3.0115
- Maximum closed-equity drawdown: 17.23%
- Capture windows: 45

## Execution costs included

- Axiom net trading fee: 0.95% each side
- Pump bonding-curve fee: 1.25% each side
- Priority fee: 0.001000 SOL per transaction
- Buy MEV bribe: 0.001000 SOL
- Solana base fee: 0.000005 SOL per transaction
- Total Axiom fees charged: 1.024878714 SOL
- Total Pump fees charged: 1.348524624 SOL
- Total fixed execution costs charged: 0.358285000 SOL

## Integrity

- Frozen model SHA-256: `69df86eaf386fd37d699928a95d25aaeb2053e743b59c9e687c8a7c49d14f977`
- The test stops at the first 100 chronological closed trades.
- The selector, position sizing, exit policy, latency, and cost model cannot change mid-test.
- Social timestamps come from immutable launch metadata and must predate CREATE by 0-10 seconds.
- A real-time X transport/fill claim is not made by this paper test.
- Production paths changed: zero.

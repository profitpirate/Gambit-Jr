# V12 Pre-Armed Axiom-costed paper-live test

Status: **COLLECTING_UNTOUCHED_FORWARD_TRADES**

This is an untouched forward paper test. It does not place real trades or modify production V12.

## Progress

- Closed trades: 65 / 100
- Wins: 37
- Win rate: 56.92%
- Wilson lower bound: 44.83%
- Net PnL: +2.835837023 SOL
- Ending bankroll: 5.835837023 SOL
- Profit factor: 3.3631
- Maximum closed-equity drawdown: 8.59%
- Capture windows: 31

## Execution costs included

- Axiom net trading fee: 0.95% each side
- Pump bonding-curve fee: 1.25% each side
- Priority fee: 0.001000 SOL per transaction
- Buy MEV bribe: 0.001000 SOL
- Solana base fee: 0.000005 SOL per transaction
- Total Axiom fees charged: 0.581017678 SOL
- Total Pump fees charged: 0.764496945 SOL
- Total fixed execution costs charged: 0.229820000 SOL

## Integrity

- Frozen model SHA-256: `69df86eaf386fd37d699928a95d25aaeb2053e743b59c9e687c8a7c49d14f977`
- The test stops at the first 100 chronological closed trades.
- The selector, position sizing, exit policy, latency, and cost model cannot change mid-test.
- Social timestamps come from immutable launch metadata and must predate CREATE by 0-10 seconds.
- A real-time X transport/fill claim is not made by this paper test.
- Production paths changed: zero.

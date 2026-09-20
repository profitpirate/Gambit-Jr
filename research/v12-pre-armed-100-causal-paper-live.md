# V12 Pre-Armed Axiom-costed paper-live test

Status: **COLLECTING_UNTOUCHED_FORWARD_TRADES**

This is an untouched forward paper test. It does not place real trades or modify production V12.

## Progress

- Closed trades: 1 / 100
- Wins: 1
- Win rate: 100.00%
- Wilson lower bound: 20.65%
- Net PnL: +0.036793070 SOL
- Ending bankroll: 3.036793070 SOL
- Profit factor: 36793069727.3660
- Maximum closed-equity drawdown: 0.00%
- Capture windows: 1

## Execution costs included

- Axiom net trading fee: 0.95% each side
- Pump bonding-curve fee: 1.25% each side
- Priority fee: 0.001000 SOL per transaction
- Buy MEV bribe: 0.001000 SOL
- Solana base fee: 0.000005 SOL per transaction
- Total Axiom fees charged: 0.006061044 SOL
- Total Pump fees charged: 0.007975058 SOL
- Total fixed execution costs charged: 0.004015000 SOL

## Integrity

- Frozen model SHA-256: `69df86eaf386fd37d699928a95d25aaeb2053e743b59c9e687c8a7c49d14f977`
- The test stops at the first 100 chronological closed trades.
- The selector, position sizing, exit policy, latency, and cost model cannot change mid-test.
- Social timestamps come from immutable launch metadata and must predate CREATE by 0-10 seconds.
- A real-time X transport/fill claim is not made by this paper test.
- Production paths changed: zero.

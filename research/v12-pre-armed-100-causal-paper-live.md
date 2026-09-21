# V12 Pre-Armed Axiom-costed paper-live test

Status: **COLLECTING_UNTOUCHED_FORWARD_TRADES**

This is an untouched forward paper test. It does not place real trades or modify production V12.

## Progress

- Closed trades: 25 / 100
- Wins: 17
- Win rate: 68.00%
- Wilson lower bound: 48.41%
- Net PnL: +1.208834565 SOL
- Ending bankroll: 4.208834565 SOL
- Profit factor: 5.3401
- Maximum closed-equity drawdown: 3.14%
- Capture windows: 12

## Execution costs included

- Axiom net trading fee: 0.95% each side
- Pump bonding-curve fee: 1.25% each side
- Priority fee: 0.001000 SOL per transaction
- Buy MEV bribe: 0.001000 SOL
- Solana base fee: 0.000005 SOL per transaction
- Total Axiom fees charged: 0.181457802 SOL
- Total Pump fees charged: 0.238760266 SOL
- Total fixed execution costs charged: 0.090325000 SOL

## Integrity

- Frozen model SHA-256: `69df86eaf386fd37d699928a95d25aaeb2053e743b59c9e687c8a7c49d14f977`
- The test stops at the first 100 chronological closed trades.
- The selector, position sizing, exit policy, latency, and cost model cannot change mid-test.
- Social timestamps come from immutable launch metadata and must predate CREATE by 0-10 seconds.
- A real-time X transport/fill claim is not made by this paper test.
- Production paths changed: zero.

# E4 V12 pre-armed Axiom-costed paper-live test

Status: **COLLECTING_UNTOUCHED_FORWARD_TRADES**

This is an untouched forward paper test. It does not place real trades or modify production V12.

## Progress

- Closed trades: 23 / 50
- Wins: 17
- Win rate: 73.91%
- Wilson lower bound: 53.53%
- Net PnL: +1.179708011 SOL
- Ending bankroll: 4.179708011 SOL
- Profit factor: 12.9822
- Maximum closed-equity drawdown: 1.19%
- Capture windows: 7

## Execution costs included

- Axiom net trading fee: 0.95% each side
- Pump bonding-curve fee: 1.25% each side
- Priority fee: 0.001000 SOL per transaction
- Buy MEV bribe: 0.001000 SOL
- Solana base fee: 0.000005 SOL per transaction
- Total Axiom fees charged: 0.165046355 SOL
- Total Pump fees charged: 0.217166257 SOL
- Total fixed execution costs charged: 0.089330000 SOL

## Integrity

- Frozen model SHA-256: `4792187bfb7cd47f2708dc9fbac247faab9650e103005a57782bdc1ad8aaa7d0`
- The test stops at the first 50 chronological closed trades.
- The selector, position sizing, exit policy, latency, and cost model cannot change mid-test.
- Social timestamps come from immutable launch metadata and must predate CREATE by 0-10 seconds.
- A real-time X transport/fill claim is not made by this paper test.
- Production paths changed: zero.

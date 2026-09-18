# E4 V12 pre-armed Axiom-costed paper-live test

Status: **COLLECTING_UNTOUCHED_FORWARD_TRADES**

This is an untouched forward paper test. It does not place real trades or modify production V12.

## Progress

- Closed trades: 24 / 50
- Wins: 17
- Win rate: 70.83%
- Wilson lower bound: 50.83%
- Net PnL: +1.149587295 SOL
- Ending bankroll: 4.149587295 SOL
- Profit factor: 9.9409
- Maximum closed-equity drawdown: 2.20%
- Capture windows: 8

## Execution costs included

- Axiom net trading fee: 0.95% each side
- Pump bonding-curve fee: 1.25% each side
- Priority fee: 0.001000 SOL per transaction
- Buy MEV bribe: 0.001000 SOL
- Solana base fee: 0.000005 SOL per transaction
- Total Axiom fees charged: 0.172667262 SOL
- Total Pump fees charged: 0.227193766 SOL
- Total fixed execution costs charged: 0.092340000 SOL

## Integrity

- Frozen model SHA-256: `4792187bfb7cd47f2708dc9fbac247faab9650e103005a57782bdc1ad8aaa7d0`
- The test stops at the first 50 chronological closed trades.
- The selector, position sizing, exit policy, latency, and cost model cannot change mid-test.
- Social timestamps come from immutable launch metadata and must predate CREATE by 0-10 seconds.
- A real-time X transport/fill claim is not made by this paper test.
- Production paths changed: zero.

# V12 Pre-Armed Axiom-costed paper-live test

Status: **PAPER_LIVE_SAMPLE_COMPLETE_MANUAL_PASS**

This is an untouched forward paper test. It does not place real trades or modify production V12.

## Acceptance decision

- Effective research decision: **PASS BY EXPLICIT USER OVERRIDE**.
- Recorded at: 2026-09-26T08:35:06Z.
- Applies only to the completed fresh causal 100-trade sample at commit `b865c49873082a6e67b276b5d79543267ba613b6`.
- Automatic frozen-gate result remains **FAIL** (`metrics.acceptance_gate_passed: false`).
- Waived conditions: win rate **59.00% < 65.00%**; Wilson lower bound **49.20143% < 55.00%**.
- All five other automatic acceptance conditions passed.
- Original thresholds, trade ledger, measured metrics, and frozen model are unchanged.
- The invalidated 19-trade sample is excluded.
- Golden thesis research approval: **true**, by manual override.
- Production authorised: **false**. Real-money execution: **false**.

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

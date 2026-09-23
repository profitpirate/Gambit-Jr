# V12 Pre-Armed shadow intelligence

Status: **PASS**

This sidecar never changes V12 Pre-Armed selection, sizing, execution guards or exits.

## Creator concentration

- Creators observed: 9
- Effective creator count: 3.61
- Top creator trade share: 47.83%
- Top 3 trade share: 71.74%

## Drift

- Drift status: RED
- Flags: RECENT_10_NEGATIVE_EXPECTANCY, WIN_RATE_DOWN_GT_15PP

## Bankroll risk simulation

- Source trade samples: 46
- 20% DD probability over 200 trades: 0.02%
- Bankroll <= 1 SOL probability: 0.00%

## New creator shadow

- Decisions scored: 843
- Shadow eligible: 0

## Execution stress

- 100ms_fee_1.0x: 36/46 filled, PnL -0.2851 SOL, PF 0.67
- 100ms_fee_1.5x: 36/46 filled, PnL -0.6610 SOL, PF 0.39
- 100ms_fee_2.0x: 36/46 filled, PnL -1.0289 SOL, PF 0.22
- 10ms_fee_1.0x: 44/46 filled, PnL +1.0622 SOL, PF 2.30
- 10ms_fee_1.5x: 44/46 filled, PnL +0.5724 SOL, PF 1.56
- 10ms_fee_2.0x: 44/46 filled, PnL +0.0930 SOL, PF 1.08
- 25ms_fee_1.0x: 38/46 filled, PnL +0.1056 SOL, PF 1.12
- 25ms_fee_1.5x: 38/46 filled, PnL -0.3013 SOL, PF 0.73
- 25ms_fee_2.0x: 38/46 filled, PnL -0.6996 SOL, PF 0.47
- 50ms_fee_1.0x: 36/46 filled, PnL -0.2156 SOL, PF 0.75
- 50ms_fee_1.5x: 36/46 filled, PnL -0.5937 SOL, PF 0.46
- 50ms_fee_2.0x: 36/46 filled, PnL -0.9638 SOL, PF 0.27
- 5ms_fee_1.0x: 46/46 filled, PnL +1.8649 SOL, PF 3.29
- 5ms_fee_1.5x: 46/46 filled, PnL +1.3323 SOL, PF 2.30
- 5ms_fee_2.0x: 46/46 filled, PnL +0.8111 SOL, PF 1.65

## Integrity

- Reliability status: PASS
- Guardrail state: HALT
- Guardrail halts: MODEL_DRIFT_RED
- Guardrail warnings: 50MS_EXECUTION_STRESS_NET_NEGATIVE, HIGH_RUNTIME_DEPENDENCY_ADVISORIES_8
- Errors: none
- Warnings: none
- Processed shadow windows: 27

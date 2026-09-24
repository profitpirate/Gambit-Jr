# V12 Pre-Armed shadow intelligence

Status: **PASS**

This sidecar never changes V12 Pre-Armed selection, sizing, execution guards or exits.

## Creator concentration

- Creators observed: 11
- Effective creator count: 3.38
- Top creator trade share: 50.75%
- Top 3 trade share: 73.13%

## Drift

- Drift status: YELLOW
- Flags: TOP_CREATOR_OVER_50_PERCENT

## Bankroll risk simulation

- Source trade samples: 67
- 20% DD probability over 200 trades: 0.02%
- Bankroll <= 1 SOL probability: 0.00%

## New creator shadow

- Decisions scored: 1251
- Shadow eligible: 0

## Execution stress

- 100ms_fee_1.0x: 52/67 filled, PnL -0.0922 SOL, PF 0.94
- 100ms_fee_1.5x: 52/67 filled, PnL -0.6879 SOL, PF 0.62
- 100ms_fee_2.0x: 52/67 filled, PnL -1.2710 SOL, PF 0.42
- 10ms_fee_1.0x: 64/67 filled, PnL +1.8414 SOL, PF 2.52
- 10ms_fee_1.5x: 64/67 filled, PnL +1.0630 SOL, PF 1.70
- 10ms_fee_2.0x: 64/67 filled, PnL +0.3011 SOL, PF 1.16
- 25ms_fee_1.0x: 56/67 filled, PnL +0.6621 SOL, PF 1.50
- 25ms_fee_1.5x: 56/67 filled, PnL -0.0011 SOL, PF 1.00
- 25ms_fee_2.0x: 56/67 filled, PnL -0.6501 SOL, PF 0.67
- 50ms_fee_1.0x: 53/67 filled, PnL -0.0334 SOL, PF 0.98
- 50ms_fee_1.5x: 53/67 filled, PnL -0.6452 SOL, PF 0.65
- 50ms_fee_2.0x: 53/67 filled, PnL -1.2440 SOL, PF 0.44
- 5ms_fee_1.0x: 67/67 filled, PnL +2.8640 SOL, PF 3.36
- 5ms_fee_1.5x: 67/67 filled, PnL +2.0246 SOL, PF 2.32
- 5ms_fee_2.0x: 67/67 filled, PnL +1.2031 SOL, PF 1.63

## Integrity

- Reliability status: PASS
- Guardrail state: WATCH
- Guardrail halts: none
- Guardrail warnings: 50MS_EXECUTION_STRESS_NET_NEGATIVE, HIGH_RUNTIME_DEPENDENCY_ADVISORIES_8, MODEL_DRIFT_YELLOW
- Errors: none
- Warnings: none
- Processed shadow windows: 42

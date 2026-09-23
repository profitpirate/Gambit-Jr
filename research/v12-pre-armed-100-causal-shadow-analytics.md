# V12 Pre-Armed shadow intelligence

Status: **PASS**

This sidecar never changes V12 Pre-Armed selection, sizing, execution guards or exits.

## Creator concentration

- Creators observed: 9
- Effective creator count: 3.21
- Top creator trade share: 51.79%
- Top 3 trade share: 76.79%

## Drift

- Drift status: YELLOW
- Flags: TOP_CREATOR_OVER_50_PERCENT

## Bankroll risk simulation

- Source trade samples: 56
- 20% DD probability over 200 trades: 0.01%
- Bankroll <= 1 SOL probability: 0.00%

## New creator shadow

- Decisions scored: 1060
- Shadow eligible: 0

## Execution stress

- 100ms_fee_1.0x: 44/56 filled, PnL -0.0255 SOL, PF 0.98
- 100ms_fee_1.5x: 44/56 filled, PnL -0.5126 SOL, PF 0.64
- 100ms_fee_2.0x: 44/56 filled, PnL -0.9893 SOL, PF 0.42
- 10ms_fee_1.0x: 53/56 filled, PnL +1.4628 SOL, PF 2.42
- 10ms_fee_1.5x: 53/56 filled, PnL +0.8454 SOL, PF 1.67
- 10ms_fee_2.0x: 53/56 filled, PnL +0.2412 SOL, PF 1.16
- 25ms_fee_1.0x: 46/56 filled, PnL +0.4568 SOL, PF 1.41
- 25ms_fee_1.5x: 46/56 filled, PnL -0.0640 SOL, PF 0.95
- 25ms_fee_2.0x: 46/56 filled, PnL -0.5737 SOL, PF 0.65
- 50ms_fee_1.0x: 44/56 filled, PnL -0.0093 SOL, PF 0.99
- 50ms_fee_1.5x: 44/56 filled, PnL -0.4974 SOL, PF 0.67
- 50ms_fee_2.0x: 44/56 filled, PnL -0.9751 SOL, PF 0.45
- 5ms_fee_1.0x: 56/56 filled, PnL +2.2900 SOL, PF 3.20
- 5ms_fee_1.5x: 56/56 filled, PnL +1.6162 SOL, PF 2.25
- 5ms_fee_2.0x: 56/56 filled, PnL +0.9567 SOL, PF 1.61

## Integrity

- Reliability status: PASS
- Guardrail state: WATCH
- Guardrail halts: none
- Guardrail warnings: 50MS_EXECUTION_STRESS_NET_NEGATIVE, HIGH_RUNTIME_DEPENDENCY_ADVISORIES_8, MODEL_DRIFT_YELLOW
- Errors: none
- Warnings: none
- Processed shadow windows: 34

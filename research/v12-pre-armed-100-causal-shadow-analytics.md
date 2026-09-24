# V12 Pre-Armed shadow intelligence

Status: **PASS**

This sidecar never changes V12 Pre-Armed selection, sizing, execution guards or exits.

## Creator concentration

- Creators observed: 12
- Effective creator count: 3.29
- Top creator trade share: 51.35%
- Top 3 trade share: 74.32%

## Drift

- Drift status: YELLOW
- Flags: TOP_CREATOR_OVER_50_PERCENT

## Bankroll risk simulation

- Source trade samples: 74
- 20% DD probability over 200 trades: 0.01%
- Bankroll <= 1 SOL probability: 0.00%

## New creator shadow

- Decisions scored: 1326
- Shadow eligible: 0

## Execution stress

- 100ms_fee_1.0x: 57/74 filled, PnL -0.3033 SOL, PF 0.83
- 100ms_fee_1.5x: 57/74 filled, PnL -0.9663 SOL, PF 0.55
- 100ms_fee_2.0x: 57/74 filled, PnL -1.6153 SOL, PF 0.36
- 10ms_fee_1.0x: 70/74 filled, PnL +2.0452 SOL, PF 2.59
- 10ms_fee_1.5x: 70/74 filled, PnL +1.1743 SOL, PF 1.73
- 10ms_fee_2.0x: 70/74 filled, PnL +0.3218 SOL, PF 1.16
- 25ms_fee_1.0x: 62/74 filled, PnL +0.5535 SOL, PF 1.36
- 25ms_fee_1.5x: 62/74 filled, PnL -0.1939 SOL, PF 0.90
- 25ms_fee_2.0x: 62/74 filled, PnL -0.9255 SOL, PF 0.60
- 50ms_fee_1.0x: 58/74 filled, PnL -0.2332 SOL, PF 0.87
- 50ms_fee_1.5x: 58/74 filled, PnL -0.9125 SOL, PF 0.57
- 50ms_fee_2.0x: 58/74 filled, PnL -1.5774 SOL, PF 0.38
- 5ms_fee_1.0x: 74/74 filled, PnL +3.5518 SOL, PF 3.87
- 5ms_fee_1.5x: 74/74 filled, PnL +2.5935 SOL, PF 2.65
- 5ms_fee_2.0x: 74/74 filled, PnL +1.6556 SOL, PF 1.84

## Integrity

- Reliability status: PASS
- Guardrail state: WATCH
- Guardrail halts: none
- Guardrail warnings: 50MS_EXECUTION_STRESS_NET_NEGATIVE, HIGH_RUNTIME_DEPENDENCY_ADVISORIES_8, MODEL_DRIFT_YELLOW
- Errors: none
- Warnings: none
- Processed shadow windows: 44

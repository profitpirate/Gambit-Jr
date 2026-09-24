# V12 Pre-Armed shadow intelligence

Status: **PASS**

This sidecar never changes V12 Pre-Armed selection, sizing, execution guards or exits.

## Creator concentration

- Creators observed: 11
- Effective creator count: 3.35
- Top creator trade share: 50.79%
- Top 3 trade share: 74.60%

## Drift

- Drift status: YELLOW
- Flags: TOP_CREATOR_OVER_50_PERCENT

## Bankroll risk simulation

- Source trade samples: 63
- 20% DD probability over 200 trades: 0.00%
- Bankroll <= 1 SOL probability: 0.00%

## New creator shadow

- Decisions scored: 1156
- Shadow eligible: 0

## Execution stress

- 100ms_fee_1.0x: 49/63 filled, PnL -0.0368 SOL, PF 0.97
- 100ms_fee_1.5x: 49/63 filled, PnL -0.5916 SOL, PF 0.65
- 100ms_fee_2.0x: 49/63 filled, PnL -1.1346 SOL, PF 0.44
- 10ms_fee_1.0x: 60/63 filled, PnL +1.7091 SOL, PF 2.42
- 10ms_fee_1.5x: 60/63 filled, PnL +0.9906 SOL, PF 1.67
- 10ms_fee_2.0x: 60/63 filled, PnL +0.2874 SOL, PF 1.16
- 25ms_fee_1.0x: 52/63 filled, PnL +0.6102 SOL, PF 1.47
- 25ms_fee_1.5x: 52/63 filled, PnL +0.0045 SOL, PF 1.00
- 25ms_fee_2.0x: 52/63 filled, PnL -0.5883 SOL, PF 0.69
- 50ms_fee_1.0x: 49/63 filled, PnL -0.0089 SOL, PF 0.99
- 50ms_fee_1.5x: 49/63 filled, PnL -0.5650 SOL, PF 0.68
- 50ms_fee_2.0x: 49/63 filled, PnL -1.1092 SOL, PF 0.46
- 5ms_fee_1.0x: 63/63 filled, PnL +2.6713 SOL, PF 3.23
- 5ms_fee_1.5x: 63/63 filled, PnL +1.8932 SOL, PF 2.27
- 5ms_fee_2.0x: 63/63 filled, PnL +1.1316 SOL, PF 1.62

## Integrity

- Reliability status: PASS
- Guardrail state: WATCH
- Guardrail halts: none
- Guardrail warnings: 50MS_EXECUTION_STRESS_NET_NEGATIVE, HIGH_RUNTIME_DEPENDENCY_ADVISORIES_8, MODEL_DRIFT_YELLOW
- Errors: none
- Warnings: none
- Processed shadow windows: 37

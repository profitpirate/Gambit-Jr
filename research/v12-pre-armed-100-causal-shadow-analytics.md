# V12 Pre-Armed shadow intelligence

Status: **PASS**

This sidecar never changes V12 Pre-Armed selection, sizing, execution guards or exits.

## Creator concentration

- Creators observed: 10
- Effective creator count: 3.33
- Top creator trade share: 50.88%
- Top 3 trade share: 75.44%

## Drift

- Drift status: YELLOW
- Flags: TOP_CREATOR_OVER_50_PERCENT

## Bankroll risk simulation

- Source trade samples: 57
- 20% DD probability over 200 trades: 0.01%
- Bankroll <= 1 SOL probability: 0.00%

## New creator shadow

- Decisions scored: 1090
- Shadow eligible: 0

## Execution stress

- 100ms_fee_1.0x: 45/57 filled, PnL -0.0418 SOL, PF 0.96
- 100ms_fee_1.5x: 45/57 filled, PnL -0.5413 SOL, PF 0.63
- 100ms_fee_2.0x: 45/57 filled, PnL -1.0303 SOL, PF 0.41
- 10ms_fee_1.0x: 54/57 filled, PnL +1.4465 SOL, PF 2.38
- 10ms_fee_1.5x: 54/57 filled, PnL +0.8167 SOL, PF 1.63
- 10ms_fee_2.0x: 54/57 filled, PnL +0.2003 SOL, PF 1.13
- 25ms_fee_1.0x: 47/57 filled, PnL +0.4405 SOL, PF 1.39
- 25ms_fee_1.5x: 47/57 filled, PnL -0.0927 SOL, PF 0.93
- 25ms_fee_2.0x: 47/57 filled, PnL -0.6146 SOL, PF 0.63
- 50ms_fee_1.0x: 45/57 filled, PnL -0.0256 SOL, PF 0.98
- 50ms_fee_1.5x: 45/57 filled, PnL -0.5261 SOL, PF 0.65
- 50ms_fee_2.0x: 45/57 filled, PnL -1.0160 SOL, PF 0.44
- 5ms_fee_1.0x: 57/57 filled, PnL +2.2737 SOL, PF 3.15
- 5ms_fee_1.5x: 57/57 filled, PnL +1.5874 SOL, PF 2.21
- 5ms_fee_2.0x: 57/57 filled, PnL +0.9158 SOL, PF 1.57

## Integrity

- Reliability status: PASS
- Guardrail state: WATCH
- Guardrail halts: none
- Guardrail warnings: 50MS_EXECUTION_STRESS_NET_NEGATIVE, HIGH_RUNTIME_DEPENDENCY_ADVISORIES_8, MODEL_DRIFT_YELLOW
- Errors: none
- Warnings: none
- Processed shadow windows: 35

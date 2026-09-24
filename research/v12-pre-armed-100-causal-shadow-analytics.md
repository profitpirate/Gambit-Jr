# V12 Pre-Armed shadow intelligence

Status: **PASS**

This sidecar never changes V12 Pre-Armed selection, sizing, execution guards or exits.

## Creator concentration

- Creators observed: 11
- Effective creator count: 3.29
- Top creator trade share: 51.52%
- Top 3 trade share: 74.24%

## Drift

- Drift status: YELLOW
- Flags: TOP_CREATOR_OVER_50_PERCENT

## Bankroll risk simulation

- Source trade samples: 66
- 20% DD probability over 200 trades: 0.03%
- Bankroll <= 1 SOL probability: 0.00%

## New creator shadow

- Decisions scored: 1183
- Shadow eligible: 0

## Execution stress

- 100ms_fee_1.0x: 52/66 filled, PnL -0.0922 SOL, PF 0.94
- 100ms_fee_1.5x: 52/66 filled, PnL -0.6879 SOL, PF 0.62
- 100ms_fee_2.0x: 52/66 filled, PnL -1.2710 SOL, PF 0.42
- 10ms_fee_1.0x: 63/66 filled, PnL +1.8532 SOL, PF 2.54
- 10ms_fee_1.5x: 63/66 filled, PnL +1.0886 SOL, PF 1.73
- 10ms_fee_2.0x: 63/66 filled, PnL +0.3402 SOL, PF 1.19
- 25ms_fee_1.0x: 55/66 filled, PnL +0.6739 SOL, PF 1.51
- 25ms_fee_1.5x: 55/66 filled, PnL +0.0246 SOL, PF 1.02
- 25ms_fee_2.0x: 55/66 filled, PnL -0.6110 SOL, PF 0.69
- 50ms_fee_1.0x: 52/66 filled, PnL -0.0216 SOL, PF 0.99
- 50ms_fee_1.5x: 52/66 filled, PnL -0.6196 SOL, PF 0.66
- 50ms_fee_2.0x: 52/66 filled, PnL -1.2049 SOL, PF 0.45
- 5ms_fee_1.0x: 66/66 filled, PnL +2.8758 SOL, PF 3.40
- 5ms_fee_1.5x: 66/66 filled, PnL +2.0503 SOL, PF 2.36
- 5ms_fee_2.0x: 66/66 filled, PnL +1.2423 SOL, PF 1.67

## Integrity

- Reliability status: PASS
- Guardrail state: WATCH
- Guardrail halts: none
- Guardrail warnings: 50MS_EXECUTION_STRESS_NET_NEGATIVE, HIGH_RUNTIME_DEPENDENCY_ADVISORIES_8, MODEL_DRIFT_YELLOW
- Errors: none
- Warnings: none
- Processed shadow windows: 39

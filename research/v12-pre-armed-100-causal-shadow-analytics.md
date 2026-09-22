# V12 Pre-Armed shadow intelligence

Status: **PASS**

This sidecar never changes V12 Pre-Armed selection, sizing, execution guards or exits.

## Creator concentration

- Creators observed: 9
- Effective creator count: 3.37
- Top creator trade share: 51.22%
- Top 3 trade share: 70.73%

## Drift

- Drift status: YELLOW
- Flags: TOP_CREATOR_OVER_50_PERCENT

## Bankroll risk simulation

- Source trade samples: 41
- 20% DD probability over 200 trades: 0.01%
- Bankroll <= 1 SOL probability: 0.00%

## New creator shadow

- Decisions scored: 732
- Shadow eligible: 0

## Execution stress

- 100ms_fee_1.0x: 32/41 filled, PnL -0.0907 SOL, PF 0.87
- 100ms_fee_1.5x: 32/41 filled, PnL -0.4235 SOL, PF 0.50
- 100ms_fee_2.0x: 32/41 filled, PnL -0.7492 SOL, PF 0.28
- 10ms_fee_1.0x: 40/41 filled, PnL +1.1410 SOL, PF 2.73
- 10ms_fee_1.5x: 40/41 filled, PnL +0.6972 SOL, PF 1.84
- 10ms_fee_2.0x: 40/41 filled, PnL +0.2629 SOL, PF 1.26
- 25ms_fee_1.0x: 34/41 filled, PnL +0.3000 SOL, PF 1.43
- 25ms_fee_1.5x: 34/41 filled, PnL -0.0638 SOL, PF 0.93
- 25ms_fee_2.0x: 34/41 filled, PnL -0.4199 SOL, PF 0.60
- 50ms_fee_1.0x: 32/41 filled, PnL -0.0212 SOL, PF 0.97
- 50ms_fee_1.5x: 32/41 filled, PnL -0.3562 SOL, PF 0.59
- 50ms_fee_2.0x: 32/41 filled, PnL -0.6841 SOL, PF 0.35
- 5ms_fee_1.0x: 41/41 filled, PnL +1.8950 SOL, PF 4.04
- 5ms_fee_1.5x: 41/41 filled, PnL +1.4216 SOL, PF 2.80
- 5ms_fee_2.0x: 41/41 filled, PnL +0.9584 SOL, PF 1.98

## Integrity

- Reliability status: PASS
- Guardrail state: WATCH
- Guardrail halts: none
- Guardrail warnings: 50MS_EXECUTION_STRESS_NET_NEGATIVE, HIGH_RUNTIME_DEPENDENCY_ADVISORIES_8, MODEL_DRIFT_YELLOW
- Errors: none
- Warnings: none
- Processed shadow windows: 23

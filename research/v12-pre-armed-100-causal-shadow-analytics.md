# V12 Pre-Armed shadow intelligence

Status: **PASS**

This sidecar never changes V12 Pre-Armed selection, sizing, execution guards or exits.

## Creator concentration

- Creators observed: 11
- Effective creator count: 3.36
- Top creator trade share: 50.72%
- Top 3 trade share: 73.91%

## Drift

- Drift status: YELLOW
- Flags: TOP_CREATOR_OVER_50_PERCENT

## Bankroll risk simulation

- Source trade samples: 69
- 20% DD probability over 200 trades: 0.03%
- Bankroll <= 1 SOL probability: 0.00%

## New creator shadow

- Decisions scored: 1291
- Shadow eligible: 0

## Execution stress

- 100ms_fee_1.0x: 53/69 filled, PnL -0.1699 SOL, PF 0.89
- 100ms_fee_1.5x: 53/69 filled, PnL -0.7779 SOL, PF 0.59
- 100ms_fee_2.0x: 53/69 filled, PnL -1.3731 SOL, PF 0.40
- 10ms_fee_1.0x: 66/69 filled, PnL +1.7847 SOL, PF 2.38
- 10ms_fee_1.5x: 66/69 filled, PnL +0.9789 SOL, PF 1.61
- 10ms_fee_2.0x: 66/69 filled, PnL +0.1903 SOL, PF 1.10
- 25ms_fee_1.0x: 58/69 filled, PnL +0.6055 SOL, PF 1.43
- 25ms_fee_1.5x: 58/69 filled, PnL -0.0851 SOL, PF 0.95
- 25ms_fee_2.0x: 58/69 filled, PnL -0.7610 SOL, PF 0.64
- 50ms_fee_1.0x: 54/69 filled, PnL -0.1112 SOL, PF 0.93
- 50ms_fee_1.5x: 54/69 filled, PnL -0.7352 SOL, PF 0.62
- 50ms_fee_2.0x: 54/69 filled, PnL -1.3461 SOL, PF 0.42
- 5ms_fee_1.0x: 69/69 filled, PnL +2.8572 SOL, PF 3.30
- 5ms_fee_1.5x: 69/69 filled, PnL +1.9893 SOL, PF 2.26
- 5ms_fee_2.0x: 69/69 filled, PnL +1.1399 SOL, PF 1.58

## Integrity

- Reliability status: PASS
- Guardrail state: WATCH
- Guardrail halts: none
- Guardrail warnings: 50MS_EXECUTION_STRESS_NET_NEGATIVE, HIGH_RUNTIME_DEPENDENCY_ADVISORIES_8, MODEL_DRIFT_YELLOW
- Errors: none
- Warnings: none
- Processed shadow windows: 43

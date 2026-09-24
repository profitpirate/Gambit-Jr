# V12 Pre-Armed shadow intelligence

Status: **PASS**

This sidecar never changes V12 Pre-Armed selection, sizing, execution guards or exits.

## Creator concentration

- Creators observed: 11
- Effective creator count: 3.36
- Top creator trade share: 50.77%
- Top 3 trade share: 73.85%

## Drift

- Drift status: YELLOW
- Flags: TOP_CREATOR_OVER_50_PERCENT

## Bankroll risk simulation

- Source trade samples: 65
- 20% DD probability over 200 trades: 0.02%
- Bankroll <= 1 SOL probability: 0.00%

## New creator shadow

- Decisions scored: 1170
- Shadow eligible: 0

## Execution stress

- 100ms_fee_1.0x: 51/65 filled, PnL -0.1322 SOL, PF 0.91
- 100ms_fee_1.5x: 51/65 filled, PnL -0.7125 SOL, PF 0.61
- 100ms_fee_2.0x: 51/65 filled, PnL -1.2804 SOL, PF 0.41
- 10ms_fee_1.0x: 62/65 filled, PnL +1.8132 SOL, PF 2.51
- 10ms_fee_1.5x: 62/65 filled, PnL +1.0641 SOL, PF 1.71
- 10ms_fee_2.0x: 62/65 filled, PnL +0.3309 SOL, PF 1.18
- 25ms_fee_1.0x: 54/65 filled, PnL +0.6339 SOL, PF 1.48
- 25ms_fee_1.5x: 54/65 filled, PnL +0.0000 SOL, PF 1.00
- 25ms_fee_2.0x: 54/65 filled, PnL -0.6204 SOL, PF 0.68
- 50ms_fee_1.0x: 51/65 filled, PnL -0.0616 SOL, PF 0.96
- 50ms_fee_1.5x: 51/65 filled, PnL -0.6441 SOL, PF 0.65
- 50ms_fee_2.0x: 51/65 filled, PnL -1.2143 SOL, PF 0.44
- 5ms_fee_1.0x: 65/65 filled, PnL +2.8358 SOL, PF 3.36
- 5ms_fee_1.5x: 65/65 filled, PnL +2.0258 SOL, PF 2.34
- 5ms_fee_2.0x: 65/65 filled, PnL +1.2329 SOL, PF 1.66

## Integrity

- Reliability status: PASS
- Guardrail state: WATCH
- Guardrail halts: none
- Guardrail warnings: 50MS_EXECUTION_STRESS_NET_NEGATIVE, HIGH_RUNTIME_DEPENDENCY_ADVISORIES_8, MODEL_DRIFT_YELLOW
- Errors: none
- Warnings: none
- Processed shadow windows: 38

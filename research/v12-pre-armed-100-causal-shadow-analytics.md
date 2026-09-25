# V12 Pre-Armed shadow intelligence

Status: **PASS**

This sidecar never changes V12 Pre-Armed selection, sizing, execution guards or exits.

## Creator concentration

- Creators observed: 13
- Effective creator count: 4.02
- Top creator trade share: 45.35%
- Top 3 trade share: 67.44%

## Drift

- Drift status: RED
- Flags: RECENT_10_NEGATIVE_EXPECTANCY

## Bankroll risk simulation

- Source trade samples: 86
- 20% DD probability over 200 trades: 0.05%
- Bankroll <= 1 SOL probability: 0.00%

## New creator shadow

- Decisions scored: 1535
- Shadow eligible: 0

## Execution stress

- 100ms_fee_1.0x: 66/86 filled, PnL -0.3017 SOL, PF 0.86
- 100ms_fee_1.5x: 66/86 filled, PnL -1.1073 SOL, PF 0.58
- 100ms_fee_2.0x: 66/86 filled, PnL -1.8958 SOL, PF 0.39
- 10ms_fee_1.0x: 81/86 filled, PnL +1.9527 SOL, PF 1.97
- 10ms_fee_1.5x: 81/86 filled, PnL +0.9087 SOL, PF 1.38
- 10ms_fee_2.0x: 81/86 filled, PnL -0.1132 SOL, PF 0.96
- 25ms_fee_1.0x: 72/86 filled, PnL +0.6325 SOL, PF 1.31
- 25ms_fee_1.5x: 72/86 filled, PnL -0.2755 SOL, PF 0.89
- 25ms_fee_2.0x: 72/86 filled, PnL -1.1644 SOL, PF 0.60
- 50ms_fee_1.0x: 68/86 filled, PnL -0.1542 SOL, PF 0.93
- 50ms_fee_1.5x: 68/86 filled, PnL -0.9941 SOL, PF 0.63
- 50ms_fee_2.0x: 68/86 filled, PnL -1.8163 SOL, PF 0.42
- 5ms_fee_1.0x: 86/86 filled, PnL +3.6794 SOL, PF 3.02
- 5ms_fee_1.5x: 86/86 filled, PnL +2.5269 SOL, PF 2.14
- 5ms_fee_2.0x: 86/86 filled, PnL +1.3989 SOL, PF 1.52

## Integrity

- Reliability status: PASS
- Guardrail state: HALT
- Guardrail halts: MODEL_DRIFT_RED
- Guardrail warnings: 50MS_EXECUTION_STRESS_NET_NEGATIVE, CLOSED_EQUITY_DRAWDOWN_OVER_12_PERCENT, HIGH_RUNTIME_DEPENDENCY_ADVISORIES_8
- Errors: none
- Warnings: none
- Processed shadow windows: 54

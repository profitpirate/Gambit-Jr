# V12 Pre-Armed shadow intelligence

Status: **PASS**

This sidecar never changes V12 Pre-Armed selection, sizing, execution guards or exits.

## Creator concentration

- Creators observed: 13
- Effective creator count: 4.10
- Top creator trade share: 44.44%
- Top 3 trade share: 68.89%

## Drift

- Drift status: RED
- Flags: RECENT_10_NEGATIVE_EXPECTANCY

## Bankroll risk simulation

- Source trade samples: 90
- 20% DD probability over 200 trades: 0.09%
- Bankroll <= 1 SOL probability: 0.00%

## New creator shadow

- Decisions scored: 1620
- Shadow eligible: 0

## Execution stress

- 100ms_fee_1.0x: 69/90 filled, PnL -0.5503 SOL, PF 0.78
- 100ms_fee_1.5x: 69/90 filled, PnL -1.3972 SOL, PF 0.53
- 100ms_fee_2.0x: 69/90 filled, PnL -2.2263 SOL, PF 0.35
- 10ms_fee_1.0x: 85/90 filled, PnL +1.8786 SOL, PF 1.87
- 10ms_fee_1.5x: 85/90 filled, PnL +0.7725 SOL, PF 1.30
- 10ms_fee_2.0x: 85/90 filled, PnL -0.3103 SOL, PF 0.90
- 25ms_fee_1.0x: 76/90 filled, PnL +0.5533 SOL, PF 1.25
- 25ms_fee_1.5x: 76/90 filled, PnL -0.4168 SOL, PF 0.84
- 25ms_fee_2.0x: 76/90 filled, PnL -1.3664 SOL, PF 0.57
- 50ms_fee_1.0x: 71/90 filled, PnL -0.4027 SOL, PF 0.84
- 50ms_fee_1.5x: 71/90 filled, PnL -1.2841 SOL, PF 0.57
- 50ms_fee_2.0x: 71/90 filled, PnL -2.1468 SOL, PF 0.38
- 5ms_fee_1.0x: 90/90 filled, PnL +3.6210 SOL, PF 2.85
- 5ms_fee_1.5x: 90/90 filled, PnL +2.4061 SOL, PF 2.01
- 5ms_fee_2.0x: 90/90 filled, PnL +1.2168 SOL, PF 1.42

## Integrity

- Reliability status: PASS
- Guardrail state: HALT
- Guardrail halts: MODEL_DRIFT_RED
- Guardrail warnings: 50MS_EXECUTION_STRESS_NET_NEGATIVE, CLOSED_EQUITY_DRAWDOWN_OVER_12_PERCENT, HIGH_RUNTIME_DEPENDENCY_ADVISORIES_8
- Errors: none
- Warnings: none
- Processed shadow windows: 57

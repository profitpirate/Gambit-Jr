# V12 Pre-Armed shadow intelligence

Status: **PASS**

This sidecar never changes V12 Pre-Armed selection, sizing, execution guards or exits.

## Creator concentration

- Creators observed: 13
- Effective creator count: 3.94
- Top creator trade share: 45.88%
- Top 3 trade share: 68.24%

## Drift

- Drift status: RED
- Flags: RECENT_10_NEGATIVE_EXPECTANCY

## Bankroll risk simulation

- Source trade samples: 85
- 20% DD probability over 200 trades: 0.06%
- Bankroll <= 1 SOL probability: 0.00%

## New creator shadow

- Decisions scored: 1506
- Shadow eligible: 0

## Execution stress

- 100ms_fee_1.0x: 65/85 filled, PnL -0.4764 SOL, PF 0.78
- 100ms_fee_1.5x: 65/85 filled, PnL -1.2619 SOL, PF 0.52
- 100ms_fee_2.0x: 65/85 filled, PnL -2.0308 SOL, PF 0.35
- 10ms_fee_1.0x: 80/85 filled, PnL +1.8281 SOL, PF 1.91
- 10ms_fee_1.5x: 80/85 filled, PnL +0.8030 SOL, PF 1.34
- 10ms_fee_2.0x: 80/85 filled, PnL -0.2004 SOL, PF 0.93
- 25ms_fee_1.0x: 71/85 filled, PnL +0.5079 SOL, PF 1.25
- 25ms_fee_1.5x: 71/85 filled, PnL -0.3812 SOL, PF 0.84
- 25ms_fee_2.0x: 71/85 filled, PnL -1.2516 SOL, PF 0.57
- 50ms_fee_1.0x: 67/85 filled, PnL -0.2788 SOL, PF 0.87
- 50ms_fee_1.5x: 67/85 filled, PnL -1.0998 SOL, PF 0.59
- 50ms_fee_2.0x: 67/85 filled, PnL -1.9035 SOL, PF 0.40
- 5ms_fee_1.0x: 85/85 filled, PnL +3.5547 SOL, PF 2.95
- 5ms_fee_1.5x: 85/85 filled, PnL +2.4212 SOL, PF 2.09
- 5ms_fee_2.0x: 85/85 filled, PnL +1.3117 SOL, PF 1.49

## Integrity

- Reliability status: PASS
- Guardrail state: HALT
- Guardrail halts: MODEL_DRIFT_RED
- Guardrail warnings: 50MS_EXECUTION_STRESS_NET_NEGATIVE, CLOSED_EQUITY_DRAWDOWN_OVER_12_PERCENT, HIGH_RUNTIME_DEPENDENCY_ADVISORIES_8
- Errors: none
- Warnings: none
- Processed shadow windows: 52

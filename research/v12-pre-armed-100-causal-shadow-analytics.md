# V12 Pre-Armed shadow intelligence

Status: **PASS**

This sidecar never changes V12 Pre-Armed selection, sizing, execution guards or exits.

## Creator concentration

- Creators observed: 13
- Effective creator count: 4.11
- Top creator trade share: 44.32%
- Top 3 trade share: 68.18%

## Drift

- Drift status: RED
- Flags: RECENT_10_NEGATIVE_EXPECTANCY

## Bankroll risk simulation

- Source trade samples: 88
- 20% DD probability over 200 trades: 0.08%
- Bankroll <= 1 SOL probability: 0.00%

## New creator shadow

- Decisions scored: 1583
- Shadow eligible: 0

## Execution stress

- 100ms_fee_1.0x: 68/88 filled, PnL -0.4901 SOL, PF 0.80
- 100ms_fee_1.5x: 68/88 filled, PnL -1.3227 SOL, PF 0.54
- 100ms_fee_2.0x: 68/88 filled, PnL -2.1377 SOL, PF 0.36
- 10ms_fee_1.0x: 83/88 filled, PnL +1.8621 SOL, PF 1.89
- 10ms_fee_1.5x: 83/88 filled, PnL +0.7883 SOL, PF 1.32
- 10ms_fee_2.0x: 83/88 filled, PnL -0.2629 SOL, PF 0.91
- 25ms_fee_1.0x: 74/88 filled, PnL +0.5419 SOL, PF 1.26
- 25ms_fee_1.5x: 74/88 filled, PnL -0.3960 SOL, PF 0.85
- 25ms_fee_2.0x: 74/88 filled, PnL -1.3141 SOL, PF 0.57
- 50ms_fee_1.0x: 70/88 filled, PnL -0.3425 SOL, PF 0.86
- 50ms_fee_1.5x: 70/88 filled, PnL -1.2096 SOL, PF 0.58
- 50ms_fee_2.0x: 70/88 filled, PnL -2.0583 SOL, PF 0.39
- 5ms_fee_1.0x: 88/88 filled, PnL +3.5890 SOL, PF 2.88
- 5ms_fee_1.5x: 88/88 filled, PnL +2.4067 SOL, PF 2.03
- 5ms_fee_2.0x: 88/88 filled, PnL +1.2494 SOL, PF 1.44

## Integrity

- Reliability status: PASS
- Guardrail state: HALT
- Guardrail halts: MODEL_DRIFT_RED
- Guardrail warnings: 50MS_EXECUTION_STRESS_NET_NEGATIVE, CLOSED_EQUITY_DRAWDOWN_OVER_12_PERCENT, HIGH_RUNTIME_DEPENDENCY_ADVISORIES_8
- Errors: none
- Warnings: none
- Processed shadow windows: 56

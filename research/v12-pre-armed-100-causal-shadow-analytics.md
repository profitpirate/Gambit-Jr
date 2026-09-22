# V12 Pre-Armed shadow intelligence

Status: **PASS**

This sidecar never changes V12 Pre-Armed selection, sizing, execution guards or exits.

## Creator concentration

- Creators observed: 9
- Effective creator count: 3.20
- Top creator trade share: 53.12%
- Top 3 trade share: 68.75%

## Drift

- Drift status: YELLOW
- Flags: TOP_CREATOR_OVER_50_PERCENT

## Bankroll risk simulation

- Source trade samples: 32
- 20% DD probability over 200 trades: 0.01%
- Bankroll <= 1 SOL probability: 0.00%

## New creator shadow

- Decisions scored: 580
- Shadow eligible: 0

## Execution stress

- 100ms_fee_1.0x: 25/32 filled, PnL -0.1006 SOL, PF 0.79
- 100ms_fee_1.5x: 25/32 filled, PnL -0.3480 SOL, PF 0.44
- 100ms_fee_2.0x: 25/32 filled, PnL -0.5901 SOL, PF 0.23
- 10ms_fee_1.0x: 32/32 filled, PnL +1.0401 SOL, PF 3.05
- 10ms_fee_1.5x: 32/32 filled, PnL +0.6964 SOL, PF 2.11
- 10ms_fee_2.0x: 32/32 filled, PnL +0.3600 SOL, PF 1.47
- 25ms_fee_1.0x: 27/32 filled, PnL +0.3522 SOL, PF 1.71
- 25ms_fee_1.5x: 27/32 filled, PnL +0.0723 SOL, PF 1.12
- 25ms_fee_2.0x: 27/32 filled, PnL -0.2016 SOL, PF 0.73
- 50ms_fee_1.0x: 25/32 filled, PnL -0.0236 SOL, PF 0.95
- 50ms_fee_1.5x: 25/32 filled, PnL -0.2735 SOL, PF 0.56
- 50ms_fee_2.0x: 25/32 filled, PnL -0.5179 SOL, PF 0.32
- 5ms_fee_1.0x: 32/32 filled, PnL +1.5080 SOL, PF 4.26
- 5ms_fee_1.5x: 32/32 filled, PnL +1.1525 SOL, PF 3.00
- 5ms_fee_2.0x: 32/32 filled, PnL +0.8047 SOL, PF 2.14

## Integrity

- Reliability status: PASS
- Guardrail state: WATCH
- Guardrail halts: none
- Guardrail warnings: 50MS_EXECUTION_STRESS_NET_NEGATIVE, HIGH_RUNTIME_DEPENDENCY_ADVISORIES_8, MODEL_DRIFT_YELLOW
- Errors: none
- Warnings: none
- Processed shadow windows: 18

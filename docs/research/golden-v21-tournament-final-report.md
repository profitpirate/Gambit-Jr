# Golden v2.1 — four-model 100-trade true-online tournament

Identical fresh-live cohort: **100 qualifying coins**. Each paper arm started with **3 SOL**. No transaction was broadcast.

## Ranking

| Rank | Model | W/L | WR | Net SOL | Ending SOL | PF | Max DD |
|---:|---|---:|---:|---:|---:|---:|---:|
| 1 | FULL_2S_CONTROL | 59/41 | 59.00% | +3.593295 | 6.593295 | 2.4867645508380756 | 15.10% |
| 2 | PARTIALS_2S | 59/41 | 59.00% | +3.032158 | 6.032158 | 2.649755496064128 | 13.23% |
| 3 | V2_CURRENT | 56/44 | 56.00% | +2.541551 | 5.541551 | 2.2272609257479403 | 18.26% |
| 4 | FLOW_V2_1 | 51/49 | 51.00% | +2.005263 | 5.005263 | 2.367066426406677 | 13.86% |

## Model feedback

### FULL_2S_CONTROL
- 3 trades reached >15% MFE but finished below +3%; winner-protection remains an improvement target.
- 12 trades lost 15%+ on position return; inspect cluster/flow classification and earlier invalidation.
- Most coins went higher after final exit; exits may be too defensive or runner retention too small.

### PARTIALS_2S
- Left 0.5611 SOL versus the best tournament arm on the identical 100-signal cohort; inspect trade-level counterfactuals.
- 3 trades reached >15% MFE but finished below +3%; winner-protection remains an improvement target.
- 10 trades lost 15%+ on position return; inspect cluster/flow classification and earlier invalidation.
- Most coins went higher after final exit; exits may be too defensive or runner retention too small.

### V2_CURRENT
- Left 1.0517 SOL versus the best tournament arm on the identical 100-signal cohort; inspect trade-level counterfactuals.
- 12 trades reached >15% MFE but finished below +3%; winner-protection remains an improvement target.
- 18 trades lost 15%+ on position return; inspect cluster/flow classification and earlier invalidation.
- Most coins went higher after final exit; exits may be too defensive or runner retention too small.

### FLOW_V2_1
- Left 1.5880 SOL versus the best tournament arm on the identical 100-signal cohort; inspect trade-level counterfactuals.
- 7 trades reached >15% MFE but finished below +3%; winner-protection remains an improvement target.
- 7 trades lost 15%+ on position return; inspect cluster/flow classification and earlier invalidation.
- Most coins went higher after final exit; exits may be too defensive or runner retention too small.
- Cluster adjustment affected 26 FLOW_V2_1 trades; compare their P&L against V2_CURRENT to validate the penalty.

## Every trade

### Trade 1 — `CPa1eLiTvsiQkMsVTpGEqz5cUCcMetZoZUKFMu1Zpump`

Best arm on this coin: **FULL_2S_CONTROL** (-0.006106 SOL)

**FULL_2S_CONTROL** — LOSS -0.006106 SOL (-2.54%); tier MEDIUM; MFE -0.83%; MAE -7.88%; exit `FULL_2S_EXIT`; hold 2003.9ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — LOSS -0.007546 SOL (-3.14%); tier MEDIUM; MFE -0.83%; MAE -7.88%; exit `HARD_2S_REMAINDER_EXIT`; hold 2003.9ms.
Could have made more vs best arm: 0.001440 SOL. Could have lost less vs best arm: 0.001440 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — LOSS -0.033064 SOL (-13.78%); tier MEDIUM; MFE -0.83%; MAE -17.69%; exit `PERSISTENT_DETERIORATION`; hold 2756.6ms.
Could have made more vs best arm: 0.026958 SOL. Could have lost less vs best arm: 0.026958 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/True.
**FLOW_V2_1** — LOSS -0.007546 SOL (-3.14%); tier MEDIUM; MFE -0.83%; MAE -7.88%; exit `FLOW_2S_EXIT_SCORE_0`; hold 2004.0ms.
Could have made more vs best arm: 0.001440 SOL. Could have lost less vs best arm: 0.001440 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/True.

### Trade 2 — `H4Gjcho6YgzBqkP791mDVM4jS4yPxApiHHvngGe3pump`

Best arm on this coin: **V2_CURRENT** (-0.009126 SOL)

**FULL_2S_CONTROL** — LOSS -0.009833 SOL (-4.11%); tier MEDIUM; MFE -4.11%; MAE -4.11%; exit `FULL_2S_EXIT`; hold 2000.7ms.
Could have made more vs best arm: 0.000706 SOL. Could have lost less vs best arm: 0.000706 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/False.
**PARTIALS_2S** — LOSS -0.009225 SOL (-3.85%); tier MEDIUM; MFE -4.10%; MAE -4.10%; exit `HARD_2S_REMAINDER_EXIT`; hold 2000.7ms.
Could have made more vs best arm: 0.000099 SOL. Could have lost less vs best arm: 0.000099 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/False.
**V2_CURRENT** — LOSS -0.009126 SOL (-3.84%); tier MEDIUM; MFE -4.09%; MAE -4.09%; exit `MAX_RUNNER_HOLD`; hold 6000.8ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/False.
**FLOW_V2_1** — LOSS -0.009225 SOL (-3.85%); tier MEDIUM; MFE -4.10%; MAE -4.10%; exit `FLOW_2S_EXIT_SCORE_-3`; hold 2000.8ms.
Could have made more vs best arm: 0.000099 SOL. Could have lost less vs best arm: 0.000099 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/False.

### Trade 3 — `FQnDCzmf2Wy5fqDDqBCk762Te3pGnQRvpahf35Gupump`

Best arm on this coin: **V2_CURRENT** (-0.009099 SOL)

**FULL_2S_CONTROL** — LOSS -0.009804 SOL (-4.11%); tier MEDIUM; MFE -4.10%; MAE -4.11%; exit `FULL_2S_EXIT`; hold 2003.9ms.
Could have made more vs best arm: 0.000705 SOL. Could have lost less vs best arm: 0.000705 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/False.
**PARTIALS_2S** — LOSS -0.009197 SOL (-3.85%); tier MEDIUM; MFE -4.10%; MAE -4.11%; exit `HARD_2S_REMAINDER_EXIT`; hold 2003.9ms.
Could have made more vs best arm: 0.000098 SOL. Could have lost less vs best arm: 0.000098 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/False.
**V2_CURRENT** — LOSS -0.009099 SOL (-3.85%); tier MEDIUM; MFE -4.09%; MAE -4.09%; exit `MAX_RUNNER_HOLD`; hold 6003.9ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/False.
**FLOW_V2_1** — LOSS -0.009197 SOL (-3.85%); tier MEDIUM; MFE -4.10%; MAE -4.11%; exit `FLOW_2S_EXIT_SCORE_-3`; hold 2004.0ms.
Could have made more vs best arm: 0.000098 SOL. Could have lost less vs best arm: 0.000098 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/False.

### Trade 4 — `Er6kBqncNYecVhRAAABeobjYBDJWLuor4pL8cjAxpump`

Best arm on this coin: **FULL_2S_CONTROL** (+0.009095 SOL)

**FULL_2S_CONTROL** — WIN +0.009095 SOL (+6.12%); tier BASE; MFE +6.12%; MAE -3.39%; exit `FULL_2S_EXIT`; hold 2002.7ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.009095 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.006777 SOL (+4.56%); tier BASE; MFE +6.12%; MAE -3.39%; exit `HARD_2S_REMAINDER_EXIT`; hold 2002.8ms.
Could have made more vs best arm: 0.002318 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.009094 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.005786 SOL (+3.92%); tier BASE; MFE +13.99%; MAE -3.38%; exit `MAX_RUNNER_HOLD`; hold 4004.5ms.
Could have made more vs best arm: 0.003309 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.020631 SOL. Post-exit higher/lower: False/True.
**FLOW_V2_1** — WIN +0.001906 SOL (+0.80%); tier MEDIUM; MFE +13.54%; MAE -3.74%; exit `FLOW_BREAKDOWN_SCORE_-5`; hold 3011.3ms.
Could have made more vs best arm: 0.007189 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.032221 SOL. Post-exit higher/lower: True/True.

### Trade 5 — `Cg6ivScmMUbZns3rBwWHMmKc3y45ridmDgEKsdqqpump`

Best arm on this coin: **FULL_2S_CONTROL** (+0.018763 SOL)

**FULL_2S_CONTROL** — WIN +0.018763 SOL (+7.86%); tier MEDIUM; MFE +7.86%; MAE -11.18%; exit `FULL_2S_EXIT`; hold 2001.4ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.018763 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — WIN +0.005576 SOL (+2.34%); tier MEDIUM; MFE +7.86%; MAE -11.17%; exit `HARD_2S_REMAINDER_EXIT`; hold 2001.5ms.
Could have made more vs best arm: 0.013187 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.018750 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — LOSS -0.004490 SOL (-1.90%); tier MEDIUM; MFE +21.08%; MAE -11.17%; exit `MAX_RUNNER_HOLD`; hold 6004.5ms.
Could have made more vs best arm: 0.023254 SOL. Could have lost less vs best arm: 0.023254 SOL. Hindsight MFE upper bound: 0.049828 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.015901 SOL (+3.56%); tier HIGH; MFE +19.82%; MAE -12.01%; exit `FLOW_BREAKDOWN_SCORE_-3`; hold 4024.5ms.
Could have made more vs best arm: 0.002863 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.088462 SOL. Post-exit higher/lower: False/True.

### Trade 6 — `3sZyt98QQKeq7wgfuPxwpVhtf755qCRU9vg4HdMUpump`

Best arm on this coin: **FULL_2S_CONTROL** (+0.075308 SOL)

**FULL_2S_CONTROL** — WIN +0.075308 SOL (+31.36%); tier MEDIUM; MFE +45.03%; MAE -3.72%; exit `FULL_2S_EXIT`; hold 2000.6ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.108151 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — WIN +0.058262 SOL (+24.39%); tier MEDIUM; MFE +45.04%; MAE -3.72%; exit `HARD_2S_REMAINDER_EXIT`; hold 2000.6ms.
Could have made more vs best arm: 0.017046 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.107606 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — WIN +0.029882 SOL (+12.66%); tier MEDIUM; MFE +45.06%; MAE -23.93%; exit `PERSISTENT_DETERIORATION_DEEP`; hold 4897.1ms.
Could have made more vs best arm: 0.045426 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.106344 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.058364 SOL (+24.38%); tier MEDIUM; MFE +45.04%; MAE -3.72%; exit `FLOW_BREAKDOWN_SCORE_-6`; hold 2254.6ms.
Could have made more vs best arm: 0.016944 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.107795 SOL. Post-exit higher/lower: False/True.

### Trade 7 — `3ZA9LfAWtPcuAbeYhpdtCyTsvS7QGQ8KoJ3cTBtWpump`

Best arm on this coin: **FULL_2S_CONTROL** (+0.010295 SOL)

**FULL_2S_CONTROL** — WIN +0.010295 SOL (+6.69%); tier BASE; MFE +6.69%; MAE -3.42%; exit `FULL_2S_EXIT`; hold 2002.6ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.010295 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — WIN +0.006365 SOL (+4.18%); tier BASE; MFE +6.70%; MAE -3.41%; exit `HARD_2S_REMAINDER_EXIT`; hold 2002.6ms.
Could have made more vs best arm: 0.003931 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.010196 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — WIN +0.006501 SOL (+4.36%); tier BASE; MFE +18.56%; MAE -3.40%; exit `MAX_RUNNER_HOLD`; hold 4002.0ms.
Could have made more vs best arm: 0.003795 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.027656 SOL. Post-exit higher/lower: False/True.
**FLOW_V2_1** — WIN +0.006920 SOL (+4.54%); tier BASE; MFE +18.54%; MAE -3.41%; exit `MAX_RUNNER_HOLD_SAFETY_CEILING`; hold 4002.1ms.
Could have made more vs best arm: 0.003376 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.028282 SOL. Post-exit higher/lower: False/True.

### Trade 8 — `AgddHYRzz4QMWCGDdvsCrQW1KLq16SG6Y3Qwy3Ydpump`

Best arm on this coin: **PARTIALS_2S** (+0.000756 SOL)

**FULL_2S_CONTROL** — LOSS -0.004381 SOL (-1.77%); tier MEDIUM; MFE +8.13%; MAE -6.88%; exit `FULL_2S_EXIT`; hold 2001.1ms.
Could have made more vs best arm: 0.005138 SOL. Could have lost less vs best arm: 0.005138 SOL. Hindsight MFE upper bound: 0.020086 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.000756 SOL (+0.31%); tier MEDIUM; MFE +8.15%; MAE -6.87%; exit `HARD_2S_REMAINDER_EXIT`; hold 2001.1ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.019883 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — LOSS -0.011104 SOL (-4.65%); tier MEDIUM; MFE +12.27%; MAE -12.36%; exit `MAX_RUNNER_HOLD`; hold 6003.9ms.
Could have made more vs best arm: 0.011860 SOL. Could have lost less vs best arm: 0.011860 SOL. Hindsight MFE upper bound: 0.029323 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — LOSS -0.007830 SOL (-3.20%); tier MEDIUM; MFE +12.24%; MAE -6.87%; exit `FLOW_BREAKDOWN_SCORE_-2`; hold 4278.3ms.
Could have made more vs best arm: 0.008586 SOL. Could have lost less vs best arm: 0.008586 SOL. Hindsight MFE upper bound: 0.029944 SOL. Post-exit higher/lower: True/True.

### Trade 9 — `EWiMpEbfgN29SdAG2SaprFyfmUEvh7Trzh7XUNrypump`

Best arm on this coin: **FULL_2S_CONTROL** (+0.033883 SOL)

**FULL_2S_CONTROL** — WIN +0.033883 SOL (+13.74%); tier MEDIUM; MFE +13.74%; MAE -3.79%; exit `FULL_2S_EXIT`; hold 2003.0ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.033883 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — WIN +0.021986 SOL (+9.01%); tier MEDIUM; MFE +13.75%; MAE -3.77%; exit `HARD_2S_REMAINDER_EXIT`; hold 2003.0ms.
Could have made more vs best arm: 0.011897 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.033569 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — LOSS -0.004678 SOL (-1.97%); tier MEDIUM; MFE +22.72%; MAE -12.27%; exit `MAX_RUNNER_HOLD`; hold 6000.4ms.
Could have made more vs best arm: 0.038561 SOL. Could have lost less vs best arm: 0.038561 SOL. Hindsight MFE upper bound: 0.054090 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.002757 SOL (+1.13%); tier MEDIUM; MFE +13.75%; MAE -3.77%; exit `FLOW_BREAKDOWN_SCORE_-2`; hold 2503.8ms.
Could have made more vs best arm: 0.031126 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.033545 SOL. Post-exit higher/lower: False/True.

### Trade 10 — `EQiUqiUzJDBMfxC9KaZEmTLBPJZnh6qQgD7G32SKpump`

Best arm on this coin: **FLOW_V2_1** (+0.052497 SOL)

**FULL_2S_CONTROL** — WIN +0.008039 SOL (+3.22%); tier MEDIUM; MFE +20.82%; MAE -3.81%; exit `FULL_2S_EXIT`; hold 2003.5ms.
Could have made more vs best arm: 0.044458 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.051932 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.016917 SOL (+6.88%); tier MEDIUM; MFE +20.85%; MAE -3.79%; exit `HARD_2S_REMAINDER_EXIT`; hold 2003.5ms.
Could have made more vs best arm: 0.035580 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.051259 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.042574 SOL (+17.91%); tier MEDIUM; MFE +35.43%; MAE -3.76%; exit `MAX_RUNNER_HOLD`; hold 6000.4ms.
Could have made more vs best arm: 0.009923 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.084188 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.052497 SOL (+21.50%); tier MEDIUM; MFE +30.31%; MAE -3.78%; exit `FLOW_BREAKDOWN_SCORE_-3`; hold 5028.4ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.073996 SOL. Post-exit higher/lower: True/True.

### Trade 11 — `EZbYSF1TU1JYYHwwihdXbGtKKHQ5kUc6RnVBrg61pump`

Best arm on this coin: **FULL_2S_CONTROL** (+0.031620 SOL)

**FULL_2S_CONTROL** — WIN +0.031620 SOL (+12.65%); tier MEDIUM; MFE +16.72%; MAE -3.82%; exit `FULL_2S_EXIT`; hold 2003.9ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.041797 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.019651 SOL (+7.95%); tier MEDIUM; MFE +16.73%; MAE -3.81%; exit `HARD_2S_REMAINDER_EXIT`; hold 2004.0ms.
Could have made more vs best arm: 0.011969 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.041373 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.019834 SOL (+8.23%); tier MEDIUM; MFE +27.96%; MAE -3.78%; exit `MAX_RUNNER_HOLD`; hold 6000.2ms.
Could have made more vs best arm: 0.011786 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.067393 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.027089 SOL (+10.91%); tier MEDIUM; MFE +27.91%; MAE -3.81%; exit `FLOW_BREAKDOWN_SCORE_-2`; hold 3517.9ms.
Could have made more vs best arm: 0.004531 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.069320 SOL. Post-exit higher/lower: True/True.

### Trade 12 — `Ab8dztUegzdeWC3ngdRLxJzyg9ejz3XSRsyoVFH3pump`

Best arm on this coin: **FLOW_V2_1** (+0.024275 SOL)

**FULL_2S_CONTROL** — WIN +0.016599 SOL (+6.57%); tier MEDIUM; MFE +11.78%; MAE -3.83%; exit `FULL_2S_EXIT`; hold 2002.2ms.
Could have made more vs best arm: 0.007676 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.029741 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.009497 SOL (+3.82%); tier MEDIUM; MFE +11.80%; MAE -3.82%; exit `HARD_2S_REMAINDER_EXIT`; hold 2002.2ms.
Could have made more vs best arm: 0.014778 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.029352 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.012295 SOL (+5.07%); tier MEDIUM; MFE +25.76%; MAE -3.79%; exit `MAX_RUNNER_HOLD`; hold 6000.5ms.
Could have made more vs best arm: 0.011980 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.062494 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.024275 SOL (+9.69%); tier MEDIUM; MFE +25.71%; MAE -3.82%; exit `FLOW_BREAKDOWN_SCORE_-10`; hold 3516.0ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.064402 SOL. Post-exit higher/lower: False/True.

### Trade 13 — `8Vo8fHHHh5qBJgAMAnypo86twf52gNmr7qwM1b81pump`

Best arm on this coin: **FLOW_V2_1** (+0.033690 SOL)

**FULL_2S_CONTROL** — WIN +0.022197 SOL (+13.99%); tier BASE; MFE +13.99%; MAE -3.47%; exit `FULL_2S_EXIT`; hold 2001.1ms.
Could have made more vs best arm: 0.011493 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.022197 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — WIN +0.013818 SOL (+8.86%); tier BASE; MFE +14.00%; MAE -3.46%; exit `HARD_2S_REMAINDER_EXIT`; hold 2001.1ms.
Could have made more vs best arm: 0.019872 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.021843 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — WIN +0.021620 SOL (+14.20%); tier BASE; MFE +26.01%; MAE -3.45%; exit `MAX_RUNNER_HOLD`; hold 4003.8ms.
Could have made more vs best arm: 0.012070 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.039610 SOL. Post-exit higher/lower: False/True.
**FLOW_V2_1** — WIN +0.033690 SOL (+13.34%); tier MEDIUM; MFE +25.39%; MAE -3.88%; exit `FLOW_BREAKDOWN_SCORE_-5`; hold 4016.7ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.064104 SOL. Post-exit higher/lower: False/True.

### Trade 14 — `GRf5nWEioNuDdnCrGE2rSxRn4KCycq3Wh3KQQyy1pump`

Best arm on this coin: **V2_CURRENT** (-0.071992 SOL)

**FULL_2S_CONTROL** — LOSS -0.104361 SOL (-40.82%); tier MEDIUM; MFE -3.09%; MAE -40.82%; exit `FULL_2S_EXIT`; hold 2002.0ms.
Could have made more vs best arm: 0.032368 SOL. Could have lost less vs best arm: 0.032368 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — LOSS -0.073584 SOL (-29.35%); tier MEDIUM; MFE -3.06%; MAE -40.81%; exit `HARD_2S_REMAINDER_EXIT`; hold 2002.0ms.
Could have made more vs best arm: 0.001592 SOL. Could have lost less vs best arm: 0.001592 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — LOSS -0.071992 SOL (-29.34%); tier MEDIUM; MFE -3.04%; MAE -40.80%; exit `EMERGENCY_CATASTROPHIC_DRAWDOWN`; hold 1220.7ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — LOSS -0.161120 SOL (-33.68%); tier HIGH; MFE -4.12%; MAE -41.37%; exit `EARLY_FLOW_INVALIDATION_-6`; hold 1220.7ms.
Could have made more vs best arm: 0.089128 SOL. Could have lost less vs best arm: 0.089128 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.

### Trade 15 — `DJJnw2RJFgqPhC98LYgYRdetjVus3uN5prw6TqnUpump`

Best arm on this coin: **PARTIALS_2S** (-0.018480 SOL)

**FULL_2S_CONTROL** — LOSS -0.023301 SOL (-9.42%); tier MEDIUM; MFE +2.13%; MAE -12.03%; exit `FULL_2S_EXIT`; hold 2003.9ms.
Could have made more vs best arm: 0.004821 SOL. Could have lost less vs best arm: 0.004821 SOL. Hindsight MFE upper bound: 0.005272 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — LOSS -0.018480 SOL (-7.55%); tier MEDIUM; MFE +2.14%; MAE -12.02%; exit `HARD_2S_REMAINDER_EXIT`; hold 2003.9ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.005248 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — LOSS -0.038402 SOL (-16.03%); tier MEDIUM; MFE +2.17%; MAE -21.50%; exit `MAX_RUNNER_HOLD`; hold 6003.1ms.
Could have made more vs best arm: 0.019922 SOL. Could have lost less vs best arm: 0.019922 SOL. Hindsight MFE upper bound: 0.005196 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — LOSS -0.022713 SOL (-9.37%); tier MEDIUM; MFE +2.16%; MAE -12.01%; exit `EARLY_FLOW_INVALIDATION_-3`; hold 583.4ms.
Could have made more vs best arm: 0.004232 SOL. Could have lost less vs best arm: 0.004232 SOL. Hindsight MFE upper bound: 0.005223 SOL. Post-exit higher/lower: True/True.

### Trade 16 — `9rmQzw4xD5rWAE22WGVuMVXF27ZW7sBNWp9eGEvpump`

Best arm on this coin: **V2_CURRENT** (+0.026965 SOL)

**FULL_2S_CONTROL** — LOSS -0.004980 SOL (-3.25%); tier BASE; MFE -3.25%; MAE -3.39%; exit `FULL_2S_EXIT`; hold 2000.4ms.
Could have made more vs best arm: 0.031945 SOL. Could have lost less vs best arm: 0.031945 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — LOSS -0.004916 SOL (-3.23%); tier BASE; MFE -3.24%; MAE -3.38%; exit `HARD_2S_REMAINDER_EXIT`; hold 2000.5ms.
Could have made more vs best arm: 0.031881 SOL. Could have lost less vs best arm: 0.031881 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.026965 SOL (+18.24%); tier BASE; MFE +34.03%; MAE -3.37%; exit `MAX_RUNNER_HOLD`; hold 4004.2ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.050305 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — LOSS -0.008305 SOL (-3.45%); tier MEDIUM; MFE -3.59%; MAE -3.73%; exit `FLOW_2S_EXIT_SCORE_0`; hold 2000.6ms.
Could have made more vs best arm: 0.035270 SOL. Could have lost less vs best arm: 0.035270 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.

### Trade 17 — `ExPh3wqmev8VVLAFYopxDZDb8w6ThLnYaEQwQAazpump`

Best arm on this coin: **FULL_2S_CONTROL** (+0.027900 SOL)

**FULL_2S_CONTROL** — WIN +0.027900 SOL (+11.39%); tier MEDIUM; MFE +11.86%; MAE -3.79%; exit `FULL_2S_EXIT`; hold 2003.5ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.029051 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.019893 SOL (+8.19%); tier MEDIUM; MFE +11.87%; MAE -3.78%; exit `HARD_2S_REMAINDER_EXIT`; hold 2003.5ms.
Could have made more vs best arm: 0.008008 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.028828 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — LOSS -0.002150 SOL (-0.90%); tier MEDIUM; MFE +16.13%; MAE -9.68%; exit `MAX_RUNNER_HOLD`; hold 6002.9ms.
Could have made more vs best arm: 0.030051 SOL. Could have lost less vs best arm: 0.030051 SOL. Hindsight MFE upper bound: 0.038487 SOL. Post-exit higher/lower: True/False.
**FLOW_V2_1** — LOSS -0.000856 SOL (-0.19%); tier HIGH; MFE +10.73%; MAE -4.71%; exit `FLOW_BREAKDOWN_SCORE_-8`; hold 2508.4ms.
Could have made more vs best arm: 0.028757 SOL. Could have lost less vs best arm: 0.028757 SOL. Hindsight MFE upper bound: 0.048262 SOL. Post-exit higher/lower: True/True.

### Trade 18 — `6b1AjkoU9f5smaUEGPusjB38PaPc6Uw9w7t6aKARs1Do`

Best arm on this coin: **FULL_2S_CONTROL** (+0.002299 SOL)

**FULL_2S_CONTROL** — WIN +0.002299 SOL (+1.49%); tier BASE; MFE +1.49%; MAE -3.64%; exit `FULL_2S_EXIT`; hold 2003.8ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.002299 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — WIN +0.000670 SOL (+0.44%); tier BASE; MFE +1.50%; MAE -3.63%; exit `HARD_2S_REMAINDER_EXIT`; hold 2003.8ms.
Could have made more vs best arm: 0.001629 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.002287 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — WIN +0.000672 SOL (+0.45%); tier BASE; MFE +1.52%; MAE -3.61%; exit `MAX_RUNNER_HOLD`; hold 4000.2ms.
Could have made more vs best arm: 0.001627 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.002261 SOL. Post-exit higher/lower: False/True.
**FLOW_V2_1** — WIN +0.000671 SOL (+0.45%); tier BASE; MFE +1.51%; MAE -3.62%; exit `FLOW_2S_EXIT_SCORE_0`; hold 2003.9ms.
Could have made more vs best arm: 0.001627 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.002267 SOL. Post-exit higher/lower: False/True.

### Trade 19 — `CSnF5ERQ1G5pRVHRsfmv9neX9Lb5ST1fj3vZWzY8pump`

Best arm on this coin: **V2_CURRENT** (-0.003518 SOL)

**FULL_2S_CONTROL** — LOSS -0.017585 SOL (-2.27%); tier EXCEPTIONAL; MFE -2.27%; MAE -3.97%; exit `FULL_2S_EXIT`; hold 2003.6ms.
Could have made more vs best arm: 0.014067 SOL. Could have lost less vs best arm: 0.014067 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/False.
**PARTIALS_2S** — LOSS -0.018223 SOL (-2.38%); tier EXCEPTIONAL; MFE -2.26%; MAE -3.95%; exit `HARD_2S_REMAINDER_EXIT`; hold 2003.6ms.
Could have made more vs best arm: 0.014705 SOL. Could have lost less vs best arm: 0.014705 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/False.
**V2_CURRENT** — LOSS -0.003518 SOL (-0.47%); tier EXCEPTIONAL; MFE +0.13%; MAE -3.92%; exit `MAX_RUNNER_HOLD`; hold 16003.1ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.000947 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — LOSS -0.012434 SOL (-1.66%); tier EXCEPTIONAL; MFE -0.78%; MAE -3.92%; exit `FLOW_BREAKDOWN_SCORE_-4`; hold 6545.9ms.
Could have made more vs best arm: 0.008916 SOL. Could have lost less vs best arm: 0.008916 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/False.

### Trade 20 — `GptRxjmphUKg2JVFhTRU1y28SWiiaJdPbVHstzc7g9ht`

Best arm on this coin: **V2_CURRENT** (-0.002389 SOL)

**FULL_2S_CONTROL** — LOSS -0.004295 SOL (-1.75%); tier MEDIUM; MFE -1.75%; MAE -3.06%; exit `FULL_2S_EXIT`; hold 2003.7ms.
Could have made more vs best arm: 0.001906 SOL. Could have lost less vs best arm: 0.001906 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/False.
**PARTIALS_2S** — LOSS -0.004440 SOL (-1.83%); tier MEDIUM; MFE -1.74%; MAE -3.05%; exit `HARD_2S_REMAINDER_EXIT`; hold 2003.7ms.
Could have made more vs best arm: 0.002051 SOL. Could have lost less vs best arm: 0.002051 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/False.
**V2_CURRENT** — LOSS -0.002389 SOL (-1.00%); tier MEDIUM; MFE -0.56%; MAE -3.05%; exit `MAX_RUNNER_HOLD`; hold 6002.3ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/False.
**FLOW_V2_1** — LOSS -0.002395 SOL (-1.00%); tier MEDIUM; MFE -0.57%; MAE -3.05%; exit `MAX_RUNNER_HOLD_SAFETY_CEILING`; hold 6002.4ms.
Could have made more vs best arm: 0.000006 SOL. Could have lost less vs best arm: 0.000006 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/False.

### Trade 21 — `nD5xNzoA2m6daGjTiBn6bw9kHSzaHxu2ZTQT3snpump`

Best arm on this coin: **FLOW_V2_1** (-0.005524 SOL)

**FULL_2S_CONTROL** — LOSS -0.035909 SOL (-7.79%); tier HIGH; MFE +12.48%; MAE -11.57%; exit `FULL_2S_EXIT`; hold 2003.8ms.
Could have made more vs best arm: 0.030385 SOL. Could have lost less vs best arm: 0.030385 SOL. Hindsight MFE upper bound: 0.057512 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — LOSS -0.024139 SOL (-5.30%); tier HIGH; MFE +12.51%; MAE -11.54%; exit `HARD_2S_REMAINDER_EXIT`; hold 2003.8ms.
Could have made more vs best arm: 0.018615 SOL. Could have lost less vs best arm: 0.018615 SOL. Hindsight MFE upper bound: 0.056955 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — LOSS -0.006706 SOL (-1.50%); tier HIGH; MFE +12.56%; MAE -11.51%; exit `MAX_RUNNER_HOLD`; hold 10000.2ms.
Could have made more vs best arm: 0.001182 SOL. Could have lost less vs best arm: 0.001182 SOL. Hindsight MFE upper bound: 0.056064 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — LOSS -0.005524 SOL (-2.32%); tier MEDIUM; MFE +13.64%; MAE -10.72%; exit `FLOW_BREAKDOWN_SCORE_-6`; hold 5281.2ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.032533 SOL. Post-exit higher/lower: True/True.

### Trade 22 — `AAfJPcqbxkwt8pg1dtnrb4djErYXi7LzVQ8WhauWpump`

Best arm on this coin: **FULL_2S_CONTROL** (+0.030381 SOL)

**FULL_2S_CONTROL** — WIN +0.030381 SOL (+12.51%); tier MEDIUM; MFE +12.51%; MAE -3.77%; exit `FULL_2S_EXIT`; hold 2001.4ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.030381 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — WIN +0.020758 SOL (+8.62%); tier MEDIUM; MFE +12.52%; MAE -3.76%; exit `HARD_2S_REMAINDER_EXIT`; hold 2001.4ms.
Could have made more vs best arm: 0.009623 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.030156 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — LOSS -0.004443 SOL (-1.87%); tier MEDIUM; MFE +15.25%; MAE -8.49%; exit `MAX_RUNNER_HOLD`; hold 6004.0ms.
Could have made more vs best arm: 0.034824 SOL. Could have lost less vs best arm: 0.034824 SOL. Hindsight MFE upper bound: 0.036218 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.020300 SOL (+4.55%); tier HIGH; MFE +11.40%; MAE -4.68%; exit `FLOW_BREAKDOWN_SCORE_-4`; hold 2506.1ms.
Could have made more vs best arm: 0.010081 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.050887 SOL. Post-exit higher/lower: False/True.

### Trade 23 — `2goEFMEBFMKNxknpSaPMsZcndsm9oQusHv6nBeuGpump`

Best arm on this coin: **FULL_2S_CONTROL** (+0.020926 SOL)

**FULL_2S_CONTROL** — WIN +0.020926 SOL (+8.53%); tier MEDIUM; MFE +8.53%; MAE -3.81%; exit `FULL_2S_EXIT`; hold 2002.7ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.020926 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — WIN +0.012699 SOL (+5.24%); tier MEDIUM; MFE +8.55%; MAE -3.79%; exit `HARD_2S_REMAINDER_EXIT`; hold 2002.7ms.
Could have made more vs best arm: 0.008227 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.020724 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — WIN +0.007848 SOL (+3.31%); tier MEDIUM; MFE +20.01%; MAE -3.77%; exit `MAX_RUNNER_HOLD`; hold 6000.5ms.
Could have made more vs best arm: 0.013077 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.047472 SOL. Post-exit higher/lower: False/True.
**FLOW_V2_1** — WIN +0.012469 SOL (+5.20%); tier MEDIUM; MFE +18.29%; MAE -3.78%; exit `FLOW_BREAKDOWN_SCORE_-5`; hold 3267.6ms.
Could have made more vs best arm: 0.008457 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.043853 SOL. Post-exit higher/lower: False/True.

### Trade 24 — `Hjuo3mGL1ogVNzUN1bcgU95Qpjz7nEa9aMyjDC4Ypump`

Best arm on this coin: **FLOW_V2_1** (-0.031222 SOL)

**FULL_2S_CONTROL** — LOSS -0.036554 SOL (-7.89%); tier HIGH; MFE +0.92%; MAE -7.90%; exit `FULL_2S_EXIT`; hold 2000.5ms.
Could have made more vs best arm: 0.005332 SOL. Could have lost less vs best arm: 0.005332 SOL. Hindsight MFE upper bound: 0.004282 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — LOSS -0.031661 SOL (-6.93%); tier HIGH; MFE +0.96%; MAE -7.87%; exit `HARD_2S_REMAINDER_EXIT`; hold 2000.5ms.
Could have made more vs best arm: 0.000438 SOL. Could have lost less vs best arm: 0.000438 SOL. Hindsight MFE upper bound: 0.004365 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — LOSS -0.078373 SOL (-17.58%); tier HIGH; MFE +11.27%; MAE -21.13%; exit `MAX_RUNNER_HOLD`; hold 10000.4ms.
Could have made more vs best arm: 0.047150 SOL. Could have lost less vs best arm: 0.047150 SOL. Hindsight MFE upper bound: 0.050239 SOL. Post-exit higher/lower: False/True.
**FLOW_V2_1** — LOSS -0.031222 SOL (-6.92%); tier HIGH; MFE +0.98%; MAE -7.85%; exit `FLOW_2S_EXIT_SCORE_-2`; hold 2000.6ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.004428 SOL. Post-exit higher/lower: False/True.

### Trade 25 — `Gw3t8sAxQpWSxDWFuyKQeaCjiqAH6RaBUCYqPMdEyMNr`

Best arm on this coin: **FULL_2S_CONTROL** (+0.025001 SOL)

**FULL_2S_CONTROL** — WIN +0.025001 SOL (+16.39%); tier BASE; MFE +16.39%; MAE -3.47%; exit `FULL_2S_EXIT`; hold 2003.5ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.025001 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.017561 SOL (+11.66%); tier BASE; MFE +16.40%; MAE -3.46%; exit `HARD_2S_REMAINDER_EXIT`; hold 2003.5ms.
Could have made more vs best arm: 0.007440 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.024705 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.002479 SOL (+1.71%); tier BASE; MFE +16.43%; MAE -3.44%; exit `MAX_RUNNER_HOLD`; hold 4003.8ms.
Could have made more vs best arm: 0.022522 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.023783 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.017372 SOL (+11.66%); tier BASE; MFE +16.41%; MAE -3.46%; exit `FLOW_2S_EXIT_SCORE_0`; hold 2003.6ms.
Could have made more vs best arm: 0.007629 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.024441 SOL. Post-exit higher/lower: True/True.

### Trade 26 — `BRW7MYjJBYNrkWjsxNzk7j7vsEnRzaMtgPmYvf1hpump`

Best arm on this coin: **FULL_2S_CONTROL** (+0.017044 SOL)

**FULL_2S_CONTROL** — WIN +0.017044 SOL (+3.69%); tier HIGH; MFE +3.69%; MAE -8.02%; exit `FULL_2S_EXIT`; hold 2003.8ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.017044 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.011593 SOL (+2.55%); tier HIGH; MFE +3.73%; MAE -7.99%; exit `HARD_2S_REMAINDER_EXIT`; hold 2003.9ms.
Could have made more vs best arm: 0.005451 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.016951 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.003444 SOL (+0.79%); tier HIGH; MFE +11.39%; MAE -18.34%; exit `MAX_RUNNER_HOLD`; hold 10001.2ms.
Could have made more vs best arm: 0.013600 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.049511 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — LOSS -0.005009 SOL (-0.67%); tier EXCEPTIONAL; MFE +2.22%; MAE -9.28%; exit `FLOW_BREAKDOWN_SCORE_-3`; hold 2757.0ms.
Could have made more vs best arm: 0.022053 SOL. Could have lost less vs best arm: 0.022053 SOL. Hindsight MFE upper bound: 0.016643 SOL. Post-exit higher/lower: True/True.

### Trade 27 — `poTGWQcr5ZshUBZGM8Wa16mD7iVaA38KNUpmFCLxPHd`

Best arm on this coin: **FULL_2S_CONTROL** (+0.012227 SOL)

**FULL_2S_CONTROL** — WIN +0.012227 SOL (+7.91%); tier BASE; MFE +7.91%; MAE -3.48%; exit `FULL_2S_EXIT`; hold 2003.2ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.012227 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.012196 SOL (+8.02%); tier BASE; MFE +7.92%; MAE -3.47%; exit `HARD_2S_REMAINDER_EXIT`; hold 2003.2ms.
Could have made more vs best arm: 0.000031 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.012044 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — LOSS -0.001758 SOL (-1.21%); tier BASE; MFE +7.95%; MAE -5.24%; exit `MAX_RUNNER_HOLD`; hold 4001.8ms.
Could have made more vs best arm: 0.013985 SOL. Could have lost less vs best arm: 0.013985 SOL. Hindsight MFE upper bound: 0.011533 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.012004 SOL (+8.03%); tier BASE; MFE +7.93%; MAE -3.46%; exit `FLOW_2S_EXIT_SCORE_0`; hold 2003.3ms.
Could have made more vs best arm: 0.000224 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.011862 SOL. Post-exit higher/lower: True/True.

### Trade 28 — `GhJVQXLZYqbn4Ku1PyCKfZLMxfvsNZTxbjvcau7Epump`

Best arm on this coin: **PARTIALS_2S** (-0.013084 SOL)

**FULL_2S_CONTROL** — LOSS -0.014825 SOL (-3.18%); tier HIGH; MFE -3.18%; MAE -4.76%; exit `FULL_2S_EXIT`; hold 2002.9ms.
Could have made more vs best arm: 0.001741 SOL. Could have lost less vs best arm: 0.001741 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — LOSS -0.013084 SOL (-2.86%); tier HIGH; MFE -3.15%; MAE -4.73%; exit `HARD_2S_REMAINDER_EXIT`; hold 2003.0ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — LOSS -0.024036 SOL (-5.53%); tier HIGH; MFE +16.16%; MAE -13.69%; exit `PERSISTENT_DETERIORATION`; hold 4869.2ms.
Could have made more vs best arm: 0.010952 SOL. Could have lost less vs best arm: 0.010952 SOL. Hindsight MFE upper bound: 0.070256 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — LOSS -0.032090 SOL (-4.27%); tier EXCEPTIONAL; MFE -4.50%; MAE -6.06%; exit `FLOW_2S_EXIT_SCORE_2`; hold 2003.0ms.
Could have made more vs best arm: 0.019006 SOL. Could have lost less vs best arm: 0.019006 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.

### Trade 29 — `GYS1v9gCBaA5ZxC8hPxoGYDjHtg8UbgmyHR23otdpump`

Best arm on this coin: **FULL_2S_CONTROL** (+0.027248 SOL)

**FULL_2S_CONTROL** — WIN +0.027248 SOL (+5.88%); tier HIGH; MFE +5.88%; MAE -4.74%; exit `FULL_2S_EXIT`; hold 2001.6ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.027248 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — WIN +0.018839 SOL (+4.13%); tier HIGH; MFE +5.92%; MAE -4.71%; exit `HARD_2S_REMAINDER_EXIT`; hold 2001.7ms.
Could have made more vs best arm: 0.008409 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.026988 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — LOSS -0.041196 SOL (-9.55%); tier HIGH; MFE +13.23%; MAE -13.32%; exit `PERSISTENT_DETERIORATION`; hold 9294.1ms.
Could have made more vs best arm: 0.068444 SOL. Could have lost less vs best arm: 0.068444 SOL. Hindsight MFE upper bound: 0.057064 SOL. Post-exit higher/lower: False/True.
**FLOW_V2_1** — LOSS -0.022000 SOL (-4.94%); tier HIGH; MFE +5.97%; MAE -5.38%; exit `FLOW_BREAKDOWN_SCORE_-6`; hold 2254.4ms.
Could have made more vs best arm: 0.049248 SOL. Could have lost less vs best arm: 0.049248 SOL. Hindsight MFE upper bound: 0.026611 SOL. Post-exit higher/lower: False/True.

### Trade 30 — `6W98YdTGRdSA7ZwY9zuj2M96dBXjrq88CTmiCqQTCZKM`

Best arm on this coin: **FULL_2S_CONTROL** (+0.010871 SOL)

**FULL_2S_CONTROL** — WIN +0.010871 SOL (+4.36%); tier MEDIUM; MFE +4.36%; MAE -3.90%; exit `FULL_2S_EXIT`; hold 2003.3ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.010871 SOL. Post-exit higher/lower: True/False.
**PARTIALS_2S** — WIN +0.007798 SOL (+3.19%); tier MEDIUM; MFE +4.38%; MAE -3.88%; exit `HARD_2S_REMAINDER_EXIT`; hold 2003.3ms.
Could have made more vs best arm: 0.003074 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.010730 SOL. Post-exit higher/lower: True/False.
**V2_CURRENT** — LOSS -0.000029 SOL (-0.01%); tier MEDIUM; MFE +4.48%; MAE -10.50%; exit `MAX_RUNNER_HOLD`; hold 6003.1ms.
Could have made more vs best arm: 0.010900 SOL. Could have lost less vs best arm: 0.010900 SOL. Hindsight MFE upper bound: 0.010151 SOL. Post-exit higher/lower: True/False.
**FLOW_V2_1** — WIN +0.007593 SOL (+3.22%); tier MEDIUM; MFE +4.43%; MAE -3.84%; exit `FLOW_2S_EXIT_SCORE_0`; hold 2003.4ms.
Could have made more vs best arm: 0.003278 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.010451 SOL. Post-exit higher/lower: True/False.

### Trade 31 — `E5QkpjLTyk2kiUtZxRx3KLXDvTxyjg3QtLqCE6EKpump`

Best arm on this coin: **FULL_2S_CONTROL** (+0.004995 SOL)

**FULL_2S_CONTROL** — WIN +0.004995 SOL (+1.06%); tier HIGH; MFE +1.06%; MAE -4.78%; exit `FULL_2S_EXIT`; hold 2004.1ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.004995 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — WIN +0.001331 SOL (+0.29%); tier HIGH; MFE +1.11%; MAE -4.74%; exit `HARD_2S_REMAINDER_EXIT`; hold 2004.1ms.
Could have made more vs best arm: 0.003664 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.005103 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — LOSS -0.069788 SOL (-16.42%); tier HIGH; MFE +14.37%; MAE -32.52%; exit `PERSISTENT_DETERIORATION_DEEP`; hold 7005.7ms.
Could have made more vs best arm: 0.074783 SOL. Could have lost less vs best arm: 0.074783 SOL. Hindsight MFE upper bound: 0.061088 SOL. Post-exit higher/lower: False/True.
**FLOW_V2_1** — WIN +0.004251 SOL (+0.96%); tier HIGH; MFE +14.27%; MAE -5.24%; exit `FLOW_BREAKDOWN_SCORE_-3`; hold 4024.0ms.
Could have made more vs best arm: 0.000743 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.063271 SOL. Post-exit higher/lower: False/True.

### Trade 32 — `21um4bSJE2uktWoNUu4XZdaxWakBdV5Abo2MpmczWorh`

Best arm on this coin: **FULL_2S_CONTROL** (+0.026191 SOL)

**FULL_2S_CONTROL** — WIN +0.026191 SOL (+10.45%); tier MEDIUM; MFE +10.45%; MAE -3.91%; exit `FULL_2S_EXIT`; hold 2001.7ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.026191 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.018328 SOL (+7.47%); tier MEDIUM; MFE +10.48%; MAE -3.88%; exit `HARD_2S_REMAINDER_EXIT`; hold 2001.7ms.
Could have made more vs best arm: 0.007863 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.025725 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.007738 SOL (+3.50%); tier MEDIUM; MFE +15.66%; MAE -3.77%; exit `MAX_RUNNER_HOLD`; hold 6003.7ms.
Could have made more vs best arm: 0.018454 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.034613 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.005086 SOL (+2.15%); tier MEDIUM; MFE +15.56%; MAE -3.84%; exit `PROFIT_FLOOR_PEAK_0.156_FLOOR_0.036_SCORE_1`; hold 4519.6ms.
Could have made more vs best arm: 0.021105 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.036859 SOL. Post-exit higher/lower: True/True.

### Trade 33 — `GtnoMzrG3yf4VNsFGYgt8FhtRZMwurXCyt2uC9grpump`

Best arm on this coin: **FULL_2S_CONTROL** (+0.063954 SOL)

**FULL_2S_CONTROL** — WIN +0.063954 SOL (+13.50%); tier HIGH; MFE +13.50%; MAE -4.81%; exit `FULL_2S_EXIT`; hold 2000.1ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.063954 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — WIN +0.047710 SOL (+10.30%); tier HIGH; MFE +13.56%; MAE -4.76%; exit `HARD_2S_REMAINDER_EXIT`; hold 2000.1ms.
Could have made more vs best arm: 0.016244 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.062779 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — LOSS -0.007016 SOL (-1.69%); tier HIGH; MFE +20.42%; MAE -14.15%; exit `PERSISTENT_DETERIORATION`; hold 8870.8ms.
Could have made more vs best arm: 0.070970 SOL. Could have lost less vs best arm: 0.070970 SOL. Hindsight MFE upper bound: 0.084903 SOL. Post-exit higher/lower: False/True.
**FLOW_V2_1** — WIN +0.031765 SOL (+7.14%); tier HIGH; MFE +13.66%; MAE -4.67%; exit `FLOW_BREAKDOWN_SCORE_-2`; hold 3012.4ms.
Could have made more vs best arm: 0.032189 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.060776 SOL. Post-exit higher/lower: False/True.

### Trade 34 — `4R6Y1nBkNJLqctZsh7koG9gdDGYQeshA9KS8w8uMULve`

Best arm on this coin: **V2_CURRENT** (+0.021688 SOL)

**FULL_2S_CONTROL** — WIN +0.001932 SOL (+0.75%); tier MEDIUM; MFE +0.75%; MAE -3.94%; exit `FULL_2S_EXIT`; hold 2000.1ms.
Could have made more vs best arm: 0.019756 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.001932 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.002558 SOL (+1.02%); tier MEDIUM; MFE +0.78%; MAE -3.91%; exit `HARD_2S_REMAINDER_EXIT`; hold 2000.1ms.
Could have made more vs best arm: 0.019131 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.001968 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.021688 SOL (+9.81%); tier MEDIUM; MFE +13.30%; MAE -3.77%; exit `MAX_RUNNER_HOLD`; hold 6001.8ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.029412 SOL. Post-exit higher/lower: False/True.
**FLOW_V2_1** — WIN +0.000516 SOL (+0.11%); tier HIGH; MFE -0.25%; MAE -4.88%; exit `FLOW_2S_EXIT_SCORE_0`; hold 2000.2ms.
Could have made more vs best arm: 0.021172 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.

### Trade 35 — `GrmLH7qMZtihW5DDZe4BWc2qzBT3NrvLZpKsRuiipump`

Best arm on this coin: **FULL_2S_CONTROL** (+0.066218 SOL)

**FULL_2S_CONTROL** — WIN +0.066218 SOL (+13.69%); tier HIGH; MFE +14.32%; MAE -4.85%; exit `FULL_2S_EXIT`; hold 2001.5ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.069258 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — WIN +0.049265 SOL (+10.47%); tier HIGH; MFE +14.39%; MAE -4.79%; exit `HARD_2S_REMAINDER_EXIT`; hold 2001.5ms.
Could have made more vs best arm: 0.016953 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.067729 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — LOSS -0.008062 SOL (-1.93%); tier HIGH; MFE +21.64%; MAE -13.70%; exit `PERSISTENT_DETERIORATION`; hold 6650.0ms.
Could have made more vs best arm: 0.074280 SOL. Could have lost less vs best arm: 0.074280 SOL. Hindsight MFE upper bound: 0.090434 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.053635 SOL (+7.16%); tier EXCEPTIONAL; MFE +12.79%; MAE -6.06%; exit `FLOW_BREAKDOWN_SCORE_-6`; hold 2758.3ms.
Could have made more vs best arm: 0.012583 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.095896 SOL. Post-exit higher/lower: False/True.

### Trade 36 — `3rpjnHnyVUQSPxg8EQMDyvmJkfMHaxPpJv5up6d6pump`

Best arm on this coin: **V2_CURRENT** (-0.037140 SOL)

**FULL_2S_CONTROL** — LOSS -0.060899 SOL (-37.01%); tier BASE; MFE -1.89%; MAE -37.01%; exit `FULL_2S_EXIT`; hold 2003.1ms.
Could have made more vs best arm: 0.023758 SOL. Could have lost less vs best arm: 0.023758 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — LOSS -0.042103 SOL (-26.43%); tier BASE; MFE -1.87%; MAE -37.00%; exit `HARD_2S_REMAINDER_EXIT`; hold 2003.1ms.
Could have made more vs best arm: 0.004963 SOL. Could have lost less vs best arm: 0.004963 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — LOSS -0.037140 SOL (-26.34%); tier BASE; MFE -1.80%; MAE -36.87%; exit `EMERGENCY_CATASTROPHIC_DRAWDOWN`; hold 1257.1ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/True.
**FLOW_V2_1** — LOSS -0.040205 SOL (-26.35%); tier BASE; MFE -1.84%; MAE -36.89%; exit `EARLY_FLOW_INVALIDATION_-4`; hold 1257.2ms.
Could have made more vs best arm: 0.003065 SOL. Could have lost less vs best arm: 0.003065 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/True.

### Trade 37 — `29myZBfbVvVKzXso3bddVhBtqVunqXHLZAyD3LLypump`

Best arm on this coin: **FULL_2S_CONTROL** (+0.045265 SOL)

**FULL_2S_CONTROL** — WIN +0.045265 SOL (+5.61%); tier EXCEPTIONAL; MFE +5.61%; MAE -12.28%; exit `FULL_2S_EXIT`; hold 2003.8ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.045265 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.036517 SOL (+4.65%); tier EXCEPTIONAL; MFE +5.72%; MAE -12.19%; exit `HARD_2S_REMAINDER_EXIT`; hold 2003.8ms.
Could have made more vs best arm: 0.008748 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.044947 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — LOSS -0.121326 SOL (-17.71%); tier EXCEPTIONAL; MFE +7.19%; MAE -22.10%; exit `PERSISTENT_DETERIORATION`; hold 6234.1ms.
Could have made more vs best arm: 0.166591 SOL. Could have lost less vs best arm: 0.166591 SOL. Hindsight MFE upper bound: 0.049249 SOL. Post-exit higher/lower: True/False.
**FLOW_V2_1** — LOSS -0.072600 SOL (-9.64%); tier EXCEPTIONAL; MFE -1.97%; MAE -12.05%; exit `EARLY_FLOW_INVALIDATION_-2`; hold 622.3ms.
Could have made more vs best arm: 0.117865 SOL. Could have lost less vs best arm: 0.117865 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.

### Trade 38 — `DB1bDNmXvcjTp2ZdPeJeJcRbh1eHpjyx7snDMGYJLVSC`

Best arm on this coin: **FULL_2S_CONTROL** (+0.019688 SOL)

**FULL_2S_CONTROL** — WIN +0.019688 SOL (+7.51%); tier MEDIUM; MFE +7.51%; MAE -3.96%; exit `FULL_2S_EXIT`; hold 2002.9ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.019688 SOL. Post-exit higher/lower: True/False.
**PARTIALS_2S** — WIN +0.011674 SOL (+4.59%); tier MEDIUM; MFE +7.56%; MAE -3.93%; exit `HARD_2S_REMAINDER_EXIT`; hold 2002.9ms.
Could have made more vs best arm: 0.008014 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.019225 SOL. Post-exit higher/lower: True/False.
**V2_CURRENT** — WIN +0.010938 SOL (+5.22%); tier MEDIUM; MFE +8.95%; MAE -3.72%; exit `MAX_RUNNER_HOLD`; hold 6004.0ms.
Could have made more vs best arm: 0.008750 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.018757 SOL. Post-exit higher/lower: True/False.
**FLOW_V2_1** — WIN +0.010960 SOL (+4.66%); tier MEDIUM; MFE +7.66%; MAE -3.83%; exit `FLOW_2S_EXIT_SCORE_0`; hold 2003.0ms.
Could have made more vs best arm: 0.008727 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.018012 SOL. Post-exit higher/lower: True/False.

### Trade 39 — `BFipPjJmCHckoWpxB6nt8knfaFN2WD7V4aHgC8KZpump`

Best arm on this coin: **V2_CURRENT** (+0.001374 SOL)

**FULL_2S_CONTROL** — LOSS -0.035518 SOL (-21.56%); tier BASE; MFE +22.99%; MAE -21.56%; exit `FULL_2S_EXIT`; hold 2001.8ms.
Could have made more vs best arm: 0.036891 SOL. Could have lost less vs best arm: 0.036891 SOL. Hindsight MFE upper bound: 0.037877 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — LOSS -0.022106 SOL (-13.85%); tier BASE; MFE +23.02%; MAE -21.54%; exit `HARD_2S_REMAINDER_EXIT`; hold 2001.8ms.
Could have made more vs best arm: 0.023480 SOL. Could have lost less vs best arm: 0.023480 SOL. Hindsight MFE upper bound: 0.036739 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.001374 SOL (+1.04%); tier BASE; MFE +23.15%; MAE -11.86%; exit `PERSISTENT_DETERIORATION`; hold 1713.3ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.030449 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — LOSS -0.031034 SOL (-13.15%); tier MEDIUM; MFE +22.60%; MAE -20.55%; exit `EARLY_FLOW_INVALIDATION_-2`; hold 1852.7ms.
Could have made more vs best arm: 0.032407 SOL. Could have lost less vs best arm: 0.032407 SOL. Hindsight MFE upper bound: 0.053323 SOL. Post-exit higher/lower: True/True.

### Trade 40 — `BgVkD6uyfFpw74jvRBJ7npk6mvHozTFD7F2qYaThpump`

Best arm on this coin: **FULL_2S_CONTROL** (+0.099942 SOL)

**FULL_2S_CONTROL** — WIN +0.099942 SOL (+12.26%); tier EXCEPTIONAL; MFE +12.26%; MAE -6.36%; exit `FULL_2S_EXIT`; hold 2002.2ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.099942 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — WIN +0.073829 SOL (+9.32%); tier EXCEPTIONAL; MFE +12.39%; MAE -6.26%; exit `HARD_2S_REMAINDER_EXIT`; hold 2002.2ms.
Could have made more vs best arm: 0.026113 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.098206 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — WIN +0.025018 SOL (+3.80%); tier EXCEPTIONAL; MFE +24.98%; MAE -18.33%; exit `MAX_RUNNER_HOLD`; hold 16003.7ms.
Could have made more vs best arm: 0.074924 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.164356 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.044384 SOL (+6.08%); tier EXCEPTIONAL; MFE +12.75%; MAE -5.98%; exit `FLOW_BREAKDOWN_SCORE_-2`; hold 3012.7ms.
Could have made more vs best arm: 0.055558 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.093020 SOL. Post-exit higher/lower: False/True.

### Trade 41 — `8nnc4eAKe3phA5jrrSnWioMuKUdDMmGXpRHAq5e4pump`

Best arm on this coin: **V2_CURRENT** (-0.025017 SOL)

**FULL_2S_CONTROL** — LOSS -0.092148 SOL (-10.97%); tier EXCEPTIONAL; MFE +5.58%; MAE -14.48%; exit `FULL_2S_EXIT`; hold 2001.3ms.
Could have made more vs best arm: 0.067131 SOL. Could have lost less vs best arm: 0.067131 SOL. Hindsight MFE upper bound: 0.046829 SOL. Post-exit higher/lower: True/False.
**PARTIALS_2S** — LOSS -0.075574 SOL (-9.32%); tier EXCEPTIONAL; MFE +5.73%; MAE -14.36%; exit `HARD_2S_REMAINDER_EXIT`; hold 2001.3ms.
Could have made more vs best arm: 0.050557 SOL. Could have lost less vs best arm: 0.050557 SOL. Hindsight MFE upper bound: 0.046433 SOL. Post-exit higher/lower: True/False.
**V2_CURRENT** — LOSS -0.025017 SOL (-3.77%); tier EXCEPTIONAL; MFE +7.09%; MAE -21.48%; exit `MAX_RUNNER_HOLD`; hold 16003.4ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.047111 SOL. Post-exit higher/lower: True/False.
**FLOW_V2_1** — LOSS -0.079953 SOL (-10.79%); tier EXCEPTIONAL; MFE +6.09%; MAE -14.08%; exit `FLOW_2S_RED_EXIT_SCORE_-2`; hold 2001.4ms.
Could have made more vs best arm: 0.054936 SOL. Could have lost less vs best arm: 0.054936 SOL. Hindsight MFE upper bound: 0.045119 SOL. Post-exit higher/lower: True/False.

### Trade 42 — `Gov5thBP1uVKL6B22oxzqneastmPnrrdvjboJeaxpump`

Best arm on this coin: **FULL_2S_CONTROL** (+0.116481 SOL)

**FULL_2S_CONTROL** — WIN +0.116481 SOL (+44.56%); tier MEDIUM; MFE +44.56%; MAE -4.00%; exit `FULL_2S_EXIT`; hold 2001.7ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.116481 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — WIN +0.101467 SOL (+40.03%); tier MEDIUM; MFE +44.63%; MAE -3.96%; exit `HARD_2S_REMAINDER_EXIT`; hold 2001.7ms.
Could have made more vs best arm: 0.015013 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.113125 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — WIN +0.055087 SOL (+26.16%); tier MEDIUM; MFE +44.98%; MAE -3.75%; exit `MAX_RUNNER_HOLD`; hold 6003.2ms.
Could have made more vs best arm: 0.061393 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.094708 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.078429 SOL (+34.00%); tier MEDIUM; MFE +44.82%; MAE -3.85%; exit `FLOW_BREAKDOWN_SCORE_-2`; hold 2756.8ms.
Could have made more vs best arm: 0.038051 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.103374 SOL. Post-exit higher/lower: True/True.

### Trade 43 — `GM81eFK7witzfNMaJfQCak3JnpUPyCRQnfts5sW5pump`

Best arm on this coin: **FULL_2S_CONTROL** (+0.063135 SOL)

**FULL_2S_CONTROL** — WIN +0.063135 SOL (+23.32%); tier MEDIUM; MFE +23.83%; MAE -3.88%; exit `FULL_2S_EXIT`; hold 2001.0ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.064513 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.043181 SOL (+16.51%); tier MEDIUM; MFE +23.89%; MAE -3.84%; exit `HARD_2S_REMAINDER_EXIT`; hold 2001.0ms.
Could have made more vs best arm: 0.019954 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.062487 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.016008 SOL (+7.45%); tier MEDIUM; MFE +44.29%; MAE -3.64%; exit `MAX_RUNNER_HOLD`; hold 6002.4ms.
Could have made more vs best arm: 0.047127 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.095209 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — LOSS -0.002698 SOL (-1.14%); tier MEDIUM; MFE +24.03%; MAE -3.74%; exit `FLOW_BREAKDOWN_SCORE_-2`; hold 2254.9ms.
Could have made more vs best arm: 0.065832 SOL. Could have lost less vs best arm: 0.065832 SOL. Hindsight MFE upper bound: 0.056944 SOL. Post-exit higher/lower: True/True.

### Trade 44 — `GLdoi3cycMtX6hEfGPeqTEaXL7gthhw9qV85aBFupump`

Best arm on this coin: **FLOW_V2_1** (-0.029380 SOL)

**FULL_2S_CONTROL** — LOSS -0.116922 SOL (-42.40%); tier MEDIUM; MFE -2.92%; MAE -42.40%; exit `FULL_2S_EXIT`; hold 2002.7ms.
Could have made more vs best arm: 0.087541 SOL. Could have lost less vs best arm: 0.087541 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/False.
**PARTIALS_2S** — LOSS -0.080529 SOL (-30.38%); tier MEDIUM; MFE -2.87%; MAE -42.38%; exit `HARD_2S_REMAINDER_EXIT`; hold 2002.7ms.
Could have made more vs best arm: 0.051148 SOL. Could have lost less vs best arm: 0.051148 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/False.
**V2_CURRENT** — LOSS -0.041718 SOL (-19.29%); tier MEDIUM; MFE -2.66%; MAE -26.59%; exit `PERSISTENT_DETERIORATION_DEEP`; hold 1232.8ms.
Could have made more vs best arm: 0.012337 SOL. Could have lost less vs best arm: 0.012337 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/False.
**FLOW_V2_1** — LOSS -0.029380 SOL (-12.41%); tier MEDIUM; MFE -2.75%; MAE -16.78%; exit `EARLY_FLOW_INVALIDATION_-8`; hold 892.3ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/False.

### Trade 45 — `6HAGGJi8RRvaqXhxrVqm1UyDkrqCgzn3N2sVoAcjEtBT`

Best arm on this coin: **FULL_2S_CONTROL** (+0.029037 SOL)

**FULL_2S_CONTROL** — WIN +0.029037 SOL (+10.90%); tier MEDIUM; MFE +10.90%; MAE -3.97%; exit `FULL_2S_EXIT`; hold 2000.4ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.029037 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.018799 SOL (+7.27%); tier MEDIUM; MFE +10.94%; MAE -3.93%; exit `HARD_2S_REMAINDER_EXIT`; hold 2000.4ms.
Could have made more vs best arm: 0.010238 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.028302 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.014365 SOL (+6.75%); tier MEDIUM; MFE +11.20%; MAE -3.72%; exit `MAX_RUNNER_HOLD`; hold 6002.0ms.
Could have made more vs best arm: 0.014672 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.023843 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — LOSS -0.017915 SOL (-4.08%); tier HIGH; MFE +9.89%; MAE -4.81%; exit `FLOW_BREAKDOWN_SCORE_-3`; hold 6571.0ms.
Could have made more vs best arm: 0.046952 SOL. Could have lost less vs best arm: 0.046952 SOL. Hindsight MFE upper bound: 0.043466 SOL. Post-exit higher/lower: True/False.

### Trade 46 — `9RVj2A7jFZkNjU2ippEsQicNRdunwn8keV1XqteHpump`

Best arm on this coin: **FLOW_V2_1** (+0.071255 SOL)

**FULL_2S_CONTROL** — WIN +0.049020 SOL (+29.19%); tier BASE; MFE +29.19%; MAE -3.42%; exit `FULL_2S_EXIT`; hold 2002.3ms.
Could have made more vs best arm: 0.022235 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.049020 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.035682 SOL (+21.95%); tier BASE; MFE +29.21%; MAE -3.40%; exit `HARD_2S_REMAINDER_EXIT`; hold 2002.3ms.
Could have made more vs best arm: 0.035573 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.047495 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.053835 SOL (+40.24%); tier BASE; MFE +67.81%; MAE -3.31%; exit `MAX_RUNNER_HOLD`; hold 4001.2ms.
Could have made more vs best arm: 0.017419 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.090716 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.071255 SOL (+48.95%); tier BASE; MFE +67.73%; MAE -3.35%; exit `MAX_RUNNER_HOLD_SAFETY_CEILING`; hold 4001.2ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.098598 SOL. Post-exit higher/lower: True/True.

### Trade 47 — `Gs1iMtXWBjaKFD9m5GjT8zFU83vyso1hT4eVGKqypump`

Best arm on this coin: **V2_CURRENT** (-0.004539 SOL)

**FULL_2S_CONTROL** — LOSS -0.005928 SOL (-3.48%); tier BASE; MFE -3.48%; MAE -3.48%; exit `FULL_2S_EXIT`; hold 2000.9ms.
Could have made more vs best arm: 0.001389 SOL. Could have lost less vs best arm: 0.001389 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — LOSS -0.005553 SOL (-3.38%); tier BASE; MFE -3.46%; MAE -3.46%; exit `HARD_2S_REMAINDER_EXIT`; hold 2000.9ms.
Could have made more vs best arm: 0.001014 SOL. Could have lost less vs best arm: 0.001014 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — LOSS -0.004539 SOL (-3.33%); tier BASE; MFE -3.36%; MAE -3.36%; exit `MAX_RUNNER_HOLD`; hold 4000.1ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — LOSS -0.004992 SOL (-3.35%); tier BASE; MFE -3.40%; MAE -3.40%; exit `FLOW_2S_EXIT_SCORE_0`; hold 2001.0ms.
Could have made more vs best arm: 0.000453 SOL. Could have lost less vs best arm: 0.000453 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.

### Trade 48 — `AC3PrJLYDeHfbM96i9Ps7vCbgwV61LqjTMM3NvUXpump`

Best arm on this coin: **FULL_2S_CONTROL** (+0.007517 SOL)

**FULL_2S_CONTROL** — WIN +0.007517 SOL (+2.76%); tier MEDIUM; MFE +2.76%; MAE -3.77%; exit `FULL_2S_EXIT`; hold 2002.1ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.007517 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.004984 SOL (+1.90%); tier MEDIUM; MFE +2.80%; MAE -3.73%; exit `HARD_2S_REMAINDER_EXIT`; hold 2002.2ms.
Could have made more vs best arm: 0.002533 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.007359 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.005714 SOL (+2.62%); tier MEDIUM; MFE +34.88%; MAE -15.40%; exit `MAX_RUNNER_HOLD`; hold 6001.3ms.
Could have made more vs best arm: 0.001803 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.076028 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.006972 SOL (+1.56%); tier HIGH; MFE +2.00%; MAE -4.47%; exit `FLOW_2S_EXIT_SCORE_0`; hold 2002.2ms.
Could have made more vs best arm: 0.000545 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.008913 SOL. Post-exit higher/lower: True/True.

### Trade 49 — `7Ywix9c32xJVaW7UWUBn8QjnuX37uPYGDvcwBr3npump`

Best arm on this coin: **FLOW_V2_1** (-0.104478 SOL)

**FULL_2S_CONTROL** — LOSS -0.244300 SOL (-47.77%); tier HIGH; MFE +5.02%; MAE -47.77%; exit `FULL_2S_EXIT`; hold 2001.7ms.
Could have made more vs best arm: 0.139822 SOL. Could have lost less vs best arm: 0.139822 SOL. Hindsight MFE upper bound: 0.025695 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — LOSS -0.185744 SOL (-37.68%); tier HIGH; MFE +5.11%; MAE -47.73%; exit `HARD_2S_REMAINDER_EXIT`; hold 2001.7ms.
Could have made more vs best arm: 0.081266 SOL. Could have lost less vs best arm: 0.081266 SOL. Hindsight MFE upper bound: 0.025203 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — LOSS -0.130460 SOL (-31.85%); tier HIGH; MFE +5.51%; MAE -40.50%; exit `EMERGENCY_CATASTROPHIC_DRAWDOWN`; hold 965.9ms.
Could have made more vs best arm: 0.025982 SOL. Could have lost less vs best arm: 0.025982 SOL. Hindsight MFE upper bound: 0.022574 SOL. Post-exit higher/lower: False/True.
**FLOW_V2_1** — LOSS -0.104478 SOL (-23.34%); tier HIGH; MFE +5.33%; MAE -29.86%; exit `CATASTROPHIC_DRAWDOWN_28PCT`; hold 586.1ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.023860 SOL. Post-exit higher/lower: False/True.

### Trade 50 — `5An3vemn2cyvwmnhMXkLHeuFcJTc9cM6rddGgNVnpump`

Best arm on this coin: **V2_CURRENT** (-0.093050 SOL)

**FULL_2S_CONTROL** — LOSS -0.115628 SOL (-45.66%); tier MEDIUM; MFE -3.75%; MAE -48.45%; exit `FULL_2S_EXIT`; hold 2149.1ms.
Could have made more vs best arm: 0.022578 SOL. Could have lost less vs best arm: 0.022578 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — LOSS -0.112582 SOL (-45.38%); tier MEDIUM; MFE -3.73%; MAE -48.44%; exit `HARD_2S_REMAINDER_EXIT`; hold 2149.1ms.
Could have made more vs best arm: 0.019532 SOL. Could have lost less vs best arm: 0.019532 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — LOSS -0.093050 SOL (-44.73%); tier MEDIUM; MFE -3.57%; MAE -48.37%; exit `EMERGENCY_CATASTROPHIC_DRAWDOWN`; hold 370.3ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/True.
**FLOW_V2_1** — LOSS -0.098982 SOL (-42.96%); tier MEDIUM; MFE -3.66%; MAE -42.96%; exit `CATASTROPHIC_DRAWDOWN_28PCT`; hold 23.3ms.
Could have made more vs best arm: 0.005932 SOL. Could have lost less vs best arm: 0.005932 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/True.

### Trade 51 — `y7Zz5CZVGxrQtvHQnhsrrdvuVsHBrpwZHT7F52Gpump`

Best arm on this coin: **PARTIALS_2S** (+0.116466 SOL)

**FULL_2S_CONTROL** — WIN +0.111142 SOL (+72.89%); tier BASE; MFE +103.45%; MAE -3.48%; exit `FULL_2S_EXIT`; hold 2002.5ms.
Could have made more vs best arm: 0.005323 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.157745 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — WIN +0.116466 SOL (+77.95%); tier BASE; MFE +103.48%; MAE -3.46%; exit `HARD_2S_REMAINDER_EXIT`; hold 2002.5ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.154615 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — WIN +0.091611 SOL (+73.08%); tier BASE; MFE +103.75%; MAE -3.38%; exit `MAX_RUNNER_HOLD`; hold 4001.2ms.
Could have made more vs best arm: 0.024855 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.130054 SOL. Post-exit higher/lower: False/True.
**FLOW_V2_1** — WIN +0.108488 SOL (+78.01%); tier BASE; MFE +103.60%; MAE -3.42%; exit `FLOW_2S_EXIT_SCORE_-6`; hold 2002.6ms.
Could have made more vs best arm: 0.007978 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.144075 SOL. Post-exit higher/lower: False/True.

### Trade 52 — `2YZ3Kb1NbEPnSEBTt4abrA9RMLQCGdaDD5uSrJSA5vnS`

Best arm on this coin: **FULL_2S_CONTROL** (+0.280976 SOL)

**FULL_2S_CONTROL** — WIN +0.280976 SOL (+35.56%); tier EXCEPTIONAL; MFE +35.56%; MAE -10.46%; exit `FULL_2S_EXIT`; hold 2001.9ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.280976 SOL. Post-exit higher/lower: True/False.
**PARTIALS_2S** — WIN +0.217715 SOL (+28.05%); tier EXCEPTIONAL; MFE +35.66%; MAE -10.40%; exit `HARD_2S_REMAINDER_EXIT`; hold 2001.9ms.
Could have made more vs best arm: 0.063261 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.276758 SOL. Post-exit higher/lower: True/False.
**V2_CURRENT** — WIN +0.198050 SOL (+30.49%); tier EXCEPTIONAL; MFE +96.28%; MAE -9.86%; exit `MAX_RUNNER_HOLD`; hold 16001.2ms.
Could have made more vs best arm: 0.082926 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.625469 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.086695 SOL (+12.00%); tier EXCEPTIONAL; MFE +36.04%; MAE -10.17%; exit `FLOW_BREAKDOWN_SCORE_-5`; hold 3003.7ms.
Could have made more vs best arm: 0.194281 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.260385 SOL. Post-exit higher/lower: True/False.

### Trade 53 — `3kkLmsBjKTiqoYrQqZwRsJu6qYNkNiX1qdRHT41Kpump`

Best arm on this coin: **FLOW_V2_1** (-0.001306 SOL)

**FULL_2S_CONTROL** — LOSS -0.011693 SOL (-2.26%); tier HIGH; MFE -0.37%; MAE -4.65%; exit `FULL_2S_EXIT`; hold 2000.3ms.
Could have made more vs best arm: 0.010387 SOL. Could have lost less vs best arm: 0.010387 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — LOSS -0.007869 SOL (-1.58%); tier HIGH; MFE -0.29%; MAE -4.58%; exit `HARD_2S_REMAINDER_EXIT`; hold 2000.4ms.
Could have made more vs best arm: 0.006564 SOL. Could have lost less vs best arm: 0.006564 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — LOSS -0.108531 SOL (-25.87%); tier HIGH; MFE +0.03%; MAE -32.49%; exit `PERSISTENT_DETERIORATION_DEEP`; hold 6624.3ms.
Could have made more vs best arm: 0.107226 SOL. Could have lost less vs best arm: 0.107226 SOL. Hindsight MFE upper bound: 0.000137 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — LOSS -0.001306 SOL (-0.55%); tier MEDIUM; MFE +0.76%; MAE -3.58%; exit `FLOW_2S_EXIT_SCORE_-5`; hold 2000.4ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.001811 SOL. Post-exit higher/lower: True/True.

### Trade 54 — `4V6tNaf9Rja4azXApBWUZ6Yjfpjzy5bJdAnPedy6pump`

Best arm on this coin: **PARTIALS_2S** (-0.009497 SOL)

**FULL_2S_CONTROL** — LOSS -0.010337 SOL (-3.77%); tier MEDIUM; MFE -3.77%; MAE -4.28%; exit `FULL_2S_EXIT`; hold 2001.2ms.
Could have made more vs best arm: 0.000840 SOL. Could have lost less vs best arm: 0.000840 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/False.
**PARTIALS_2S** — LOSS -0.009497 SOL (-3.58%); tier MEDIUM; MFE -3.71%; MAE -4.23%; exit `HARD_2S_REMAINDER_EXIT`; hold 2001.2ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/False.
**V2_CURRENT** — LOSS -0.011056 SOL (-5.14%); tier MEDIUM; MFE -3.43%; MAE -5.96%; exit `MAX_RUNNER_HOLD`; hold 6001.8ms.
Could have made more vs best arm: 0.001559 SOL. Could have lost less vs best arm: 0.001559 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/False.
**FLOW_V2_1** — LOSS -0.011431 SOL (-4.80%); tier MEDIUM; MFE -3.56%; MAE -5.46%; exit `FLOW_BREAKDOWN_SCORE_-2`; hold 3012.1ms.
Could have made more vs best arm: 0.001935 SOL. Could have lost less vs best arm: 0.001935 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/False.

### Trade 55 — `4vf1NCh6w7HMn5oRNDzAbSB4suxjc1mMM9nnNyUrpump`

Best arm on this coin: **V2_CURRENT** (+0.009782 SOL)

**FULL_2S_CONTROL** — LOSS -0.006902 SOL (-4.04%); tier BASE; MFE +8.41%; MAE -4.04%; exit `FULL_2S_EXIT`; hold 2001.2ms.
Could have made more vs best arm: 0.016683 SOL. Could have lost less vs best arm: 0.016683 SOL. Hindsight MFE upper bound: 0.014374 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — LOSS -0.003530 SOL (-2.14%); tier BASE; MFE +8.43%; MAE -4.02%; exit `HARD_2S_REMAINDER_EXIT`; hold 2001.3ms.
Could have made more vs best arm: 0.013312 SOL. Could have lost less vs best arm: 0.013312 SOL. Hindsight MFE upper bound: 0.013933 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.009782 SOL (+7.31%); tier BASE; MFE +20.75%; MAE -3.91%; exit `MAX_RUNNER_HOLD`; hold 4002.7ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.027781 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — LOSS -0.003116 SOL (-2.10%); tier BASE; MFE +8.50%; MAE -3.96%; exit `FLOW_2S_EXIT_SCORE_-5`; hold 2001.3ms.
Could have made more vs best arm: 0.012898 SOL. Could have lost less vs best arm: 0.012898 SOL. Hindsight MFE upper bound: 0.012600 SOL. Post-exit higher/lower: True/True.

### Trade 56 — `EsFPMQ5q2TVUgwkz4dNbhnNQ1yMRCu13hF9PTGjo6d3d`

Best arm on this coin: **V2_CURRENT** (-0.000690 SOL)

**FULL_2S_CONTROL** — LOSS -0.012603 SOL (-4.62%); tier MEDIUM; MFE +12.25%; MAE -15.38%; exit `FULL_2S_EXIT`; hold 2001.4ms.
Could have made more vs best arm: 0.011913 SOL. Could have lost less vs best arm: 0.011913 SOL. Hindsight MFE upper bound: 0.033442 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — LOSS -0.003601 SOL (-1.36%); tier MEDIUM; MFE +12.30%; MAE -15.35%; exit `HARD_2S_REMAINDER_EXIT`; hold 2001.5ms.
Could have made more vs best arm: 0.002911 SOL. Could have lost less vs best arm: 0.002911 SOL. Hindsight MFE upper bound: 0.032477 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — LOSS -0.000690 SOL (-0.32%); tier MEDIUM; MFE +12.56%; MAE -15.17%; exit `MAX_RUNNER_HOLD`; hold 6004.0ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.026988 SOL. Post-exit higher/lower: False/True.
**FLOW_V2_1** — LOSS -0.020963 SOL (-8.85%); tier MEDIUM; MFE +12.44%; MAE -15.25%; exit `EARLY_FLOW_INVALIDATION_-6`; hold 1940.5ms.
Could have made more vs best arm: 0.020273 SOL. Could have lost less vs best arm: 0.020273 SOL. Hindsight MFE upper bound: 0.029468 SOL. Post-exit higher/lower: True/True.

### Trade 57 — `FbhjhmFaPtjEEECMbUJifjYLqcr2nyFf8qWSELVYpump`

Best arm on this coin: **V2_CURRENT** (+0.093168 SOL)

**FULL_2S_CONTROL** — WIN +0.061310 SOL (+12.02%); tier HIGH; MFE +14.81%; MAE -7.72%; exit `FULL_2S_EXIT`; hold 2002.4ms.
Could have made more vs best arm: 0.031858 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.075561 SOL. Post-exit higher/lower: True/False.
**PARTIALS_2S** — WIN +0.047891 SOL (+9.68%); tier HIGH; MFE +14.89%; MAE -7.66%; exit `HARD_2S_REMAINDER_EXIT`; hold 2002.4ms.
Could have made more vs best arm: 0.045277 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.073683 SOL. Post-exit higher/lower: True/False.
**V2_CURRENT** — WIN +0.093168 SOL (+23.12%); tier HIGH; MFE +72.12%; MAE -7.30%; exit `MAX_RUNNER_HOLD`; hold 10001.7ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.290602 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.001973 SOL (+0.84%); tier MEDIUM; MFE +16.23%; MAE -6.66%; exit `PROFIT_FLOOR_PEAK_0.162_FLOOR_0.042_SCORE_-1`; hold 2502.9ms.
Could have made more vs best arm: 0.091195 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.038174 SOL. Post-exit higher/lower: True/False.

### Trade 58 — `7KyTKBJeaMM3bGJFxmhD7UKQNNyFiMAkcqAGxJp9pump`

Best arm on this coin: **V2_CURRENT** (-0.007871 SOL)

**FULL_2S_CONTROL** — LOSS -0.010916 SOL (-3.94%); tier MEDIUM; MFE -3.94%; MAE -3.94%; exit `FULL_2S_EXIT`; hold 2001.3ms.
Could have made more vs best arm: 0.003044 SOL. Could have lost less vs best arm: 0.003044 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — LOSS -0.009855 SOL (-3.68%); tier MEDIUM; MFE -3.90%; MAE -3.90%; exit `HARD_2S_REMAINDER_EXIT`; hold 2001.3ms.
Could have made more vs best arm: 0.001984 SOL. Could have lost less vs best arm: 0.001984 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — LOSS -0.007871 SOL (-3.54%); tier MEDIUM; MFE -3.70%; MAE -3.70%; exit `MAX_RUNNER_HOLD`; hold 6003.8ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/True.
**FLOW_V2_1** — LOSS -0.008424 SOL (-3.58%); tier MEDIUM; MFE -3.76%; MAE -3.76%; exit `FLOW_2S_EXIT_SCORE_0`; hold 2001.4ms.
Could have made more vs best arm: 0.000552 SOL. Could have lost less vs best arm: 0.000552 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/True.

### Trade 59 — `ShFAd6ir7edCAH6aSYDXaCrPLd9sb16TCcHW6UMpump`

Best arm on this coin: **PARTIALS_2S** (-0.049400 SOL)

**FULL_2S_CONTROL** — LOSS -0.060663 SOL (-11.72%); tier HIGH; MFE +1.84%; MAE -13.74%; exit `FULL_2S_EXIT`; hold 2002.2ms.
Could have made more vs best arm: 0.011264 SOL. Could have lost less vs best arm: 0.011264 SOL. Hindsight MFE upper bound: 0.009550 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — LOSS -0.049400 SOL (-9.87%); tier HIGH; MFE +1.92%; MAE -13.68%; exit `HARD_2S_REMAINDER_EXIT`; hold 2002.2ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.009611 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — LOSS -0.100110 SOL (-24.08%); tier HIGH; MFE +2.29%; MAE -29.39%; exit `PERSISTENT_DETERIORATION_DEEP`; hold 4655.8ms.
Could have made more vs best arm: 0.050710 SOL. Could have lost less vs best arm: 0.050710 SOL. Hindsight MFE upper bound: 0.009530 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — LOSS -0.049789 SOL (-11.32%); tier HIGH; MFE +2.19%; MAE -13.47%; exit `EARLY_FLOW_INVALIDATION_-7`; hold 1856.8ms.
Could have made more vs best arm: 0.000389 SOL. Could have lost less vs best arm: 0.000389 SOL. Hindsight MFE upper bound: 0.009618 SOL. Post-exit higher/lower: True/True.

### Trade 60 — `EVRWzt55U6mEvj4S88cMbCuK3Ax1GBJLukJ6ZpYhpump`

Best arm on this coin: **FULL_2S_CONTROL** (+0.032337 SOL)

**FULL_2S_CONTROL** — WIN +0.032337 SOL (+19.08%); tier BASE; MFE +19.08%; MAE -3.50%; exit `FULL_2S_EXIT`; hold 2001.9ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.032337 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.020582 SOL (+12.52%); tier BASE; MFE +19.11%; MAE -3.48%; exit `HARD_2S_REMAINDER_EXIT`; hold 2001.9ms.
Could have made more vs best arm: 0.011755 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.031395 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.005968 SOL (+4.47%); tier BASE; MFE +21.96%; MAE -3.37%; exit `MAX_RUNNER_HOLD`; hold 4000.8ms.
Could have made more vs best arm: 0.026369 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.029337 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.013609 SOL (+5.90%); tier MEDIUM; MFE +18.73%; MAE -3.76%; exit `FLOW_BREAKDOWN_SCORE_-2`; hold 2756.7ms.
Could have made more vs best arm: 0.018728 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.043206 SOL. Post-exit higher/lower: True/True.

### Trade 61 — `Fpaw4hawiV3tgJBdtHc4Wt2PWNgLxt789TBPSidppump`

Best arm on this coin: **FLOW_V2_1** (-0.032423 SOL)

**FULL_2S_CONTROL** — LOSS -0.045356 SOL (-16.57%); tier MEDIUM; MFE -3.96%; MAE -16.57%; exit `FULL_2S_EXIT`; hold 2002.5ms.
Could have made more vs best arm: 0.012933 SOL. Could have lost less vs best arm: 0.012933 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — LOSS -0.037251 SOL (-14.08%); tier MEDIUM; MFE -3.92%; MAE -16.53%; exit `HARD_2S_REMAINDER_EXIT`; hold 2002.5ms.
Could have made more vs best arm: 0.004828 SOL. Could have lost less vs best arm: 0.004828 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — LOSS -0.058208 SOL (-27.18%); tier MEDIUM; MFE -3.70%; MAE -35.21%; exit `EMERGENCY_CATASTROPHIC_DRAWDOWN`; hold 5054.9ms.
Could have made more vs best arm: 0.025785 SOL. Could have lost less vs best arm: 0.025785 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/True.
**FLOW_V2_1** — LOSS -0.032423 SOL (-13.99%); tier MEDIUM; MFE -3.77%; MAE -16.41%; exit `EARLY_FLOW_INVALIDATION_-3`; hold 1955.9ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/True.

### Trade 62 — `J6LUMYY6tFKXHmbG24PD9PmV4YnXzgMehXfuZ5ovpump`

Best arm on this coin: **V2_CURRENT** (+0.017969 SOL)

**FULL_2S_CONTROL** — LOSS -0.008298 SOL (-3.07%); tier MEDIUM; MFE +12.45%; MAE -3.96%; exit `FULL_2S_EXIT`; hold 2003.9ms.
Could have made more vs best arm: 0.026266 SOL. Could have lost less vs best arm: 0.026266 SOL. Hindsight MFE upper bound: 0.033645 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — LOSS -0.007317 SOL (-2.80%); tier MEDIUM; MFE +12.50%; MAE -3.92%; exit `HARD_2S_REMAINDER_EXIT`; hold 2003.9ms.
Could have made more vs best arm: 0.025286 SOL. Could have lost less vs best arm: 0.025286 SOL. Hindsight MFE upper bound: 0.032705 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.017969 SOL (+8.58%); tier MEDIUM; MFE +17.27%; MAE -3.69%; exit `MAX_RUNNER_HOLD`; hold 6002.6ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.036185 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — LOSS -0.006163 SOL (-2.69%); tier MEDIUM; MFE +12.69%; MAE -3.78%; exit `FLOW_2S_EXIT_SCORE_0`; hold 2004.0ms.
Could have made more vs best arm: 0.024132 SOL. Could have lost less vs best arm: 0.024132 SOL. Hindsight MFE upper bound: 0.029071 SOL. Post-exit higher/lower: True/True.

### Trade 63 — `AJp348N4JJARCh6bziqKKnZNktzqHnqpfDynVk5nPdsR`

Best arm on this coin: **FLOW_V2_1** (-0.027119 SOL)

**FULL_2S_CONTROL** — LOSS -0.138954 SOL (-27.50%); tier HIGH; MFE +9.17%; MAE -27.50%; exit `FULL_2S_EXIT`; hold 2002.9ms.
Could have made more vs best arm: 0.111836 SOL. Could have lost less vs best arm: 0.111836 SOL. Hindsight MFE upper bound: 0.046320 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — LOSS -0.106620 SOL (-21.79%); tier HIGH; MFE +9.24%; MAE -27.45%; exit `HARD_2S_REMAINDER_EXIT`; hold 2002.9ms.
Could have made more vs best arm: 0.079501 SOL. Could have lost less vs best arm: 0.079501 SOL. Hindsight MFE upper bound: 0.045218 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — LOSS -0.072198 SOL (-18.25%); tier HIGH; MFE +9.67%; MAE -28.56%; exit `PERSISTENT_DETERIORATION_DEEP`; hold 5544.2ms.
Could have made more vs best arm: 0.045079 SOL. Could have lost less vs best arm: 0.045079 SOL. Hindsight MFE upper bound: 0.038247 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — LOSS -0.027119 SOL (-11.86%); tier MEDIUM; MFE +10.41%; MAE -17.42%; exit `EARLY_FLOW_INVALIDATION_-4`; hold 1297.7ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.023807 SOL. Post-exit higher/lower: True/True.

### Trade 64 — `DhXzZ3yqgF4TuxWFXK3YBGverUUi4WvZ5B16z6pLwgKA`

Best arm on this coin: **V2_CURRENT** (+0.136054 SOL)

**FULL_2S_CONTROL** — WIN +0.005752 SOL (+1.19%); tier HIGH; MFE +3.36%; MAE -13.48%; exit `FULL_2S_EXIT`; hold 2003.4ms.
Could have made more vs best arm: 0.130302 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.016275 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — LOSS -0.006794 SOL (-1.44%); tier HIGH; MFE +3.41%; MAE -13.44%; exit `HARD_2S_REMAINDER_EXIT`; hold 2003.4ms.
Could have made more vs best arm: 0.142848 SOL. Could have lost less vs best arm: 0.142848 SOL. Hindsight MFE upper bound: 0.016128 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.136054 SOL (+35.36%); tier HIGH; MFE +55.35%; MAE -16.21%; exit `MAX_RUNNER_HOLD`; hold 10002.3ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.212939 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — LOSS -0.004746 SOL (-2.10%); tier MEDIUM; MFE +4.44%; MAE -12.62%; exit `FLOW_2S_EXIT_SCORE_-2`; hold 2003.5ms.
Could have made more vs best arm: 0.140800 SOL. Could have lost less vs best arm: 0.140800 SOL. Hindsight MFE upper bound: 0.010056 SOL. Post-exit higher/lower: True/True.

### Trade 65 — `4erwXrt4QpNbSpXr2MaJiyYk7J3AbbpXQPA7qGcRpump`

Best arm on this coin: **FULL_2S_CONTROL** (+0.106149 SOL)

**FULL_2S_CONTROL** — WIN +0.106149 SOL (+41.01%); tier MEDIUM; MFE +41.01%; MAE -3.31%; exit `FULL_2S_EXIT`; hold 2000.6ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.106149 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — WIN +0.070454 SOL (+27.97%); tier MEDIUM; MFE +41.04%; MAE -3.29%; exit `HARD_2S_REMAINDER_EXIT`; hold 2000.6ms.
Could have made more vs best arm: 0.035694 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.103382 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — WIN +0.042199 SOL (+19.53%); tier MEDIUM; MFE +59.82%; MAE -3.21%; exit `MAX_RUNNER_HOLD`; hold 6003.9ms.
Could have made more vs best arm: 0.063950 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.129265 SOL. Post-exit higher/lower: False/True.
**FLOW_V2_1** — WIN +0.042305 SOL (+18.71%); tier MEDIUM; MFE +59.78%; MAE -3.24%; exit `FLOW_BREAKDOWN_SCORE_-5`; hold 3258.4ms.
Could have made more vs best arm: 0.063844 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.135179 SOL. Post-exit higher/lower: False/True.

### Trade 66 — `2WA9CLPypQuC5CZgPrayvqX4fxv2d3AXSE6dJ5Q4FVkf`

Best arm on this coin: **FLOW_V2_1** (-0.099371 SOL)

**FULL_2S_CONTROL** — LOSS -0.377813 SOL (-45.22%); tier EXCEPTIONAL; MFE -6.43%; MAE -45.22%; exit `FULL_2S_EXIT`; hold 2003.3ms.
Could have made more vs best arm: 0.278442 SOL. Could have lost less vs best arm: 0.278442 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/False.
**PARTIALS_2S** — LOSS -0.297599 SOL (-36.97%); tier EXCEPTIONAL; MFE -6.30%; MAE -45.15%; exit `HARD_2S_REMAINDER_EXIT`; hold 2003.3ms.
Could have made more vs best arm: 0.198227 SOL. Could have lost less vs best arm: 0.198227 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/False.
**V2_CURRENT** — LOSS -0.217840 SOL (-31.77%); tier EXCEPTIONAL; MFE -5.76%; MAE -38.72%; exit `EMERGENCY_CATASTROPHIC_DRAWDOWN`; hold 1011.4ms.
Could have made more vs best arm: 0.118468 SOL. Could have lost less vs best arm: 0.118468 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/False.
**FLOW_V2_1** — LOSS -0.099371 SOL (-13.85%); tier EXCEPTIONAL; MFE -5.90%; MAE -16.42%; exit `EARLY_FLOW_INVALIDATION_-2`; hold 998.5ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/False.

### Trade 67 — `E6EnEEy6Pt2917K4tz3MLsgewJT8XSuWLYkY8rfNpump`

Best arm on this coin: **V2_CURRENT** (+0.012563 SOL)

**FULL_2S_CONTROL** — LOSS -0.012121 SOL (-5.11%); tier MEDIUM; MFE -2.00%; MAE -5.11%; exit `FULL_2S_EXIT`; hold 2001.5ms.
Could have made more vs best arm: 0.024684 SOL. Could have lost less vs best arm: 0.024684 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — LOSS -0.009479 SOL (-4.06%); tier MEDIUM; MFE -1.99%; MAE -5.10%; exit `HARD_2S_REMAINDER_EXIT`; hold 2001.5ms.
Could have made more vs best arm: 0.022042 SOL. Could have lost less vs best arm: 0.022042 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.012563 SOL (+6.22%); tier MEDIUM; MFE +35.97%; MAE -32.00%; exit `MAX_RUNNER_HOLD`; hold 6001.9ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.072659 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — LOSS -0.020213 SOL (-4.87%); tier HIGH; MFE -2.60%; MAE -5.68%; exit `FLOW_2S_EXIT_SCORE_0`; hold 2001.6ms.
Could have made more vs best arm: 0.032776 SOL. Could have lost less vs best arm: 0.032776 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.

### Trade 68 — `DgXWC1prkWs9aD9KNSae6YwKBKCKRxasUaPe4QUUpump`

Best arm on this coin: **FLOW_V2_1** (+0.018551 SOL)

**FULL_2S_CONTROL** — WIN +0.016359 SOL (+11.08%); tier BASE; MFE +11.08%; MAE -3.36%; exit `FULL_2S_EXIT`; hold 2003.9ms.
Could have made more vs best arm: 0.002191 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.016359 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.011992 SOL (+8.23%); tier BASE; MFE +11.09%; MAE -3.35%; exit `HARD_2S_REMAINDER_EXIT`; hold 2004.0ms.
Could have made more vs best arm: 0.006558 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.016153 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.013774 SOL (+10.86%); tier BASE; MFE +24.02%; MAE -3.29%; exit `MAX_RUNNER_HOLD`; hold 4001.6ms.
Could have made more vs best arm: 0.004776 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.030477 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.018551 SOL (+8.43%); tier MEDIUM; MFE +23.54%; MAE -3.63%; exit `PROFIT_FLOOR_PEAK_0.235_FLOOR_0.115_SCORE_1`; hold 3771.4ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.051780 SOL. Post-exit higher/lower: True/True.

### Trade 69 — `AtRxKk1x51sSA9fdMpUxgkzH6L3SCXdMnAo294tipump`

Best arm on this coin: **FLOW_V2_1** (-0.002258 SOL)

**FULL_2S_CONTROL** — LOSS -0.002420 SOL (-1.63%); tier BASE; MFE +0.01%; MAE -3.14%; exit `FULL_2S_EXIT`; hold 2002.5ms.
Could have made more vs best arm: 0.000162 SOL. Could have lost less vs best arm: 0.000162 SOL. Hindsight MFE upper bound: 0.000017 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — LOSS -0.002390 SOL (-1.63%); tier BASE; MFE +0.02%; MAE -3.14%; exit `HARD_2S_REMAINDER_EXIT`; hold 2002.6ms.
Could have made more vs best arm: 0.000132 SOL. Could have lost less vs best arm: 0.000132 SOL. Hindsight MFE upper bound: 0.000023 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — LOSS -0.033935 SOL (-26.60%); tier BASE; MFE +0.05%; MAE -37.23%; exit `EMERGENCY_CATASTROPHIC_DRAWDOWN`; hold 2024.7ms.
Could have made more vs best arm: 0.031676 SOL. Could have lost less vs best arm: 0.031676 SOL. Hindsight MFE upper bound: 0.000060 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — LOSS -0.002258 SOL (-1.63%); tier BASE; MFE +0.03%; MAE -3.12%; exit `FLOW_2S_EXIT_SCORE_0`; hold 2002.6ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.000041 SOL. Post-exit higher/lower: True/True.

### Trade 70 — `FFdJxtrG94uuhKuZQEMrftQb6hj7hy3bF3azdRUsvGg8`

Best arm on this coin: **V2_CURRENT** (+0.118893 SOL)

**FULL_2S_CONTROL** — LOSS -0.026905 SOL (-6.05%); tier HIGH; MFE +5.45%; MAE -8.92%; exit `FULL_2S_EXIT`; hold 2002.8ms.
Could have made more vs best arm: 0.145798 SOL. Could have lost less vs best arm: 0.145798 SOL. Hindsight MFE upper bound: 0.024248 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — LOSS -0.019063 SOL (-4.35%); tier HIGH; MFE +5.48%; MAE -8.89%; exit `HARD_2S_REMAINDER_EXIT`; hold 2002.8ms.
Could have made more vs best arm: 0.137956 SOL. Could have lost less vs best arm: 0.137956 SOL. Hindsight MFE upper bound: 0.024028 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.118893 SOL (+31.48%); tier HIGH; MFE +54.34%; MAE -17.07%; exit `MAX_RUNNER_HOLD`; hold 10000.7ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.205229 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — LOSS -0.018084 SOL (-8.17%); tier MEDIUM; MFE +6.50%; MAE -12.67%; exit `EARLY_FLOW_INVALIDATION_-4`; hold 2448.6ms.
Could have made more vs best arm: 0.136977 SOL. Could have lost less vs best arm: 0.136977 SOL. Hindsight MFE upper bound: 0.014371 SOL. Post-exit higher/lower: True/True.

### Trade 71 — `3qDNVTdugeSJ3eRcR7RKwbSeGTnTraqDPDJouU3upump`

Best arm on this coin: **FULL_2S_CONTROL** (+0.207712 SOL)

**FULL_2S_CONTROL** — WIN +0.207712 SOL (+88.35%); tier MEDIUM; MFE +88.35%; MAE -3.81%; exit `FULL_2S_EXIT`; hold 2001.1ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.207712 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.195679 SOL (+84.25%); tier MEDIUM; MFE +88.38%; MAE -3.80%; exit `HARD_2S_REMAINDER_EXIT`; hold 2001.1ms.
Could have made more vs best arm: 0.012033 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.205259 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.163950 SOL (+77.73%); tier MEDIUM; MFE +129.95%; MAE -3.70%; exit `MAX_RUNNER_HOLD`; hold 6003.4ms.
Could have made more vs best arm: 0.043761 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.274090 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.114764 SOL (+83.54%); tier BASE; MFE +89.39%; MAE -3.40%; exit `FLOW_BREAKDOWN_SCORE_-2`; hold 2254.6ms.
Could have made more vs best arm: 0.092948 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.122805 SOL. Post-exit higher/lower: True/True.

### Trade 72 — `GPhzxaxCVLjAtNQYSWG2D66vJ847hoUpw5ohe4XYQ7EA`

Best arm on this coin: **PARTIALS_2S** (+0.011614 SOL)

**FULL_2S_CONTROL** — LOSS -0.001119 SOL (-0.24%); tier HIGH; MFE +11.54%; MAE -9.84%; exit `FULL_2S_EXIT`; hold 2001.4ms.
Could have made more vs best arm: 0.012733 SOL. Could have lost less vs best arm: 0.012733 SOL. Hindsight MFE upper bound: 0.054460 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — WIN +0.011614 SOL (+2.50%); tier HIGH; MFE +11.58%; MAE -9.81%; exit `HARD_2S_REMAINDER_EXIT`; hold 2001.4ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.053804 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — WIN +0.004645 SOL (+1.11%); tier HIGH; MFE +28.72%; MAE -17.27%; exit `PERSISTENT_DETERIORATION`; hold 7469.1ms.
Could have made more vs best arm: 0.006969 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.120629 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.004344 SOL (+1.90%); tier MEDIUM; MFE +12.77%; MAE -8.91%; exit `FLOW_2S_EXIT_SCORE_-3`; hold 2001.5ms.
Could have made more vs best arm: 0.007270 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.029235 SOL. Post-exit higher/lower: False/True.

### Trade 73 — `GejJfwcT7KM59Qc21raAGR3spDpRdqAizKKa7MrDYi4P`

Best arm on this coin: **FULL_2S_CONTROL** (+1.172079 SOL)

**FULL_2S_CONTROL** — WIN +1.172079 SOL (+248.42%); tier HIGH; MFE +248.42%; MAE -4.81%; exit `FULL_2S_EXIT`; hold 2000.6ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 1.172079 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.955895 SOL (+204.88%); tier HIGH; MFE +248.55%; MAE -4.78%; exit `HARD_2S_REMAINDER_EXIT`; hold 2000.6ms.
Could have made more vs best arm: 0.216183 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 1.159621 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.472189 SOL (+112.22%); tier HIGH; MFE +343.87%; MAE -4.57%; exit `MAX_RUNNER_HOLD`; hold 10002.4ms.
Could have made more vs best arm: 0.699890 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 1.446921 SOL. Post-exit higher/lower: True/False.
**FLOW_V2_1** — WIN +0.435431 SOL (+189.87%); tier MEDIUM; MFE +317.31%; MAE -3.72%; exit `FLOW_BREAKDOWN_SCORE_-3`; hold 4019.1ms.
Could have made more vs best arm: 0.736648 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.727691 SOL. Post-exit higher/lower: True/True.

### Trade 74 — `2tPypwHvWFSiX9YVi6sFT2fSSMREU4G78MxqwD4Qpump`

Best arm on this coin: **V2_CURRENT** (+0.031463 SOL)

**FULL_2S_CONTROL** — WIN +0.006815 SOL (+1.97%); tier MEDIUM; MFE +3.68%; MAE -4.07%; exit `FULL_2S_EXIT`; hold 2002.0ms.
Could have made more vs best arm: 0.024648 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.012728 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.007155 SOL (+2.20%); tier MEDIUM; MFE +3.78%; MAE -3.99%; exit `HARD_2S_REMAINDER_EXIT`; hold 2002.0ms.
Could have made more vs best arm: 0.024308 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.012281 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.031463 SOL (+12.00%); tier MEDIUM; MFE +27.86%; MAE -5.12%; exit `MAX_RUNNER_HOLD`; hold 6003.3ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.073052 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.005224 SOL (+1.05%); tier HIGH; MFE +3.00%; MAE -4.69%; exit `FLOW_2S_EXIT_SCORE_-2`; hold 2002.1ms.
Could have made more vs best arm: 0.026240 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.014877 SOL. Post-exit higher/lower: True/True.

### Trade 75 — `G2SKqbENwBTM2apeNmEt5bjVEMbyo2aiUbvse5cmKnqs`

Best arm on this coin: **FULL_2S_CONTROL** (+0.169632 SOL)

**FULL_2S_CONTROL** — WIN +0.169632 SOL (+26.15%); tier HIGH; MFE +52.35%; MAE -7.58%; exit `FULL_2S_EXIT`; hold 2004.1ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.339552 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.131276 SOL (+21.48%); tier HIGH; MFE +52.64%; MAE -7.42%; exit `HARD_2S_REMAINDER_EXIT`; hold 2004.1ms.
Could have made more vs best arm: 0.038356 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.321667 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.164201 SOL (+33.08%); tier HIGH; MFE +59.91%; MAE -6.94%; exit `MAX_RUNNER_HOLD`; hold 10002.0ms.
Could have made more vs best arm: 0.005431 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.297327 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.054175 SOL (+20.48%); tier MEDIUM; MFE +55.39%; MAE -5.98%; exit `FLOW_2S_EXIT_SCORE_-4`; hold 2004.3ms.
Could have made more vs best arm: 0.115457 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.146551 SOL. Post-exit higher/lower: True/True.

### Trade 76 — `A6V6BVUUTzdMQwzekYQTQQyKur5sdUUbjB6ZBxVm89hU`

Best arm on this coin: **FLOW_V2_1** (-0.023576 SOL)

**FULL_2S_CONTROL** — LOSS -0.131014 SOL (-19.44%); tier HIGH; MFE -0.00%; MAE -19.44%; exit `FULL_2S_EXIT`; hold 2004.1ms.
Could have made more vs best arm: 0.107438 SOL. Could have lost less vs best arm: 0.107438 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — LOSS -0.094551 SOL (-14.99%); tier HIGH; MFE +0.20%; MAE -19.28%; exit `HARD_2S_REMAINDER_EXIT`; hold 2004.2ms.
Could have made more vs best arm: 0.070975 SOL. Could have lost less vs best arm: 0.070975 SOL. Hindsight MFE upper bound: 0.001252 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — LOSS -0.058127 SOL (-11.16%); tier HIGH; MFE +0.71%; MAE -14.54%; exit `PERSISTENT_DETERIORATION`; hold 1291.7ms.
Could have made more vs best arm: 0.034551 SOL. Could have lost less vs best arm: 0.034551 SOL. Hindsight MFE upper bound: 0.003688 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — LOSS -0.023576 SOL (-8.77%); tier MEDIUM; MFE +1.87%; MAE -13.60%; exit `EARLY_FLOW_INVALIDATION_-6`; hold 1291.8ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.005020 SOL. Post-exit higher/lower: True/True.

### Trade 77 — `FMLzFciJ2BXEpxeuby2qCTDYiJBwqfJFyW9wY21f5Mcd`

Best arm on this coin: **V2_CURRENT** (+0.144986 SOL)

**FULL_2S_CONTROL** — WIN +0.005062 SOL (+0.77%); tier HIGH; MFE +7.66%; MAE -7.65%; exit `FULL_2S_EXIT`; hold 2408.4ms.
Could have made more vs best arm: 0.139924 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.050112 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — LOSS -0.002546 SOL (-0.41%); tier HIGH; MFE +7.82%; MAE -7.52%; exit `HARD_2S_REMAINDER_EXIT`; hold 2408.4ms.
Could have made more vs best arm: 0.147532 SOL. Could have lost less vs best arm: 0.147532 SOL. Hindsight MFE upper bound: 0.048203 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.144986 SOL (+28.30%); tier HIGH; MFE +78.50%; MAE -19.69%; exit `MAX_RUNNER_HOLD`; hold 10001.8ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.402081 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — LOSS -0.000371 SOL (-0.14%); tier MEDIUM; MFE +9.29%; MAE -6.31%; exit `FLOW_2S_EXIT_SCORE_-3`; hold 2408.5ms.
Could have made more vs best arm: 0.145357 SOL. Could have lost less vs best arm: 0.145357 SOL. Hindsight MFE upper bound: 0.024806 SOL. Post-exit higher/lower: True/True.

### Trade 78 — `4hbQepwrPiNsXTPm7eTPW9Tt54Sm1VfRFj3Wpvxibo1s`

Best arm on this coin: **V2_CURRENT** (+0.099717 SOL)

**FULL_2S_CONTROL** — WIN +0.055205 SOL (+13.96%); tier MEDIUM; MFE +48.02%; MAE -4.55%; exit `FULL_2S_EXIT`; hold 2005.7ms.
Could have made more vs best arm: 0.044512 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.189951 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.071546 SOL (+19.18%); tier MEDIUM; MFE +48.21%; MAE -4.44%; exit `HARD_2S_REMAINDER_EXIT`; hold 2005.7ms.
Could have made more vs best arm: 0.028171 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.179867 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.099717 SOL (+30.67%); tier MEDIUM; MFE +76.68%; MAE -4.21%; exit `MAX_RUNNER_HOLD`; hold 6002.6ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.249348 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.036324 SOL (+20.02%); tier BASE; MFE +49.78%; MAE -3.56%; exit `FLOW_2S_EXIT_SCORE_-2`; hold 2005.9ms.
Could have made more vs best arm: 0.063393 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.090317 SOL. Post-exit higher/lower: True/True.

### Trade 79 — `Ao99fFxPAz8ecGw13CcAXo9yWAN1HW6RLF2ZoBNupump`

Best arm on this coin: **PARTIALS_2S** (+0.475947 SOL)

**FULL_2S_CONTROL** — WIN +0.471528 SOL (+43.18%); tier EXCEPTIONAL; MFE +57.01%; MAE -7.97%; exit `FULL_2S_EXIT`; hold 2000.2ms.
Could have made more vs best arm: 0.004419 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.622546 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — WIN +0.475947 SOL (+46.35%); tier EXCEPTIONAL; MFE +57.61%; MAE -7.66%; exit `HARD_2S_REMAINDER_EXIT`; hold 2000.3ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.591617 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — WIN +0.352693 SOL (+39.63%); tier EXCEPTIONAL; MFE +58.89%; MAE -11.52%; exit `PERSISTENT_DETERIORATION`; hold 10195.5ms.
Could have made more vs best arm: 0.123255 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.524129 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.250653 SOL (+50.07%); tier HIGH; MFE +62.63%; MAE -5.12%; exit `FLOW_2S_EXIT_SCORE_1`; hold 2000.4ms.
Could have made more vs best arm: 0.225294 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.313567 SOL. Post-exit higher/lower: False/True.

### Trade 80 — `5zoK779SmQRrh9zQvdJG4wKDwKhex5hy3TnD9UMepump`

Best arm on this coin: **FULL_2S_CONTROL** (+0.238880 SOL)

**FULL_2S_CONTROL** — WIN +0.238880 SOL (+19.52%); tier EXCEPTIONAL; MFE +19.86%; MAE -12.01%; exit `FULL_2S_EXIT`; hold 2000.9ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.242979 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.175393 SOL (+15.07%); tier EXCEPTIONAL; MFE +20.17%; MAE -11.80%; exit `HARD_2S_REMAINDER_EXIT`; hold 2001.0ms.
Could have made more vs best arm: 0.063487 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.234734 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — LOSS -0.018052 SOL (-1.80%); tier EXCEPTIONAL; MFE +21.02%; MAE -12.42%; exit `PERSISTENT_DETERIORATION`; hold 4427.0ms.
Could have made more vs best arm: 0.256932 SOL. Could have lost less vs best arm: 0.256932 SOL. Hindsight MFE upper bound: 0.210837 SOL. Post-exit higher/lower: True/False.
**FLOW_V2_1** — WIN +0.138168 SOL (+15.25%); tier EXCEPTIONAL; MFE +21.54%; MAE -10.87%; exit `FLOW_BREAKDOWN_SCORE_-5`; hold 3010.6ms.
Could have made more vs best arm: 0.100712 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.195147 SOL. Post-exit higher/lower: True/True.

### Trade 81 — `5cpGpn3spQ9m98mrPCzKG5NzeRqbNVYFj7GeoGSipump`

Best arm on this coin: **FULL_2S_CONTROL** (+0.147113 SOL)

**FULL_2S_CONTROL** — WIN +0.147113 SOL (+11.46%); tier EXCEPTIONAL; MFE +40.53%; MAE -8.86%; exit `FULL_2S_EXIT`; hold 2003.4ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.520198 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — WIN +0.107411 SOL (+8.89%); tier EXCEPTIONAL; MFE +41.14%; MAE -8.51%; exit `HARD_2S_REMAINDER_EXIT`; hold 2003.4ms.
Could have made more vs best arm: 0.039702 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.496788 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — WIN +0.082053 SOL (+8.22%); tier EXCEPTIONAL; MFE +42.83%; MAE -27.05%; exit `MAX_RUNNER_HOLD`; hold 16003.3ms.
Could have made more vs best arm: 0.065060 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.427711 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.006497 SOL (+1.15%); tier HIGH; MFE +46.47%; MAE -5.43%; exit `FLOW_BREAKDOWN_SCORE_-2`; hold 3013.0ms.
Could have made more vs best arm: 0.140616 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.262289 SOL. Post-exit higher/lower: False/True.

### Trade 82 — `8sFhGE4RSHGUg67V6sYCZ6jaMZQ1ZDHmHnJbyPGrpump`

Best arm on this coin: **FLOW_V2_1** (+0.099320 SOL)

**FULL_2S_CONTROL** — WIN +0.075379 SOL (+28.55%); tier BASE; MFE +28.55%; MAE -3.95%; exit `FULL_2S_EXIT`; hold 2004.9ms.
Could have made more vs best arm: 0.023941 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.075379 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.053004 SOL (+21.47%); tier BASE; MFE +28.67%; MAE -3.87%; exit `HARD_2S_REMAINDER_EXIT`; hold 2004.9ms.
Could have made more vs best arm: 0.046316 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.070775 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.077673 SOL (+38.11%); tier BASE; MFE +62.37%; MAE -3.67%; exit `MAX_RUNNER_HOLD`; hold 4269.6ms.
Could have made more vs best arm: 0.021648 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.127125 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.099320 SOL (+32.94%); tier MEDIUM; MFE +61.47%; MAE -4.12%; exit `MAX_RUNNER_HOLD_SAFETY_CEILING`; hold 6003.9ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.185354 SOL. Post-exit higher/lower: True/True.

### Trade 83 — `EJM8RyxJfojvK4v7RGLyiyspLwb8ckrPwE9FXv6rpump`

Best arm on this coin: **V2_CURRENT** (+0.027201 SOL)

**FULL_2S_CONTROL** — WIN +0.012258 SOL (+1.53%); tier HIGH; MFE +30.86%; MAE -6.65%; exit `FULL_2S_EXIT`; hold 2001.8ms.
Could have made more vs best arm: 0.014944 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.247934 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — WIN +0.006019 SOL (+0.80%); tier HIGH; MFE +31.27%; MAE -6.38%; exit `HARD_2S_REMAINDER_EXIT`; hold 2001.8ms.
Could have made more vs best arm: 0.021182 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.234104 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — WIN +0.027201 SOL (+4.37%); tier HIGH; MFE +32.22%; MAE -17.07%; exit `PERSISTENT_DETERIORATION`; hold 2953.3ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.200762 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — LOSS -0.015359 SOL (-4.96%); tier MEDIUM; MFE +34.62%; MAE -6.50%; exit `PROFIT_FLOOR_PEAK_0.346_FLOOR_0.196_SCORE_2`; hold 2504.9ms.
Could have made more vs best arm: 0.042560 SOL. Could have lost less vs best arm: 0.042560 SOL. Hindsight MFE upper bound: 0.107143 SOL. Post-exit higher/lower: False/True.

### Trade 84 — `H8Fe67ugg9RQ8W2F4C6GN4GTNvjGLGqektoHbZZ2XbB9`

Best arm on this coin: **V2_CURRENT** (-0.003969 SOL)

**FULL_2S_CONTROL** — LOSS -0.039827 SOL (-14.84%); tier BASE; MFE -3.66%; MAE -17.49%; exit `FULL_2S_EXIT`; hold 2001.9ms.
Could have made more vs best arm: 0.035858 SOL. Could have lost less vs best arm: 0.035858 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — LOSS -0.036512 SOL (-14.61%); tier BASE; MFE -3.60%; MAE -17.44%; exit `HARD_2S_REMAINDER_EXIT`; hold 2001.9ms.
Could have made more vs best arm: 0.032543 SOL. Could have lost less vs best arm: 0.032543 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — LOSS -0.003969 SOL (-1.90%); tier BASE; MFE +3.35%; MAE -23.86%; exit `MAX_RUNNER_HOLD`; hold 4004.2ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.007013 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — LOSS -0.051171 SOL (-16.60%); tier MEDIUM; MFE -3.81%; MAE -17.61%; exit `EARLY_FLOW_INVALIDATION_-3`; hold 754.2ms.
Could have made more vs best arm: 0.047202 SOL. Could have lost less vs best arm: 0.047202 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.

### Trade 85 — `B66R6MbwDwEz7DyWy5uCrF3SwvtjHAh9ixFype15LLkq`

Best arm on this coin: **FLOW_V2_1** (-0.030026 SOL)

**FULL_2S_CONTROL** — LOSS -0.063315 SOL (-14.85%); tier MEDIUM; MFE -4.27%; MAE -16.62%; exit `FULL_2S_EXIT`; hold 2002.8ms.
Could have made more vs best arm: 0.033290 SOL. Could have lost less vs best arm: 0.033290 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — LOSS -0.059720 SOL (-15.05%); tier MEDIUM; MFE -4.16%; MAE -16.53%; exit `HARD_2S_REMAINDER_EXIT`; hold 2002.8ms.
Could have made more vs best arm: 0.029694 SOL. Could have lost less vs best arm: 0.029694 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — LOSS -0.099691 SOL (-29.83%); tier MEDIUM; MFE -3.92%; MAE -35.83%; exit `EMERGENCY_CATASTROPHIC_DRAWDOWN`; hold 5633.7ms.
Could have made more vs best arm: 0.069665 SOL. Could have lost less vs best arm: 0.069665 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — LOSS -0.030026 SOL (-15.79%); tier BASE; MFE -3.40%; MAE -15.90%; exit `EARLY_FLOW_INVALIDATION_-4`; hold 619.9ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/True.

### Trade 86 — `Bt4z3qfJphgEeeg3UeMs12REyWrv4yGDPrwK3miHWp7x`

Best arm on this coin: **V2_CURRENT** (+0.131284 SOL)

**FULL_2S_CONTROL** — LOSS -0.042988 SOL (-5.44%); tier HIGH; MFE +16.43%; MAE -6.13%; exit `FULL_2S_EXIT`; hold 2001.9ms.
Could have made more vs best arm: 0.174272 SOL. Could have lost less vs best arm: 0.174272 SOL. Hindsight MFE upper bound: 0.129799 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — LOSS -0.001474 SOL (-0.20%); tier HIGH; MFE +16.75%; MAE -5.89%; exit `HARD_2S_REMAINDER_EXIT`; hold 2001.9ms.
Could have made more vs best arm: 0.132757 SOL. Could have lost less vs best arm: 0.132757 SOL. Hindsight MFE upper bound: 0.123122 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.131284 SOL (+21.46%); tier HIGH; MFE +36.67%; MAE -6.71%; exit `MAX_RUNNER_HOLD`; hold 10002.6ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.224269 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.011431 SOL (+3.79%); tier MEDIUM; MFE +19.26%; MAE -3.98%; exit `FLOW_2S_EXIT_SCORE_-4`; hold 2002.1ms.
Could have made more vs best arm: 0.119853 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.058120 SOL. Post-exit higher/lower: True/True.

### Trade 87 — `9MXRT7wDKGAG6wAbz6qgLWxn4MQxf9ud1PY62iEH8zj3`

Best arm on this coin: **V2_CURRENT** (+0.048259 SOL)

**FULL_2S_CONTROL** — LOSS -0.002301 SOL (-0.55%); tier MEDIUM; MFE -0.55%; MAE -4.63%; exit `FULL_2S_EXIT`; hold 2003.2ms.
Could have made more vs best arm: 0.050560 SOL. Could have lost less vs best arm: 0.050560 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — LOSS -0.000108 SOL (-0.03%); tier MEDIUM; MFE -0.42%; MAE -4.51%; exit `HARD_2S_REMAINDER_EXIT`; hold 2003.2ms.
Could have made more vs best arm: 0.048367 SOL. Could have lost less vs best arm: 0.048367 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.048259 SOL (+14.33%); tier MEDIUM; MFE +31.30%; MAE -4.25%; exit `MAX_RUNNER_HOLD`; hold 6005.0ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.105386 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.000928 SOL (+0.31%); tier MEDIUM; MFE +0.02%; MAE -4.09%; exit `FLOW_2S_EXIT_SCORE_0`; hold 2003.3ms.
Could have made more vs best arm: 0.047331 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.000059 SOL. Post-exit higher/lower: True/True.

### Trade 88 — `FHQs4ZeJdWBNjwcTKoiG9Mui7oFhkUrJGN9PokJA19MX`

Best arm on this coin: **FLOW_V2_1** (-0.130305 SOL)

**FULL_2S_CONTROL** — LOSS -0.264553 SOL (-33.79%); tier HIGH; MFE +9.37%; MAE -33.79%; exit `FULL_2S_EXIT`; hold 2003.9ms.
Could have made more vs best arm: 0.134247 SOL. Could have lost less vs best arm: 0.134247 SOL. Hindsight MFE upper bound: 0.073351 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — LOSS -0.179903 SOL (-24.48%); tier HIGH; MFE +9.64%; MAE -33.64%; exit `HARD_2S_REMAINDER_EXIT`; hold 2003.9ms.
Could have made more vs best arm: 0.049598 SOL. Could have lost less vs best arm: 0.049598 SOL. Hindsight MFE upper bound: 0.070854 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — LOSS -0.166187 SOL (-26.03%); tier HIGH; MFE +10.19%; MAE -35.61%; exit `EMERGENCY_CATASTROPHIC_DRAWDOWN`; hold 2485.1ms.
Could have made more vs best arm: 0.035882 SOL. Could have lost less vs best arm: 0.035882 SOL. Hindsight MFE upper bound: 0.065085 SOL. Post-exit higher/lower: False/True.
**FLOW_V2_1** — LOSS -0.130305 SOL (-22.96%); tier HIGH; MFE +10.60%; MAE -31.83%; exit `CATASTROPHIC_DRAWDOWN_28PCT`; hold 366.4ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.060172 SOL. Post-exit higher/lower: False/True.

### Trade 89 — `7iGECjp9pj41bjw7hgZLNCMQuBD4nEEHRqhQXJsepump`

Best arm on this coin: **PARTIALS_2S** (-0.008375 SOL)

**FULL_2S_CONTROL** — LOSS -0.009340 SOL (-3.77%); tier BASE; MFE +3.58%; MAE -3.77%; exit `FULL_2S_EXIT`; hold 2004.3ms.
Could have made more vs best arm: 0.000965 SOL. Could have lost less vs best arm: 0.000965 SOL. Hindsight MFE upper bound: 0.008876 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — LOSS -0.008375 SOL (-3.55%); tier BASE; MFE +3.64%; MAE -3.72%; exit `HARD_2S_REMAINDER_EXIT`; hold 2004.3ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.008583 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — LOSS -0.010447 SOL (-5.11%); tier BASE; MFE +3.78%; MAE -26.42%; exit `MAX_RUNNER_HOLD`; hold 4197.5ms.
Could have made more vs best arm: 0.002072 SOL. Could have lost less vs best arm: 0.002072 SOL. Hindsight MFE upper bound: 0.007732 SOL. Post-exit higher/lower: True/False.
**FLOW_V2_1** — LOSS -0.010885 SOL (-3.72%); tier MEDIUM; MFE +3.37%; MAE -3.96%; exit `FLOW_2S_EXIT_SCORE_-5`; hold 2004.4ms.
Could have made more vs best arm: 0.002510 SOL. Could have lost less vs best arm: 0.002510 SOL. Hindsight MFE upper bound: 0.009856 SOL. Post-exit higher/lower: True/True.

### Trade 90 — `E22oDR9GPBYoM6QNVeM6BDTQam8qYiky8YeHMDADpump`

Best arm on this coin: **V2_CURRENT** (+0.069857 SOL)

**FULL_2S_CONTROL** — WIN +0.039930 SOL (+5.38%); tier HIGH; MFE +5.38%; MAE -10.10%; exit `FULL_2S_EXIT`; hold 2001.2ms.
Could have made more vs best arm: 0.029927 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.039930 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.021468 SOL (+3.04%); tier HIGH; MFE +5.56%; MAE -9.96%; exit `HARD_2S_REMAINDER_EXIT`; hold 2001.2ms.
Could have made more vs best arm: 0.048388 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.039290 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.069857 SOL (+11.41%); tier HIGH; MFE +22.72%; MAE -9.57%; exit `MAX_RUNNER_HOLD`; hold 10003.3ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.139072 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — LOSS -0.013480 SOL (-2.47%); tier HIGH; MFE +6.37%; MAE -9.29%; exit `FLOW_BREAKDOWN_SCORE_-7`; hold 2252.1ms.
Could have made more vs best arm: 0.083337 SOL. Could have lost less vs best arm: 0.083337 SOL. Hindsight MFE upper bound: 0.034830 SOL. Post-exit higher/lower: True/True.

### Trade 91 — `4tvrTkYpaMkfW6RMR9aSizBPEd68LAKhdcX7mSs6pump`

Best arm on this coin: **V2_CURRENT** (+0.050038 SOL)

**FULL_2S_CONTROL** — WIN +0.037222 SOL (+9.33%); tier MEDIUM; MFE +9.33%; MAE -4.37%; exit `FULL_2S_EXIT`; hold 2002.4ms.
Could have made more vs best arm: 0.012816 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.037222 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.021503 SOL (+5.68%); tier MEDIUM; MFE +9.44%; MAE -4.28%; exit `HARD_2S_REMAINDER_EXIT`; hold 2002.4ms.
Could have made more vs best arm: 0.028535 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.035721 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.050038 SOL (+15.07%); tier MEDIUM; MFE +45.92%; MAE -4.08%; exit `MAX_RUNNER_HOLD`; hold 6000.2ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.152459 SOL. Post-exit higher/lower: False/True.
**FLOW_V2_1** — WIN +0.034151 SOL (+6.27%); tier HIGH; MFE +8.59%; MAE -5.00%; exit `FLOW_2S_EXIT_SCORE_0`; hold 2002.5ms.
Could have made more vs best arm: 0.015887 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.046744 SOL. Post-exit higher/lower: True/True.

### Trade 92 — `8iVwZjPgC9s9H1puqW4G5ogUzCt8pzfAPAgZzEKmpump`

Best arm on this coin: **FULL_2S_CONTROL** (+0.095921 SOL)

**FULL_2S_CONTROL** — WIN +0.095921 SOL (+23.87%); tier MEDIUM; MFE +32.96%; MAE -4.66%; exit `FULL_2S_EXIT`; hold 2002.2ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.132440 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.093192 SOL (+24.50%); tier MEDIUM; MFE +33.12%; MAE -4.55%; exit `HARD_2S_REMAINDER_EXIT`; hold 2002.3ms.
Could have made more vs best arm: 0.002729 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.125961 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.066064 SOL (+19.66%); tier MEDIUM; MFE +33.46%; MAE -4.33%; exit `MAX_RUNNER_HOLD`; hold 6003.8ms.
Could have made more vs best arm: 0.029856 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.112424 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.048469 SOL (+16.54%); tier MEDIUM; MFE +33.78%; MAE -4.12%; exit `FLOW_BREAKDOWN_SCORE_-6`; hold 2757.9ms.
Could have made more vs best arm: 0.047452 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.099013 SOL. Post-exit higher/lower: True/True.

### Trade 93 — `4VHSkVYQ66xTS4iZmWFXTVKMyTZS6E81XMr7Xf7EAiCv`

Best arm on this coin: **FULL_2S_CONTROL** (+0.198558 SOL)

**FULL_2S_CONTROL** — WIN +0.198558 SOL (+25.86%); tier HIGH; MFE +25.86%; MAE -6.15%; exit `FULL_2S_EXIT`; hold 2001.5ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.198558 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — WIN +0.159773 SOL (+21.98%); tier HIGH; MFE +26.13%; MAE -5.96%; exit `HARD_2S_REMAINDER_EXIT`; hold 2001.6ms.
Could have made more vs best arm: 0.038785 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.189944 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — WIN +0.186095 SOL (+29.08%); tier HIGH; MFE +60.06%; MAE -13.96%; exit `MAX_RUNNER_HOLD`; hold 10000.3ms.
Could have made more vs best arm: 0.012463 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.384389 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — LOSS -0.021808 SOL (-7.34%); tier MEDIUM; MFE +28.97%; MAE -12.58%; exit `PROFIT_FLOOR_PEAK_0.290_FLOOR_0.170_SCORE_2`; hold 2503.4ms.
Could have made more vs best arm: 0.220366 SOL. Could have lost less vs best arm: 0.220366 SOL. Hindsight MFE upper bound: 0.086030 SOL. Post-exit higher/lower: True/True.

### Trade 94 — `72fKQpS6iHVXx3bHFEKuLY3kJbAqhaotsdgzcYLeTSFd`

Best arm on this coin: **FULL_2S_CONTROL** (+0.153672 SOL)

**FULL_2S_CONTROL** — WIN +0.153672 SOL (+36.12%); tier MEDIUM; MFE +43.12%; MAE -4.67%; exit `FULL_2S_EXIT`; hold 2003.4ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.183450 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — WIN +0.109699 SOL (+27.39%); tier MEDIUM; MFE +43.32%; MAE -4.55%; exit `HARD_2S_REMAINDER_EXIT`; hold 2003.4ms.
Could have made more vs best arm: 0.043972 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.173517 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — WIN +0.055268 SOL (+15.42%); tier MEDIUM; MFE +43.66%; MAE -4.35%; exit `MAX_RUNNER_HOLD`; hold 6002.6ms.
Could have made more vs best arm: 0.098403 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.156439 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.076390 SOL (+25.87%); tier MEDIUM; MFE +44.15%; MAE -4.06%; exit `FLOW_BREAKDOWN_SCORE_-5`; hold 2508.5ms.
Could have made more vs best arm: 0.077282 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.130362 SOL. Post-exit higher/lower: False/True.

### Trade 95 — `EAc2jC3ij94BTfU8c18Sb5aC8YcLmm87V7V3L7nWpump`

Best arm on this coin: **FULL_2S_CONTROL** (+0.547486 SOL)

**FULL_2S_CONTROL** — WIN +0.547486 SOL (+40.03%); tier EXCEPTIONAL; MFE +40.03%; MAE -7.17%; exit `FULL_2S_EXIT`; hold 2004.1ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.547486 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — WIN +0.428035 SOL (+33.46%); tier EXCEPTIONAL; MFE +40.51%; MAE -6.88%; exit `HARD_2S_REMAINDER_EXIT`; hold 2004.2ms.
Could have made more vs best arm: 0.119451 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.518160 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — WIN +0.292289 SOL (+25.94%); tier EXCEPTIONAL; MFE +54.90%; MAE -6.38%; exit `MAX_RUNNER_HOLD`; hold 16000.8ms.
Could have made more vs best arm: 0.255197 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.618652 SOL. Post-exit higher/lower: False/True.
**FLOW_V2_1** — WIN +0.121434 SOL (+12.89%); tier EXCEPTIONAL; MFE +42.38%; MAE -5.77%; exit `FLOW_BREAKDOWN_SCORE_-7`; hold 3008.8ms.
Could have made more vs best arm: 0.426052 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.399058 SOL. Post-exit higher/lower: True/True.

### Trade 96 — `BwDd9xEveZw2wSWkMtAwJ8RPaFDkpZGMnyTto4R6qfpY`

Best arm on this coin: **FULL_2S_CONTROL** (+0.239251 SOL)

**FULL_2S_CONTROL** — WIN +0.239251 SOL (+49.69%); tier MEDIUM; MFE +54.02%; MAE -6.80%; exit `FULL_2S_EXIT`; hold 2000.9ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.260092 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.151754 SOL (+34.21%); tier MEDIUM; MFE +54.33%; MAE -6.63%; exit `HARD_2S_REMAINDER_EXIT`; hold 2000.9ms.
Could have made more vs best arm: 0.087498 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.240986 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.126344 SOL (+32.90%); tier MEDIUM; MFE +121.37%; MAE -6.38%; exit `MAX_RUNNER_HOLD`; hold 6000.5ms.
Could have made more vs best arm: 0.112907 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.466082 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.136379 SOL (+43.84%); tier MEDIUM; MFE +72.26%; MAE -6.06%; exit `FLOW_BREAKDOWN_SCORE_-5`; hold 3515.5ms.
Could have made more vs best arm: 0.102872 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.224766 SOL. Post-exit higher/lower: True/True.

### Trade 97 — `DkBdmgWovnD4MFp5qNXEn2moTfKX7aUt3z4ozbEQpump`

Best arm on this coin: **FLOW_V2_1** (-0.044746 SOL)

**FULL_2S_CONTROL** — LOSS -0.194141 SOL (-20.68%); tier HIGH; MFE -6.84%; MAE -20.68%; exit `FULL_2S_EXIT`; hold 2002.0ms.
Could have made more vs best arm: 0.149394 SOL. Could have lost less vs best arm: 0.149394 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — LOSS -0.145805 SOL (-17.06%); tier HIGH; MFE -6.47%; MAE -20.38%; exit `HARD_2S_REMAINDER_EXIT`; hold 2002.0ms.
Could have made more vs best arm: 0.101058 SOL. Could have lost less vs best arm: 0.101058 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — LOSS -0.219332 SOL (-29.68%); tier HIGH; MFE -5.96%; MAE -36.11%; exit `EMERGENCY_CATASTROPHIC_DRAWDOWN`; hold 3794.4ms.
Could have made more vs best arm: 0.174585 SOL. Could have lost less vs best arm: 0.174585 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — LOSS -0.044746 SOL (-13.90%); tier MEDIUM; MFE -4.09%; MAE -18.44%; exit `EARLY_FLOW_INVALIDATION_-5`; hold 1925.2ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.000000 SOL. Post-exit higher/lower: False/True.

### Trade 98 — `73Z2trMC4AnPB428ZYpXDPrASmCDpifam8KES8DBpump`

Best arm on this coin: **FLOW_V2_1** (+0.297913 SOL)

**FULL_2S_CONTROL** — WIN +0.261749 SOL (+53.96%); tier MEDIUM; MFE +64.93%; MAE -5.01%; exit `FULL_2S_EXIT`; hold 2000.8ms.
Could have made more vs best arm: 0.036164 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.315003 SOL. Post-exit higher/lower: True/True.
**PARTIALS_2S** — WIN +0.212657 SOL (+47.89%); tier MEDIUM; MFE +65.34%; MAE -4.81%; exit `HARD_2S_REMAINDER_EXIT`; hold 2000.9ms.
Could have made more vs best arm: 0.085256 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.290125 SOL. Post-exit higher/lower: True/True.
**V2_CURRENT** — WIN +0.169062 SOL (+44.90%); tier MEDIUM; MFE +87.59%; MAE -4.49%; exit `MAX_RUNNER_HOLD`; hold 6003.7ms.
Could have made more vs best arm: 0.128851 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.329848 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.297913 SOL (+49.90%); tier HIGH; MFE +63.84%; MAE -5.56%; exit `FLOW_2S_EXIT_SCORE_-5`; hold 2001.1ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.381099 SOL. Post-exit higher/lower: True/True.

### Trade 99 — `H9X1jw4QKk5vfv2S84UNWNe8VwP4xXKtLvWTnuoDpump`

Best arm on this coin: **FLOW_V2_1** (+0.722380 SOL)

**FULL_2S_CONTROL** — WIN +0.255135 SOL (+16.13%); tier EXCEPTIONAL; MFE +16.13%; MAE -10.16%; exit `FULL_2S_EXIT`; hold 2001.6ms.
Could have made more vs best arm: 0.467245 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.255135 SOL. Post-exit higher/lower: True/False.
**PARTIALS_2S** — WIN +0.263588 SOL (+18.29%); tier EXCEPTIONAL; MFE +17.00%; MAE -9.53%; exit `HARD_2S_REMAINDER_EXIT`; hold 2001.6ms.
Could have made more vs best arm: 0.458792 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.244970 SOL. Post-exit higher/lower: True/False.
**V2_CURRENT** — WIN +0.660147 SOL (+54.15%); tier EXCEPTIONAL; MFE +81.87%; MAE -8.52%; exit `MAX_RUNNER_HOLD`; hold 16002.1ms.
Could have made more vs best arm: 0.062233 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.998028 SOL. Post-exit higher/lower: True/True.
**FLOW_V2_1** — WIN +0.722380 SOL (+67.55%); tier EXCEPTIONAL; MFE +82.55%; MAE -7.82%; exit `FLOW_BREAKDOWN_SCORE_-7`; hold 6290.6ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.882885 SOL. Post-exit higher/lower: True/True.

### Trade 100 — `BhaNSVbxJ7sbrRzs1RZnXjuFXeH1DxVbkxJPreFWpump`

Best arm on this coin: **FULL_2S_CONTROL** (+0.012470 SOL)

**FULL_2S_CONTROL** — WIN +0.012470 SOL (+3.79%); tier BASE; MFE +3.79%; MAE -4.35%; exit `FULL_2S_EXIT`; hold 2002.0ms.
Could have made more vs best arm: 0.000000 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.012470 SOL. Post-exit higher/lower: False/True.
**PARTIALS_2S** — WIN +0.005459 SOL (+1.81%); tier BASE; MFE +3.95%; MAE -4.21%; exit `HARD_2S_REMAINDER_EXIT`; hold 2002.0ms.
Could have made more vs best arm: 0.007010 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.011889 SOL. Post-exit higher/lower: False/True.
**V2_CURRENT** — WIN +0.005291 SOL (+1.91%); tier BASE; MFE +4.08%; MAE -4.09%; exit `MAX_RUNNER_HOLD`; hold 4004.0ms.
Could have made more vs best arm: 0.007179 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.011300 SOL. Post-exit higher/lower: False/True.
**FLOW_V2_1** — WIN +0.005044 SOL (+2.02%); tier BASE; MFE +4.23%; MAE -3.95%; exit `MAX_RUNNER_HOLD_SAFETY_CEILING`; hold 4004.1ms.
Could have made more vs best arm: 0.007426 SOL. Could have lost less vs best arm: 0.000000 SOL. Hindsight MFE upper bound: 0.010576 SOL. Post-exit higher/lower: False/True.


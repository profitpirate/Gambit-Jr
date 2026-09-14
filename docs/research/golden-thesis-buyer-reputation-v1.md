# Golden Thesis candidate v1 — early-buyer reputation

This lane is **standalone memecoin strategy research**. It does not copy, wait for, score, or use the E4 wallet as an entry signal. E4 is not part of the rule.

## Frozen rule

At 10 ms after a Pump SOL-quote launch CREATE:

1. Observe the unique non-creator buyers visible during the first 10 ms.
2. For each such wallet, use only its **strictly prior, already-settled** appearances in other launches.
3. A prior appearance is labelled a win only when a hypothetical 10 ms entry followed by a 2,000 ms hold was profitable after the same Pump fee/quote model used by the frozen corpus.
4. Aggregate prior appearances `N` and prior wins `W` across the current launch's early buyers.
5. Signal iff `N >= 10` and `W/N >= 0.70`.
6. Enter at the first executable reserve state at/after the 10 ms decision horizon; paper only.
7. Hold 2,000 ms, then exit from the live reserve state.

Source wallet identity, E4 selection labels, E4 position size, E4 timing, future P&L, and future buyer outcomes are forbidden entry inputs.

## Evidence before prospective freeze

Frozen 66,000-launch corpus, chronologically split:

- train: 74 signals, 47 wins, 63.5% WR, fixed-stake P&L +0.4348 SOL, PF ~3.67
- validation: 58 signals, 47 wins, 81.0% WR, fixed-stake P&L +0.4522 SOL, PF ~6.94; all four validation windows positive
- historical holdout: 92 signals, 74 wins, 80.4% WR, fixed-stake P&L +2.1838 SOL; all ten holdout windows positive

The historical holdout is **not pristine certification anymore** because this research lane inspected it while investigating robustness. It is supporting evidence only.

Two later 3,000-launch captures that were not used to choose the frozen thresholds were then processed sequentially while carrying buyer reputation forward:

- run 34779504872: 6 signals, 5 wins / 1 loss, +0.07414 SOL at 0.0555 fixed stake
- run 34809168521: 3 signals, 2 wins / 1 loss, +0.00754 SOL at 0.0555 fixed stake
- combined: 9 signals, 7 wins / 2 losses = 77.78% WR, +0.08168 SOL fixed-stake P&L
- 2 SOL bankroll simulation at 1.85% equity risk: ending balance ~2.0551 SOL

Nine forward-ish trades are promising but far too few to establish a durable edge.

## Certification contract

The candidate is frozen **before** prospective testing. No threshold or exit changes are allowed during certification.

Prospective certification requires at least 50 closed paper trades accumulated without resetting the bankroll or buyer-reputation state, with:

- positive net P&L after the live reserve quote model;
- profit factor > 1.25;
- positive expectancy;
- maximum realized-equity drawdown <= 20%;
- no single winner contributing > 50% of gross profits;
- evidence spanning multiple independently captured market windows;
- no unresolved paper exits or silent feed/scoring failures.

Win rate is reported but is not itself the optimization target. A lower-WR positive-expectancy strategy can be superior to an overfit high-WR strategy.

Until that prospective contract is met, this remains **GOLDEN_CANDIDATE**, not GOLDEN_CERTIFIED.

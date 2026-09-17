# E4 pre-armed selection research

## What the oracle is

The oracle is not a selection model. It is a non-deployable ceiling that is handed E4's true future choices and measures the economics and latency decay of copying those choices.

## Strongest reproducible selection formula

The leading research-only rule is:

1. The creator has at least one earlier E4 selection.
2. The creator-to-social-handle relationship was observed before this launch.
3. A status from that handle arrives 0–10 seconds before CREATE.
4. The CREATE event reports at least 2 SOL of creator seed.
5. Mayhem launches are rejected.
6. The hot path uses only cached hash lookups and integer comparisons; metadata and social I/O are completed before CREATE.
7. The existing entry-output guard and independently frozen exit policy remain unchanged.

This is one profitable E4-like branch, not a complete reconstruction of every E4 choice.

## Strictly later epoch

The evaluation below uses the seven capture windows strictly after the social source's last run. The formula thresholds came from the earlier social epoch.

| Added execution latency | Trades | WR | Wilson lower | Net PnL from 3 SOL | PF | Drawdown | Windows |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 ms | 28 | 100.0% | 87.9% | +4.5721 SOL | no losses | 0.00% | 6 |
| 1 ms | 27 | 92.6% | 76.6% | +2.3392 SOL | 47.735 | 1.29% | 6 |
| 2 ms | 26 | 92.3% | 75.9% | +1.8328 SOL | 38.410 | 1.26% | 6 |
| 5 ms | 24 | 87.5% | 69.0% | +1.4915 SOL | 22.905 | 1.17% | 6 |
| 10 ms | 20 | 90.0% | 69.9% | +0.8864 SOL | 22.136 | 0.70% | 6 |
| 20 ms | 18 | 66.7% | 43.7% | +0.3689 SOL | 3.249 | 3.77% | 6 |
| 50 ms | 18 | 66.7% | 43.7% | +0.3346 SOL | 3.000 | 3.89% | 6 |

The rule predicted 28 launches and overlapped 8 of 91 E4 attempts in this epoch: 28.6% E4-intent precision and 8.8% recall. Its value is therefore the profitable cohort it isolates, not exact cloning of E4's complete accept/reject policy.

## Execution speed

The direct Python gate benchmarks at 0.4 microseconds median and 0.5 microseconds p99 over 100,000 decisions. It performs zero hot-path I/O and creates no model objects. The practical latency budget is therefore dominated by transaction construction and submission, not selection inference.

## What the coins have in common

- E4 strongly avoids mayhem launches.
- Exact developer recurrence is real: after the corrected point-in-time warmup, launches from creators with one prior E4 attempt had an 18.97x selection lift; creators with at least two had a 43.43x lift.
- Selected launches typically seed about 3 SOL versus about 0.13 SOL for ignored launches.
- The durable profitable branch combines a recurring developer, a previously linked social identity, a near-synchronous pre-launch post, and at least 2 SOL seed.
- Recurring early buyers are diagnostic, but they are not the primary trigger: many E4 decisions occur before buyer evidence exists.

## Integrity limits

- This is retrospective research, not an approved golden thesis.
- The strictly later segment has only 24 executed trades at 5 ms and six executable capture windows.
- Of 611 recurring-creator holdout launches, 335 metadata records were retrievable, 244 exposed a social link, and 221 exposed a status timestamp. Missing metadata is retained in the denominator and never silently dropped.
- An untouched future/live epoch is required before production approval.
- Production V12 paths were not changed.

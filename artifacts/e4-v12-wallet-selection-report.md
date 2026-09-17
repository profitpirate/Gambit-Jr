# E4 wallet-selection reconstruction

## Verdict

No golden thesis is approved. The research isolated a real E4 selection edge,
but it is coupled to same-slot execution and decays too quickly for the current
250–300 ms operating path. Production V12 was not modified.

The closest learned candidate scores launches at the CREATE event and combines:

1. probability that E4 would submit a buy, with both landed and failed buys as
   positive intent labels;
2. conditional probability that the attempted buy would land;
3. a reserve/output execution guard; and
4. an independent two-second exit with a 30% first scale-out at 1.15x, a 0.70x
   stop, and a 25% trailing retrace.

Its dominant inputs are launch mode, causal creator recurrence (including prior
E4 attempts, landed attempts, and failed attempts), creator seed size, and
create-time metadata/text structure. E4-authored events are excluded from every
selector feature.

## First sealed 250 ms result

The first model was frozen before the final ten capture windows were opened.
Across 30,000 chronological launches and 175 E4 attempts it produced 92 signals:

- intent precision: 54.35%;
- intent recall: 28.57%;
- intent ROC-AUC: 0.9496;
- executed trades after the hard output guard: 28;
- wins: 10 (35.71% WR);
- net PnL: +0.1215 SOL from 3 SOL;
- profit factor: 1.162;
- 100 ms additional latency: -0.2605 SOL;
- 500 ms additional latency: -0.4233 SOL.

This candidate was rejected.

## Exact-wallet oracle: where E4's edge lives

The oracle is non-deployable: it uses the future E4 label only to measure the
maximum value of knowing E4's exact choices. Its exit policy was selected on the
development windows, then replayed unchanged on the ten later windows.

| Exact E4 set | Entry horizon | Trades | WR | Net PnL | PF |
|---|---:|---:|---:|---:|---:|
| landed choices | 0 ms | 105 | 80.95% | +79.5114 SOL | 4.477 |
| landed choices | 5 ms | 72 | 65.28% | +11.0166 SOL | 5.818 |
| landed choices | 20 ms | 56 | 50.00% | +2.3078 SOL | 3.165 |
| landed choices | 250 ms | 50 | 48.00% | +1.1049 SOL | 2.223 |
| all attempts | 0 ms | 175 | 59.43% | +105.7925 SOL | 1.636 |
| all attempts | 5 ms | 112 | 66.07% | +24.4654 SOL | 3.712 |
| all attempts | 20 ms | 86 | 54.65% | +6.1572 SOL | 3.321 |
| all attempts | 250 ms | 74 | 45.95% | +2.3776 SOL | 2.280 |

The large compounded PnL at 0 ms is not operationally attainable and must not be
presented as a strategy result. It is evidence that selection has value before
the market moves. The WR collapse between 5 ms and 20 ms is the key causal fact.

## Fast-horizon exploratory model

After the first holdout was opened, an exploratory CREATE-event model was built
to test the latency hypothesis. The same ten windows are therefore contaminated
for this revision and cannot certify it.

At an impossible 0 ms entry it recorded 66 trades, 87.88% WR, and +16.8276 SOL.
With only 5 ms added latency it fell to 51 trades, 62.75% WR, and +2.8361 SOL
(PF 3.733). At 500 ms it fell to 29 trades, 17.24% WR, and -0.5715 SOL.

The model is promising research evidence, not a golden thesis: it misses the 65%
WR gate at 5 ms, covers only seven of ten windows after execution gating, and a
normal remote VPS cannot credibly guarantee CREATE-to-submission in five
milliseconds.

## Required next evidence

Do not tune again on these ten windows. Freeze the CREATE-event selector, collect
a new future evidence epoch, and measure end-to-end websocket receipt, scoring,
transaction construction, relay acknowledgement, landing, and output retention.
Only a new chronological epoch plus untouched live trades can decide whether a
co-located/validator-adjacent execution path makes this thesis deployable.

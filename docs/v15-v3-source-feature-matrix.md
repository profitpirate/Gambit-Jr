# V1.5 Intelligence V3 source-to-feature matrix

This is the mandatory pre-implementation evidence gate for `INTELLIGENCE_V3_RESEARCH`.
`CONTROL_V15` and `INTELLIGENCE_V2` remain frozen. No result in this document approves a feature,
changes public alerts, or authorizes production.

The machine-readable inventory is
[`docs/evidence/v15-v3-source-feature-matrix.json`](evidence/v15-v3-source-feature-matrix.json).
It records dates, platforms, sample sizes, outcomes, point-in-time status, limitations, applicability,
licensing, and the validation contract for every proposed identifier.

## Findings reproduced from primary sources

| Source | Reproducible scope | Evidence carried into V3 | Hard limitation |
|---|---|---|---|
| [Marino et al.](https://arxiv.org/abs/2602.14860) | 655,770 Pump.fun launches, September 2025 | Real SOL state and accumulation speed matter; fewer trades to the same curve state is informative; bot-like activity can be adverse | Data/code unavailable; graduation is not a tradable return |
| [MELT](https://github.com/git-disl/MELT) | 41k+ migrated launches, 200M+ typed transactions, Dec 2024–Mar 2025 | Wash, transfer, mint, bundle and linked-account traces are meaningful manipulation evidence | Survivor-selected migrated sample; CC BY-NC 4.0 means research only |
| [Catching the Rug](https://arxiv.org/abs/2608.20271) | 6.4M tokens, Pump.fun and Raydium, Nov 2024–Jun 2025 | First-five-minute classic tabular models and platform-aware training are justified | Public reusable data/code not established; within-window rugs are dropped |
| [SolRPDS](https://github.com/DeFiLabX/SolRPDS) | 2021–2024 Solana DEX pool evidence | LP additions/removals, withdrawals and inactivity support liquidity-risk research | Pre-Pump.fun platform transfer only |
| [SolRugDetector](https://arxiv.org/abs/2603.24625) | 100,063 Orca/Raydium/Meteora tokens, H1 2025 | Freeze abuse, liquidity withdrawal, pump-and-dump and group recurrence are distinct risks | Conservative 24h labels omit slower rugs; dataset licence not stated |
| [Corrected Graduation Regime Windows](https://arxiv.org/abs/2607.02823) | 860,213 released launches, May–Jun 2026 | Social presence is association/context, not velocity; initial self-buy needs risk adjustment | Polling observed only ~6-minute fast graduations; virtual market cap is not real capital |
| [Coordinated Sniper Cohorts](https://arxiv.org/abs/2607.02795) | 166,098 launches, 1,578,333 buyer observations | Linked early wallets must be collapsed to independent components | Activity-matched placebo outperforms the cohort association; no naive bullish causal signal |
| [Order flow and cryptocurrency returns](https://doi.org/10.1016/j.finmar.2026.101047) | 84 established cryptocurrencies, 2018–Jun 2022 | Buy/sell flow deserves an independently validated specialist | Daily/weekly fiat flow is adjacent, not direct launch-token evidence; data by request |
| [The Social Signal](https://doi.org/10.1016/j.jfineco.2024.103870) | US firm-days on StockTwits/Twitter/Seeking Alpha, 2012–2021 | Attention, sentiment and source/user type must be separate | Equity daily evidence is adjacent; attention often predicts reversal |
| [Twitter-Based Attention](https://ssrn.com/abstract=5010136) | Crypto-day panel, 2018–2022 | Investor attention and official-channel activity must be separate | Daily established-asset evidence and non-public Twitter history |
| [Solana Tracker PnL V2](https://docs.solanatracker.io/data-api/pnl-v2/wallet/get-wallet-token-position) | Live operator queries | Use strict/adjusted modes, filter identity labels, capture responses at decision time | Current PnL is future information in replay; API key and commercial terms apply |

## Reproduction status

The repository already contains checksum-verified, ignored local research copies of the 798,430-launch
Pump.fun corpus and the MELT feature/bundle release. Existing reports reproduce their counts and
limitations. MELT-derived parameters cannot be shipped commercially. Marino data/code, the
Catching-the-Rug corpus, the Journal of Financial Markets order-flow data, and the two proprietary
social datasets are not publicly reproducible from this workspace, so their findings are treated as
design evidence only.

The corrected RED-PUMP release is useful as a collector-bias regression fixture, not a 24-hour outcome
set. Its documented failure mode becomes a V3 invariant: `market_cap`, `real_reserves`, `liquidity`,
and `price` remain separate, and a timeout after collector loss is censored rather than a failure.

## Current V2 decision-path audit

The live and research paths currently form this chain:

`provider -> normalized MarketSnapshot -> service components -> legacy ScoringEngine -> intelligence
pillars -> V1.5 mean-based runner/failure engines -> legacy AND V1.5 signal gate -> Discord/outbox ->
tracked outcome`.

The audit found:

- `service.py` requires both an accepted legacy classification and an accepted V1.5 tier. That is
  hidden double-gating for research comparisons.
- `v15_engine.py` averages only known stage features. Missingness therefore changes effective weights.
- `scoring/engine.py`, `intelligence.py`, `alpha_engine.py`, and `v15_engine.py` each impose manual
  weights or thresholds, producing duplicate score/veto truth.
- `intelligence_v2_research.py` caps model fitting at 80,000 observations by retaining positives and
  striding negatives. It then calibrates on a changed fitting distribution without prevalence weights.
- V2 selects percentile-blend weights on one validation slice. Percentile averaging destroys specialist
  probability meaning and its short single split is not sealed evidence.
- The social engine correctly returns no score because links are presence, not velocity. The narrative
  engine returns metadata identity/fit only; it has no trend evidence. V3 must preserve those unknowns.
- Service liquidity quality is a level transformed from USD liquidity. It is not liquidity velocity,
  real SOL reserve trajectory, or migration continuity.
- Several provider-enriched fields have no per-field availability timestamp. They are unusable in a
  strict replay until provenance and evidence time are supplied.

V3 therefore owns one `V3DecisionEnvelope` in shadow research. Legacy and V1.5 results become named
comparison fields, not V3 vetoes. The production notifier remains reachable only from frozen CONTROL.

## Promotion rule

An identifier with no direct/adjacent source, no point-in-time provider, or zero current coverage is
explicitly unavailable. It cannot influence V3. Every supported identifier still requires group-purged,
nested chronological validation on natural prevalence, later evidence, calibration, actionability, and
family ablation before it can be considered for approval.

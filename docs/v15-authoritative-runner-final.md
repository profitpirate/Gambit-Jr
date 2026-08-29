# A. ARCHITECTURE BEFORE / AFTER

Before this cleanup, five overlapping objects could appear to describe signal
truth: legacy `ScoringEngine`, Alpha/V1.4 lifecycle decisions, deterministic
`V15Decision`, `V3ShadowEngine`, and `RunnerThesisEngine`. Live routing combined
older score/classification state while Runner Thesis remained research-only.

After cleanup, `RunnerDecision` is the only final signal-policy input and the
only object that selects `HOLD`, `REJECTED`, `RESEARCH_SHADOW_CALL`,
`OPERATOR_SHADOW_ALERT`, or `PUBLIC_ALERT`. The production dependency map is:

| System | Classification | Permitted responsibility |
|---|---|---|
| `RunnerDecisionEngine` | AUTHORITATIVE | One final route decision and immutable decision-time outcome anchor |
| deterministic `V15Decision` / `CONTROL_V15` | CHAMPION CONTROL | Current nomination/tier evidence consumed by `RunnerDecision`; cannot route directly |
| legacy `ScoringEngine` | CONTROL_ONLY | Compatibility score, safety rejections, payload context |
| Alpha/V1.4 decision | CONTROL_ONLY | Candidate lifecycle and comparison evidence |
| `V3ShadowEngine` | RESEARCH_ONLY | Same-universe challenger evidence; database-enforced no public route |
| `RunnerThesisEngine` | RESEARCH/EXPLANATION_ONLY | Heuristic thesis formation; sigmoid outputs are explicitly heuristic scores, not calibrated probabilities |
| retired hand-built control/V3/failure hybrid | DEPRECATED/REMOVED | No longer calculated or compared |

The authoritative flow is discovery → canonical events → incremental token
state → feature state → CONTROL_V15 nomination → target-specific model slots →
independent failure/actionability slots → explanation → `RunnerDecision` → one
explicit route. No target probabilities are emitted unless the model record is
chronologically validated and calibrated; unsupported values remain `null`.

# B. AUDIT FINDINGS

The round-two finding ledger is in `docs/v15-code-audit-final.md`. Key fixes are:

- decision outcomes now start at each actual decision price/market cap and store
  target times for 2X/5X/10X/20X/50X, MFE, MAE, failure and copyability fields;
- deterministic evaluation-universe hashing blocks mismatched comparisons;
- the research lab no longer constructs a hidden arithmetic hybrid;
- runner, failure and copyability research targets are fitted independently;
- sell absorption V2, sequence bands, capital efficiency and hard-negative
  research have explicit non-public contracts;
- the hot path uses compact incremental state and `Counter`/actor tables instead
  of repeated full-history and repeated-buyer scans;
- same-token events remain ordered while different-token lanes run concurrently;
- Pump Anchor logs emit before optional transaction enrichment, with bounded
  queue/cache/coalescing/backoff and paginated cursor recovery;
- evidence freshness is derived from provenance, and routed decisions gain
  measured enqueue/Discord latency fields as those stages occur;
- research shadow calls, operator alerts and public alerts are separate states,
  with both alert routes disabled by default.

# C. DUNE OWNERSHIP

Gambit owns eight versioned templates under
`src/memecoin_bot/historical/sql/dune/`: launches, Pump.fun trades, PumpSwap
trades, migrations, wallet activity, creator activity, monthly universe and
outcome reconstruction. The registry records template hash, schema version,
expected columns, strict parameters, source tables, minimum date and dated
compatibility state. The implementation uses Dune's current direct SQL endpoint,
then polls and pages an execution, validates the result schema, writes immutable
Parquet partitions outside Git, and checkpoints month state. A saved-query ID is
an optional fallback only when the plan explicitly rejects direct SQL.

Official schema/API review used Dune's [direct SQL execution](https://docs.dune.com/api-reference/executions/endpoint/execute-sql),
[Solana DEX trades](https://docs.dune.com/data-catalog/curated/dex-trades/solana/solana-dex-trades),
[Solana transactions](https://docs.dune.com/data-catalog/solana/transactions), and
[Solana token transfers](https://docs.dune.com/data-catalog/curated/token-transfers/solana/solana-token-transfers)
contracts.

Actual acquisition in this environment:

| Item | Count/state |
|---|---:|
| Repository SQL templates | 8 |
| Direct Dune queries executed | 0 |
| Months acquired | 0 |
| Rows acquired | 0 |
| Dune Parquet committed | 0 |
| Reason | `DUNE_API_KEY` not configured |

Mocked pagination/schema tests are engineering evidence only and are not counted
as historical data. No fake corpus or saved-query claim is reported.

# D. HELIUS

When `HELIUS_API_KEY` is present and `SOLANA_RPC_URL` remains the public mainnet
default, Gambit derives redacted Helius RPC/WSS endpoints as primary and keeps the
public URL as fallback. Native Pump program logs, dynamic HOT curve accounts and
curated wallet/creator accounts remain selectively scoped. PumpPortal is optional
redundancy and is never geo-bypassed or treated as a production blocker.

The implementation follows Helius' current [endpoint](https://www.helius.dev/docs/api-reference/endpoints),
[logsSubscribe](https://www.helius.dev/docs/api-reference/rpc/websocket/logssubscribe),
and [rate-limit](https://www.helius.dev/docs/billing/rate-limits) contracts.

Actual Helius evidence in this environment: 0 live events, 0 measured live 429s,
0 reconnects, and no live transport latency because `HELIUS_API_KEY` is absent.
Fast-path decoding, 429, disconnect, fallback and ordering results are local
contract tests—not live-event claims.

# E. INTELLIGENCE

No real multi-year decision corpus was available in this checkout, so no
same-universe control/challenger metric is reportable and no feature/model was
approved. Approved historical features: 0. Challenger promotions: 0.

`CONTROL_V15` remains the explicit champion. `SELL_ABSORPTION_V2`,
`SEQUENCE_GBM_2X`, `HARD_NEGATIVE_2X`, `MID_5X`, and `RIGHT_TAIL_10X` remain
research-only challengers. June/July diagnostic windows remain retired for
approval. The previously quoted 15.21%/20.03% references are cross-experiment
hypotheses, not comparable results and are not reused as a claimed lift.

# F. PERFORMANCE

Measured locally on Windows with durable SQLite/WAL commits:

| Workload | Throughput | Incremental write p50 / p95 / p99 | Feature CPU | Compact state | Integrity |
|---|---:|---:|---:|---:|---|
| 10,000 HOT-token events | 1,061/s | 0.504 / 1.398 / 3.006 ms | 2.611 ms | 2,469 bytes | PASS |
| 50,000 HOT-token events | 922/s | 0.757 / 1.663 / 3.837 ms | 1.824 ms | 2,756 bytes | PASS |
| 100,000 HOT-token events | 835/s | 0.814 / 1.851 / 4.581 ms | 1.531 ms | 2,846 bytes | PASS |

Peak traced Python memory was 376–392 KB for these incremental runs. The 100-
and 1,000-token lane tests reached all eight configured handlers concurrently,
processed every event and recorded zero failures. The earlier generic 10K launch
soak was 685/s; it is a cross-workload reference, not a same-workload lift claim.
Network source-to-Discord latency is excluded because no live provider/Discord
delivery occurred in this environment.

# G. REMAINING BLOCKERS

- A legitimate `DUNE_API_KEY` is required to acquire the 2024-present corpus and
  produce real counts, coverage, regimes, outcomes and same-universe metrics.
- A legitimate `HELIUS_API_KEY` and live runtime are required for real transport,
  reconnect, 429, source-to-feature and source-to-Discord evidence.
- No calibrated target model can be approved until a mature chronological corpus,
  hard-negative cohort, ablations and untouched outer test exist.
- Production deployment is explicitly outside this task and has not occurred.
- Push-triggered CI/staging evidence must be recorded from the final commit before
  CI/STAGING can be marked PASS.

Engineering status before final CI: single authority PASS; outcome-from-decision
PASS; evaluation hash PASS; repo-owned Dune SQL PASS; optional query ID PASS;
Helius-primary-ready PASS; PumpPortal optional PASS; Pump fast path PASS;
paginated backfill PASS; incremental state PASS; token concurrency PASS; O(N²)
buyer scan removed PASS; derived freshness PASS; latency lifecycle PASS; explicit
shadow semantics PASS; security PASS. Data complete NO, research complete NO,
production ready NO, task complete pending final CI/staging evidence.

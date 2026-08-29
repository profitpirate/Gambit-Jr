# Gambit Jr V1.5 convergence result

## Pass/fail delta

| Previously failing requirement | Action taken | Current status | Evidence |
|---|---|---|---|
| No autonomous continuation | Added a durable 15-phase leased/checkpointed convergence runner, CLI, machine report and immutable cycle ledger. | PASS | `python -m memecoin_bot.convergence run/status/report`; killed-process recovery test |
| One blocker stopped all later work | Each phase records its own result and independent phases continue after credential/data/research failures. | PASS | empty-environment test reaches all 15 evidence states |
| Process death/restart not proven | Hard-killed a real child after it built a HOT candidate and claimed a convergence phase; reopened both databases and safely reclaimed work. | PASS | `test_hard_killed_worker_is_recovered_by_a_real_restart` |
| No current provider admission truth | Added dated capabilities, credential preflight and real probes with coverage/latency/errors. | PASS engineering / BLOCKED_EXTERNAL for credentials | live-probe evidence JSON |
| Keyless live flow not proven | Real native Solana, DexScreener, GeckoTerminal and Bluesky transports returned data. | PASS transport only | 146 REST/RPC records plus 5 Bluesky envelopes; exact table below |
| Dune history not month-resumable | Added reviewed saved-query execute/poll/page client and 32 explicit month partitions. | PASS engineering / BLOCKED_EXTERNAL data | Dune contract test; `DUNE_API_KEY` and `DUNE_QUERY_ID` absent |
| Continuous >=24-month universe absent | Preserved the existing 7GB real corpus, bias labels and explicit month ledger; did not invent coverage. | FAIL_RESEARCH | only Jun–Jul 2026 has broad launch-universe depth; 32-month table below |
| Production operational history absent | Added read-only allowlisted operational import; no production DB copy was provided. | BLOCKED_EXTERNAL | `--operational-db` provider path implemented; no VPS access used |
| Social/narrative source absent | Added authorized Discord, authorized Telegram and keyless Bluesky plugins with privacy/PIT controls; extended DEX promotional observations. | BLOCKED_EXTERNAL / FAIL_RESEARCH | Bluesky transport live but zero CA matches; Discord/Telegram credentials/allowlists absent |
| Runner/failure/actionability conflated | Runtime maintains independent `P_RUNNER`, `P_FAILURE`, `P_ACTIONABLE`, evidence, contradiction, uncertainty, analogues and invalidation. | PASS engineering / FAIL_RESEARCH | thesis tests and rejected offline combined model |
| Prospective learning idle | Immutable shadow calls settle into outcome, error autopsy, reflection and PIT analogue memory; convergence revisits maturity/drift/challengers. | AWAITING_MATURITY | no matured live cohort yet; public route locked false |
| Full code/security/performance audit not proven | Added deterministic audit ledger, audit CLI, current dependency scan, real load/CPU/memory profile and explicit report. | PASS locally | `docs/v15-code-audit-final.md`; zero open audit findings |
| Public production readiness previously ambiguous | Engineering, intelligence and public readiness are separate; no auto promotion or deployment exists. | FAIL_RESEARCH | `PUBLIC PRODUCTION READY = NO` |

## Runner performance: unchanged evidence, no new approval

This implementation adds the machinery and live/keyless evidence; it does not manufacture a new evaluation window. The latest legitimate fixed-frequency results remain:

| Model/thesis | Frequency | Signals | Signals/day | 2X precision | 2X recall | 5X precision | 5X recall | 10X precision | 10X recall | 20X recall | 50X recall | Terminal failure | MAE | Call age | Latency |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| CONTROL exact-band | 1% | 2,361 | 157.4 | 15.21% | not restated | 3.60% | not restated | 1.48% | not restated | 5.98% | 4.17% | 13.05% | not restated | fixed historical band | offline |
| Previous V3 Quick 2X | 1% | not restated | not restated | 13.92% | not restated | 3.90% | not restated | 1.48% | not restated | 8.87% | 9.77% | 3.58% | not restated | fixed historical band | offline |
| Second-leg sell absorption | 1% | 2,361 | 157.4 | 20.03% | not restated | 3.52% | not restated | 1.14% | not restated | not restated | not restated | 13.47% | not restated | fixed historical band | offline |
| Combined thesis | 1% | 2,361 | 157.4 | 8.43% | not restated | 2.50% | not restated | 1.31% | not restated | not restated | not restated | 10.76% | not restated | fixed historical band | offline |
| Prospective adaptive thesis | shadow only | 0 matured | 0 | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable | live age pending | runtime stores latency |

The combined thesis remains rejected because it destroys useful specialist information. The sell-absorption branch is a promising 2X nomination hypothesis, not a multi-stage runner approval. Missing recall/MAE fields are not inferred.

### Thesis cohorts

| Thesis | Sample | 2X | 5X | 10X | 20X | 50X | Failure | Regime stability | Status |
|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| Second-leg sell absorption | 2,361 | 20.03% | 3.52% | 1.14% | unavailable | unavailable | 13.47% | unproven outside retired window | FAIL_RESEARCH / research-only |
| Control exact-band | 2,361 | 15.21% | 3.60% | 1.48% | 5.98% recall | 4.17% recall | 13.05% | drift observed | current control, not target-qualified |
| Combined thesis | 2,361 | 8.43% | 2.50% | 1.31% | unavailable | unavailable | 10.76% | unproven | REJECTED |
| Live adaptive runner thesis | 0 matured | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable | awaiting multiple regimes | AWAITING_MATURITY |

## Provider admission

Official facts were rechecked on 2026-08-29 against [Dune rate limits](https://docs.dune.com/api-reference/overview/rate-limits), [Helius credits](https://www.helius.dev/docs/billing/credits), [PumpPortal data API](https://pumpportal.fun/data-api/bonk-fun-data-api/), [DexScreener reference](https://docs.dexscreener.com/api/reference), [GeckoTerminal limits](https://apiguide.geckoterminal.com/faq) and [Bluesky Jetstream](https://bsky.network/docs/jetstream/).

| Provider | Role | Free/paid | Configured? | Live? | Events | Latency p50/p95 | Errors | Coverage | Status |
|---|---|---|---:|---:|---:|---:|---:|---|---|
| Native Solana RPC | chain fallback/health | public rate-limited | yes | yes | 1 | 510.20/510.20ms | 0 | real mainnet slot | SECONDARY; not production primary |
| DexScreener | market/promotion enrichment | keyless | yes | yes | 120 / 105 tokens | 206.45/295.77ms | 0 | 30 profiles, boosts, takeovers, ads each | ENRICHMENT |
| GeckoTerminal | pool/market redundancy | keyless, 30/min | yes | yes | 20 / 20 tokens | 208.21/208.21ms | 0 | Solana new-pool page 1 | SECONDARY |
| Bluesky Jetstream | social transport | keyless | yes | yes transport | 5 envelopes, 0 CA matches | 814.36/915.66ms | 0 | public posts only | ENRICHMENT transport; social result FAIL_RESEARCH |
| Helius | selective Solana primary/enrichment | free credit tier | no | no | 0 | unavailable | 1 preflight | no key | BLOCKED_EXTERNAL |
| PumpPortal | launch/migration speed | free event classes; keyed | no | no | 0 | unavailable | 1 preflight | no key | BLOCKED_EXTERNAL |
| Dune | indexed historical backbone | free credit/rate tier | no | no | 0 | unavailable | 1 preflight | no key/query id | BLOCKED_EXTERNAL |
| Authorized Discord | social | existing bot credentials | no allowlist | no | 0 | unavailable | 0 | authorized channels only | BLOCKED_EXTERNAL |
| Telegram | social | official API | no | no | 0 | unavailable | 0 | authorized/public channels only | BLOCKED_EXTERNAL |
| Reddit | slow narrative | approval/credential dependent | no | no | 0 | unavailable | 0 | not integrated as critical | BLOCKED_EXTERNAL |
| X direct API | social | pay-per-use reads | no | no | 0 | unavailable | 0 | official free path not useful | REJECTED |

## Historical month ledger

Counts are never split between months when the source artifact did not provide a verified partition count. `combined` means the number is verified for that source window but a per-month allocation would be invented. Full-universe coverage means broad winners/failures within the stated partial window, not a full calendar month.

| Month | Universe type | Tokens | Trades/events | Outcome labels | Source | PIT quality | Usable? |
|---|---|---:|---:|---:|---|---|---|
| 2024-01 | migrated/survivor-only | 3,751 combined Jan–Nov | 3,751 combined | migration context only | Coin-Meme | context-only | no unbiased evaluation |
| 2024-02 | migrated/survivor-only | combined | combined | context only | Coin-Meme | context-only | no unbiased evaluation |
| 2024-03 | migrated/survivor-only | combined | combined | context only | Coin-Meme | context-only | no unbiased evaluation |
| 2024-04 | migrated/survivor-only | combined | combined | context only | Coin-Meme | context-only | no unbiased evaluation |
| 2024-05 | migrated/survivor-only | combined | combined | context only | Coin-Meme | context-only | no unbiased evaluation |
| 2024-06 | migrated/survivor-only | combined | combined | context only | Coin-Meme | context-only | no unbiased evaluation |
| 2024-07 | migrated/survivor-only | combined | combined | context only | Coin-Meme | context-only | no unbiased evaluation |
| 2024-08 | migrated/survivor-only | combined | combined | context only | Coin-Meme | context-only | no unbiased evaluation |
| 2024-09 | migrated/survivor-only | combined | combined | context only | Coin-Meme | context-only | no unbiased evaluation |
| 2024-10 | migrated/survivor-only | combined | combined | context only | Coin-Meme | context-only | no unbiased evaluation |
| 2024-11 | migrated/survivor-only | combined | combined | context only | Coin-Meme | context-only | no unbiased evaluation |
| 2024-12 | migrated/coordination context | 41,470 combined Dec–Mar | 3,328,964 coordination edges combined | behavioral labels | MELT | context/research-only | no unbiased evaluation |
| 2025-01 | migrated/coordination context | combined | combined | combined | MELT | context/research-only | no unbiased evaluation |
| 2025-02 | migrated/coordination context | combined | combined | combined | MELT | context/research-only | no unbiased evaluation |
| 2025-03 | migrated/coordination context | combined | combined | combined | MELT | context/research-only | no unbiased evaluation |
| 2025-04 | missing | 0 | 0 | 0 | none | none | no |
| 2025-05 | missing | 0 | 0 | 0 | none | none | no |
| 2025-06 | missing | 0 | 0 | 0 | none | none | no |
| 2025-07 | missing | 0 | 0 | 0 | none | none | no |
| 2025-08 | missing | 0 | 0 | 0 | none | none | no |
| 2025-09 | missing | 0 | 0 | 0 | none | none | no |
| 2025-10 | missing | 0 | 0 | 0 | none | none | no |
| 2025-11 | missing | 0 | 0 | 0 | none | none | no |
| 2025-12 | missing | 0 | 0 | 0 | none | none | no |
| 2026-01 | missing | 0 | 0 | 0 | none | none | no |
| 2026-02 | sentiment context | 86,838 mints combined Feb–Mar | 193,410 analyses combined | no runner labels | Pump Studio | PIT timestamps, contextual | narrative research only |
| 2026-03 | sentiment context | combined | combined | no runner labels | Pump Studio | PIT timestamps, contextual | narrative research only |
| 2026-04 | missing | 0 | 0 | 0 | none | none | no |
| 2026-05 | missing | 0 | 0 | 0 | none | none | no |
| 2026-06 | broad launch universe, partial month | 798,430 combined Jun–Jul | 33,581,765 raw trades combined; 20,400,058 valid ordered | 798,430 combined | Pumpfun Memecoin Corpus | PIT-safe after exclusions | usable retired research window |
| 2026-07 | broad launch universe, partial month | combined | combined | combined | Pumpfun Memecoin Corpus | PIT-safe after exclusions | usable retired research window |
| 2026-08 | partial forward universe | 85,442 | 730,850 observations | 0 target-bearing 2X+ labels | Trenches | PIT-safe forward observations | context/shadow only |

Warehouse reality: 1,801,980 normalized events, 1,801,978 PIT feature snapshots, 798,430 outcomes, 18/200 high-relevance research sources, 0 approved features, 0 approved challengers. Historical completion is **FAIL_RESEARCH**, not 24 months.

## Social evidence

| Source | Live? | Mentions | Unique authors | Coverage | Latency | Lead/lag availability | Incremental predictive result |
|---|---:|---:|---:|---|---:|---|---|
| Bluesky Jetstream | transport yes | 0 matching CAs in 5 envelopes | 0 | public posts, known CAs only | 814.36/915.66ms p50/p95 | implemented, no qualifying sample | unavailable / FAIL_RESEARCH |
| DexScreener social/promotion metadata | yes | 120 events / 105 tokens | not applicable | profiles, boosts, takeovers, ads | 206.45/295.77ms | event timestamp path available | not runner proof; unavailable |
| Authorized Discord | no | 0 | 0 | explicit allowlist only | unavailable | implemented | BLOCKED_EXTERNAL |
| Telegram | no | 0 | 0 | authorized/public configured channels only | unavailable | implemented | BLOCKED_EXTERNAL |
| X direct | no | 0 | 0 | official pay-per-read rejected | unavailable | unavailable | unavailable |

## Audit summary

| Code issues found | Fixed | Remaining open | Dead code removed | Queries optimized | Race/restart | Security | Dependencies |
|---:|---:|---:|---:|---:|---|---|---|
| 11 deterministic findings plus 7 explicit release findings | 5 persisted + process/index/load fixes | 0 high/critical code findings | 0; none proven obsolete | 1 run-order index; prior operational indexes retained | hard-kill and lease reclaim PASS | secret/privacy/public-route/PIT controls PASS | `pip check` PASS; `pip-audit` no known vulnerabilities |

The detailed ledger, performance numbers, second self-review, database and security sections are in `docs/v15-code-audit-final.md`.

## Final local validation

| Gate | Result |
|---|---|
| Full pytest | PASS — 290 passed + 5 subtests; one third-party `audioop` deprecation warning |
| Migration/restart/provider/Discord/replay/chaos/convergence E2E subset | PASS — 102 passed; same warning |
| Ruff | PASS — `ruff check .` → `All checks passed!` |
| Dependency consistency | PASS — `pip check` → `No broken requirements found.` |
| Dependency vulnerabilities | PASS — `pip-audit 2.9.0 --local` → `No known vulnerabilities found`; local unpublished package skipped as expected |
| Package | PASS — isolated sdist and wheel build; convergence package and warehouse migration 006 present |
| 10,000-event soak | PASS — 10,256 persisted, 10,000 duplicates suppressed, zero duplicate keys, 685.35/s, 56.07% one core, 239,142 peak traced bytes |
| Database | PASS — quick/integrity checks OK, zero FK violations, WAL, reconciliation difference 0 |
| Docker / isolated staging | Awaiting push-triggered GitHub workflow; no local Docker/VPS action performed |

## Readiness state

| Gate | State |
|---|---|
| Persistent orchestration | PASS |
| Code/security/dependency/performance audit | PASS locally |
| Keyless provider transport | PASS with reduced redundancy |
| Credentialed primary providers | BLOCKED_EXTERNAL |
| Historical >=24-month processed memory | FAIL_RESEARCH |
| Research source gate | FAIL_RESEARCH (18/200) |
| Wallet/funder/bundle/migration real validation | FAIL_RESEARCH |
| Social predictive contribution | FAIL_RESEARCH |
| Prospective shadow maturity | AWAITING_MATURITY |
| Intelligence production ready | FAIL_RESEARCH |
| Public production ready | **NO** |

No public route, production deployment, threshold relaxation or automatic approval occurred.

## Operator commands

```bash
# Run/resume the full independent research cycle
python -m memecoin_bot.convergence run

# View the phase ledger
python -m memecoin_bot.convergence status

# Run real provider admission probes / view provider health
python -m memecoin_bot.convergence providers --probe
python -m memecoin_bot.convergence providers

# View 32-month acquisition progress
python -m memecoin_bot.convergence historical

# Run repository/security/database/performance audit
python -m memecoin_bot.convergence audits

# View current champion and latest precision/recall
python -m memecoin_bot.convergence champion
python -m memecoin_bot.convergence metrics

# Emit/read the daily machine report
python -m memecoin_bot.convergence report

# Run live operational capture in existing shadow mode
memecoin-bot run

# Full source/restart/chaos/E2E validation
python -m pytest
python scripts/v15_load_soak.py --events 10000 --queue-size 256 --burst-multiplier 3
```

Configure the historical store/archive and optional read-only operational copy with the CLI's global `--warehouse`, `--archive` and `--operational-db` arguments. Provider credentials are described only in `docs/FREE_PROVIDER_SETUP.md`.

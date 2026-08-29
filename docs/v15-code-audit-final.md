# Gambit Jr V1.5 final code, security and performance audit

## Round-two authoritative architecture ledger

| ID | Severity | File/function | Problem and impact | Fix | Regression test/evidence | Status |
|---|---|---|---|---|---|---|
| R2-001 | CRITICAL | `service.py` / signal policy | Legacy classification and V1.5 state could compete for final routing. | Final promotion now consumes only `RunnerDecision.routes_alert`; earlier engines are stored as named controls. | authoritative decision + service regression suite | FIXED |
| R2-002 | HIGH | `realtime/thesis.py` | Fixed sigmoids were named probabilities despite no empirical calibration. | Public thesis contract now exposes heuristic scores and explicit `HEURISTIC_NOT_CALIBRATED`; calibrated target slots remain null. | `test_runner_decision_is_single_authority_and_never_labels_heuristics_as_probability` | FIXED |
| R2-003 | HIGH | `realtime/learning.py` | A hand-built control/V3 average minus failure could destroy positive runner signal. | Removed hidden hybrid from calculation and comparison frontiers. | repository scan for `control_v3_hybrid` | FIXED |
| R2-004 | CRITICAL | learning/outcomes | Discovery-based peaks could label a late decision as a winner. | Added immutable decision entry state and target outcomes beginning at `decision_at`. | `test_outcomes_start_at_decision_not_discovery` | FIXED |
| R2-005 | CRITICAL | `historical/evaluation.py` | Different universes could be compared as apparent lift/deterioration. | Canonical contract/member hash and fail-closed same-universe check. | `test_evaluation_hash_refuses_cross_universe_comparison` | FIXED |
| R2-006 | HIGH | `historical/runner_models.py` | Linear univariate averaging missed interactions; model responsibilities were conflated. | Chronological regularized logistic and nonlinear HGB candidates; separately fitted runner targets, terminal failure and copyability. | nonlinear/independent target research test | FIXED |
| R2-007 | HIGH | `realtime/features.py` | Sell absorption, capital efficiency and sequences were reduced to coarse endpoints. | Added 5/10/20/30s responses, second-sell response, capital milestones and eight sequence bands. | sell-absorption and feature projector tests | FIXED |
| R2-008 | HIGH | `realtime/features.py` | Repeated buyer scans introduced avoidable O(N²) cost. | Single-pass `Counter`, sets and persisted actor/window counters. | 10K test + 10K/50K/100K benchmark | FIXED |
| R2-009 | HIGH | `realtime/incremental.py` | Every event rebuilt full token history. | Compact restart-safe O(1) scalar/actor/window updates with one transaction per event; full projector is reconciliation fallback. | 100K bounded-state benchmark | FIXED |
| R2-010 | HIGH | `realtime/lanes.py` / service | One serial worker allowed an unrelated HOT token to block others. | Eight bounded hash lanes preserve same-token order and permit cross-token concurrency. | ordering/concurrency test; 100/1,000-token benchmark | FIXED |
| R2-011 | HIGH | `realtime/providers.py` | Pump event emission waited for `getTransaction`. | Decode Anchor log payload first; queue only missing enrichment asynchronously. | fast path asserts zero RPC calls | FIXED |
| R2-012 | HIGH | `realtime/providers.py` | RPC enrichment could duplicate work or exhaust quotas. | Bounded queue/semaphores/retries, LRU transaction cache, signature coalescing and public fallback. | fallback/rate-limit/provider tests | FIXED |
| R2-013 | HIGH | `realtime/providers.py` | Shallow recovery could silently miss long gaps. | Cursor-based paginated signatures with maximum pages and explicit `gap_incomplete`. | paginated backfill boundary test | FIXED |
| R2-014 | HIGH | `historical/sql/dune` / providers | User had to own a saved Dune query and result paging. | Eight versioned repo SQL templates, strict registry, direct execute/poll/page/Parquet/checkpoint; saved ID optional. | Dune schema/parameter/empty/page/resume tests | FIXED |
| R2-015 | HIGH | config/main/provider sources | Helius key did not automatically own primary Solana transport. | Derive primary RPC/WSS only for public defaults; public RPC remains fallback; key is redacted/excluded from fingerprint. | config/fallback tests | FIXED |
| R2-016 | MEDIUM | provider admission | Region-blocked PumpPortal could block production capability state. | Classified as optional redundancy when native/Helius paths satisfy core transport. | provider capability tests | FIXED |
| R2-017 | HIGH | evidence/latency | Freshness and delivery latency contained asserted or null fields. | Provenance-derived freshness plus measured enqueue/delivery lifecycle; non-routes use explicit states. | freshness and delivery latency tests | FIXED |
| R2-018 | CRITICAL | shadow configuration | Research inference could imply a user-facing alert. | Separate research, operator and public states; both alert routes default false. | route-state tests and staging config | FIXED |
| R2-019 | MEDIUM | `database/store.py` | Store remained broad and risked duplicated ownership if split carelessly. | Kept one transactional authority; added bounded domain facades for decisions, outcomes and incremental state instead of a second DB owner. | migration/restart/full suite | PROVEN NON-ISSUE / IMPROVED |
| R2-020 | CRITICAL | repository/security | Data, keys, credentials or generated artifacts could enter Git. | SQL/templates only; Parquet, databases, `.env`, caches and output roots remain ignored; provider errors redact keys. | final path/secret scan | PASS |

Round-two scans also checked duplicate brains, duplicate calculations, unused
gates, sync hot-path SQL, N+1/O(N²) loops, unbounded queues/memory, duplicate RPC,
future leakage, wrong outcome anchors, inconsistent units, silent fallbacks,
null latency and fake freshness. Remaining legacy probability column names belong
to the additive V1.5 research schema and are compatibility storage only; the
current object/API labels them as heuristics and they have no route authority.

### Round-two measured performance

The new authoritative benchmark measured 10K/50K/100K HOT-token streams at
1,061/922/835 events per second. Write p95 was 1.398/1.663/1.851 ms, compact
state stayed below 2.9 KB, peak traced memory stayed below 0.4 MB, and every
SQLite run finished WAL `quick_check=ok` with zero foreign-key violations.
Eight token lanes processed 100 and 1,000 distinct tokens with eight concurrent
handlers and zero failures. See `docs/v15-authoritative-runner-final.md` for the
full measured table and explicit exclusions.

Audit date: 2026-08-29. Scope: `src/`, `tests/`, `scripts`, packaging, the historical warehouse schema and operational SQLite schema. The final deterministic repository audit parsed 145 Python files. Its current result contains 13 findings: 5 `FIXED`, 6 `PASS`, 2 `REVIEWED`, 0 `OPEN`.

## Finding ledger

| File | Function/class | Problem | Severity | Fix | Test added/evidence | Status |
|---|---|---|---|---|---|---|
| `src/memecoin_bot/convergence/runner.py` | `ConvergenceOrchestrator` | Data, research, audit and shadow phases had no durable owner. | HIGH | Added leased phase claims, checkpoints, retry states, immutable cycle summaries and independent continuation after blockers. | `tests/test_v15_convergence.py` | FIXED |
| `src/memecoin_bot/historical/providers.py` | `DuneExecutionClient` / `DuneMonthHistoricalProvider` | Latest-result retrieval could mix periods and could not resume reviewed month partitions. | HIGH | Added execute/poll/page flow, bounded 429 handling, explicit 2024-01..2026-08 parameters and source/availability timestamps. | `test_dune_month_provider_executes_reviewed_partition_and_resumes` | FIXED |
| `006_convergence_orchestrator.sql` | `convergence_phases` | Run-status reads lacked a dedicated run/ordinal access path. | LOW | Added `idx_convergence_run_ordinal(run_id, ordinal)`; the measured 15-row query remains only 0.0175ms p95 and SQLite may reasonably choose the primary-key index. | query plan + migration repetition in full suite | FIXED |
| `src/memecoin_bot/social/sources.py` | `social_events_from_text` | Raw social content and stable user/channel identifiers would create avoidable privacy retention. | HIGH | Persist content SHA-256 and salted hashes only; require explicit Discord/Telegram allowlists and known-token matches. | `test_social_plugins_preserve_pit_and_privacy_and_reject_unknown_tokens` | FIXED |
| `src/memecoin_bot/convergence/providers.py` | `ProviderRegistry` | Credential-bearing request URLs or values could leak into logs/evidence. | HIGH | Credential state is redacted and probe evidence never stores credentialed URLs. | provider preflight test + repository secret scan | FIXED |
| `src/memecoin_bot/realtime/providers.py` | `HeliusCuratedSource` | Provider commentary used stale product assumptions. | LOW | Retained standard `logsSubscribe`, documented current credit metering and moved dated facts into the capability registry. | provider capability test | FIXED |
| `tests/test_v15_convergence.py` | hard-kill E2E | Graceful reopen tests did not prove recovery from process death. | HIGH | A real child process creates a HOT candidate, acquires a convergence lease, is forcibly killed, and a new process verifies HOT state, WAL integrity, zero public output and lease recovery. | `test_hard_killed_worker_is_recovered_by_a_real_restart` | FIXED |
| repository | broad exception inventory | Broad boundaries can hide provider or optional-service failures. | MEDIUM | Audited 17 handlers; retained only isolation/entry-point boundaries with explicit state/log behavior. | AST audit inventory | REVIEWED |
| repository | mutable module-state inventory | Mutable module containers can become parallel state authorities. | MEDIUM | Audited 50 literals; authoritative runtime/research state remains SQLite or instance-owned bounded structures. | AST audit inventory | REVIEWED |
| repository | blocking async inventory | `time.sleep` inside async code would stop ingestion. | HIGH | AST audit found zero blocking sleeps inside async functions. | `python -m memecoin_bot.convergence audits` | PASS |
| repository | credential scan | Inline credentials could expose Discord/provider accounts. | CRITICAL | Refined the entropy scan to distinguish fixtures, and verified no real value is staged. | deterministic audit + pre-commit path/secret review | PASS |
| `pyproject.toml` | dependencies/extras | Runtime could accidentally rely on undeclared transitive packages. | HIGH | Core, research, timezone and optional Telethon dependencies are declared in their owning groups. | build, isolated CI install, `pip check` | PASS |
| local validation environment | vulnerability database | The pre-existing pip 25.0.1 installer had six published advisories. | HIGH | Upgraded the non-repository validation environment to pip 26.2.1; `pip-audit 2.9.0 --local` then reported no known vulnerabilities. | current PyPI advisory query, 2026-08-29 | FIXED |
| SQLite stores | integrity/foreign keys | Corruption or broken references invalidate restart/evidence claims. | CRITICAL | Read-only integrity/FK checks are release audit controls; schemas use FK enforcement, WAL and bounded busy timeouts. | audit database section + soak/restart tests | PASS |
| `scripts/v15_load_soak.py` | `run` | Performance evidence omitted CPU consumption. | MEDIUM | Added process CPU seconds and single-core utilization to the bounded load report. | 10,000-event final soak | FIXED |
| operational model/research schemas | public-route constraints | A research challenger or thesis could accidentally reach public alerts. | CRITICAL | Database CHECKs/triggers and runtime contracts force every convergence, thesis, challenger and report route to false. | public-route mutation tests | PASS |
| historical/realtime feature stores | timestamp boundaries | Outcome, wallet, analogue or social knowledge could leak backwards. | CRITICAL | Separate observed/received/available/outcome-available fields and `available_at <= decision_at` lookups; naive timestamps fail. | PIT, analogue future-outcome and social ordering tests | PASS |
| canonical event fabric | duplicate truth | Native/Portal observations could form two tokens or silently overwrite conflicts. | HIGH | One canonical key, source confirmation rows and explicit conflict evidence. | native+Portal reconciliation, conflict and 10,050 duplicate-storm tests | PASS |
| repository | proven dead code | Deleting historical/research paths without proof would destroy reproducibility. | INFO | Vulture and manual inventory found no production module safe to delete; zero files removed and compatibility remains isolated. | prior V1.5 audit plus current AST inventory | PASS |

## Architecture and second independent review

The operational store owns live canonical events, candidates, features, thesis transitions and immutable shadow calls. The historical warehouse owns raw evidence, normalized events, PIT features, outcomes, research decisions, holdouts and convergence jobs. Provider adapters do not own business truth. Public output remains owned by the existing operational signal/outbox path; convergence cannot invoke it.

The adversarial second pass specifically checked:

- **implemented but never consumed:** Bluesky/Telegram sources are installed into the runtime source list when enabled; authorized Discord observations enter the existing service canonical-event handler. Disabled sources report explicit state.
- **tested only with mocks:** Dune remains credential-blocked and therefore is not called live; native Solana, DexScreener, GeckoTerminal and Bluesky received real network probes. Mocked Dune contracts are not reported as live evidence.
- **configured but not receiving:** no credentialed provider is configured or passed. Five Bluesky envelopes with zero matching CAs prove transport only.
- **feature always null:** social, wallet-linkage and migration fields remain `UNKNOWN` until real evidence arrives; none is turned into zero or an approval.
- **fallback hides failure:** every optional source has an independent admission/probe state; keyless mode continuing does not turn a blocked primary into PASS.
- **research model accidentally public:** schema constraints, immutable call tables and tests prove `public_route=false`.
- **future leakage:** Dune acquisition time is distinct from source time; analogue and outcome reads enforce availability; inspected holdouts are retired.
- **duplicate truth:** canonical reconciliation and conflicts preserve one token/event authority without discarding disagreements.

## Performance profile

| Workload | Result | Interpretation |
|---|---:|---|
| Fresh 10,000 sustained events + 3x burst | 10,256 persisted in 14.965s; 685.35/s | PASS; 10,000 replay duplicates suppressed, 512 bounded drops by policy |
| CPU | 8.391 process CPU seconds; 56.07% of one core | bounded local Windows run, not a VPS capacity promise |
| Memory | 239,142 peak traced bytes | Python allocations measured by `tracemalloc`; excludes SQLite OS cache |
| 7GB warehouse PIT lookup | p95 0.0274ms | prior representative 100-sample profile |
| approved empty-context lookup | p95 0.0348ms | prior representative 100-sample profile |
| coverage query | p95 0.0650ms | prior representative 100-sample profile |
| Discord payload rendering | p95 0.0778ms | prior local 100-sample render profile; network delivery excluded |
| keyless provider probes | 206.45–814.36ms p50 | live network evidence; see `docs/evidence/v15-convergence-live-probe.json` |
| convergence status query | p95 0.0175ms | final 20-sample local audit; PK index selected for the fixed 15-row phase set |
| previous native live source→feature | p95 5,827ms | provider arrival dominated; availability p95 was 1.63ms; not an SLA |

The load database finished `quick_check=ok`, zero FK violations, WAL mode, zero duplicate keys and reconciliation difference zero. No measured SQLite query was a practical bottleneck; the dedicated run/ordinal index provides a stable path as the ledger grows. API calls remain batched/bounded: DexScreener batches up to 30 addresses, Gecko obeys 30 calls/minute, and providers have bounded retry/circuit behavior.

## Database and recovery audit

- migrations are additive and repeatable; no existing user database is deleted or rewritten;
- all convergence phase claims are atomic compare-and-set updates with owner and expiry;
- expired leases become retryable before a new worker claims them;
- the hard-kill E2E proves operational HOT state and convergence work survive an ungraceful process exit;
- immutable shadow calls/outcomes reject conflicting updates;
- WAL, FK checks, quick/integrity checks, targeted indexes and bounded queues are release gates;
- raw archives remain content-addressed and outside Git; the 7GB local evidence warehouse is ignored;
- backup/restore is an operator filesystem concern and no production database was accessed in this task.

## Security audit

- no `.env`, API key, Discord token, Telegram session, database, raw corpus, log or evidence cache is committed;
- all SQL values originating outside fixed internal identifiers use parameters; dynamic table names are allowlisted;
- social inputs are accepted only from authorized Discord/Telegram scopes and only known contract addresses become observations;
- content bodies are not retained by the new social plugins;
- no pickle or unsafe YAML/deserialization path was introduced;
- file acquisition writes through controlled archive/store roots and content hashes;
- the project remains read-only: no private key, signing, transaction, swap or fund-transfer path exists;
- `pip check` passes and current `pip-audit --local` reports no known third-party vulnerabilities (the local project itself is correctly skipped because it is not a PyPI release).

## Remaining audit limitations

No local Docker engine or VPS is available on this Windows host. Docker build, isolated Compose, staging health and staging acceptance must therefore be evidenced by the push-triggered workflow. Sustained Discord delivery, simultaneous loss of every real provider, OS-level disk-full behavior and production-VPS CPU/RSS remain staging/operational acceptance—not claims manufactured by local fixtures.

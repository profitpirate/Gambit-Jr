# Gambit Jr V1.5 final code, security and performance audit

Audit date: 2026-08-29. Scope: `src/`, `tests/`, `scripts/`, packaging, the historical warehouse schema and operational SQLite schema. The final deterministic repository audit parsed 136 Python files and 42,877 lines. Its latest persisted result contains 11 findings: 5 `FIXED`, 4 `PASS`, 2 `REVIEWED`, 0 `OPEN`.

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

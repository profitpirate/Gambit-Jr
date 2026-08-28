# Gambit Jr V1.5 production closure

This document records the production-owned V1.5 path and the Discord interaction contract. It distinguishes evidence the runtime consumes from evidence that remains `UNKNOWN`; missing evidence is never converted to zero.

## Production function trace

| Stage | Production owner | Persisted or emitted truth |
|---|---|---|
| Process assembly | `memecoin_bot.main.build` | Validates settings, migrates the database, reconciles stale/V1.4 state, registers version/config fingerprints, and constructs providers, discovery, safety, service, notifier, launch sources, and tracker. |
| Poll discovery | `IntelligenceService.scan_once` → `DiscoveryPoller.poll` | Provider discoveries become `DiscoveryEvent` values. Provider failure is isolated to the scan cycle. |
| Direct launch discovery | launch source callback → `IntelligenceService.enqueue_launch_event` → `handle_launch_event` | `Store.record_launch_event`, `upsert_discovery`, `ensure_candidate`, and `link_launch_candidate` preserve source identity and duplicate protection. |
| Launch T0 | `IntelligenceService.handle_launch_event` → `t0_decision` | `Store.record_v14_decision` preserves launch evidence; qualifying Genesis calls use `record_immutable_call` and idempotent `enqueue_v14_event`. |
| Candidate creation | `IntelligenceService.evaluate` | `Store.upsert_discovery` and `ensure_candidate`; existing candidates return `KNOWN_CANDIDATE` instead of creating a second lifecycle. |
| Market T0 and enrichment | `IntelligenceService._monitor_candidate` | Market snapshot, chain safety, optional GMGN, previous snapshots, developer, narrative, social, on-chain, and momentum evidence. `save_snapshot`, `save_gmgn_intelligence`, and candidate-attempt methods retain the observed inputs. |
| Provider resilience | `parallel_enrichment` and `ResilientJsonClient` | A failed optional provider becomes unavailable evidence. Safety failure is represented explicitly; it cannot silently become a passing zero. Provider health is persisted through `Store.set_provider_health`. |
| Stage selection | `_v15_stage` | Launch phase selects `REVIVAL` or `BONDING`; a live pair selects `MIGRATED`; otherwise `NEW`. Restart monitoring reconstructs the discovery metadata from the stored token row. |
| Survival and payoff | `survival_engine` and `payoff_engine` inside `_monitor_candidate` | Their scores/grades are inputs to V1.5, the candidate decision, and the outbound signal payload. They are evidence-derived estimates, not profitability claims. |
| Tradeability | `tradeability` inside `_monitor_candidate` | Conservative constant-product impact estimates at $50/$100/$250/$500/$1,000 are persisted by `Store.record_v15_tradeability`; they are estimates, never executable quotes. |
| Actor concentration | `_economic_holder_rows` → `economic_concentration` | Known non-economic LP/burn/locker labels are excluded. Connected/deployer-related concentration and actor independence feed V1.5. With no configured GMGN holder evidence these fields remain `UNKNOWN`. |
| Buyer replacement | `buyer_trajectory` | Stored GMGN buyer cohorts feed replacement/retention quality. Without cohorts its state and score remain `UNKNOWN`. |
| Freshness/conflict | `_evidence_age_seconds` and `_monitor_candidate` provenance construction | `Store.record_v15_provider_evidence` stores provider, retrieval time, age, confidence and conflict state. Stale evidence and safety conflict warnings cap V1.5 public quality. |
| V1.5 decision | `evaluate_v15` | Stage-specific runner score, independent failure score, evidence coverage, setup conviction, entry state, survival grade, critical unknowns, conflicts, why-now, and public tier. Premium requires sufficient coverage, no critical unknown/conflict/stale evidence, and open entry. |
| Immutable decision | `Store.record_v15_decision` | Every observed decision is versioned and fingerprinted. The first public V1.5 call is inserted once into `v15_t0_calls`; database triggers reject update and delete. |
| Signal qualification | `_monitor_candidate` | Both the legacy normalized classification and an authoritative public V1.5 tier must qualify. `SILENT_WATCH` and `REJECT` cannot create a new public signal. |
| Signal persistence/outbox | `signal_payload` → payload V1.5 enrichment → `Store.create_signal` | Signal creation, candidate transition, and outbox insertion are transactional/idempotent under database constraints. The outbound payload carries the authoritative V1.5 tier and decision facts. |
| Guild routing | `IntelligenceService.flush_outbox` → `Store.alert_allowed` | `SIGNAL` delivery is allowed only for `PREMIUM`, `STRONG`, `HIGH_RISK_MOMENTUM`, or `CATALYST_REVIVAL`. Per-guild and fallback-channel delivery records prevent restart duplicates. |
| Discord automatic card | `format_discord_event` → `DiscordNotifier.send`/`send_to` | The V1.5 tier, runner/failure, coverage/entry and survival fields are rendered, validated by `validate_webhook_payload`, then sent. |
| Outcome tracking | `SignalTracker.monitor_once` | `Store.update_tracking` stores current/peak/drawdown truth; `record_milestones` atomically claims each milestone and creates an idempotent outbox event. Historical T0 is not mutated. |
| Performance attribution | `Store.performance`, `right_tail_performance`, `v15_performance` | `/performance` and its menu page read stored outcomes and keep signal precision, right-tail/missed-runner metrics, failure metrics, and sample size separate. Small samples are labeled unreliable. |

## Discord response contract

`InteractionResponder` owns command acknowledgement and delivery. A command chooses public/private visibility before transport. Fast commands use the initial interaction endpoint; only provider-bound `/scan` and `/compare` defer. Deferred commands and component updates use an exact original-response PATCH with present fields only. The payload excludes generic send-only defaults and null placeholders; `followup.send` is reserved for additional notices.

`MenuView(timeout=900)`, `ScanView(..., timeout=900)`, and `TokenActionView(timeout=900)` are the finite views attached to private messages. Their stateless `timeout=None` variants are restart routers registered with `Client.add_view`; stable custom IDs allow menu, Refresh, Watch, and Copy CA dispatch after restart, but they do not make an ephemeral message live forever. An expired private session is recreated with `/menu` or `/scan`.

All card/view payloads pass `discord.validation` before transport. HTTP failures record sanitized status, Discord code/text, route/method, command, interaction/ack/defer state, visibility, payload kind, content/embed/view/component facts, duration, and result. Tokens and webhook/interaction paths are redacted.

## Explicit external or unavailable truth

- Real live Discord delivery can only be accepted after deployment and the live command/component checklist; local tests exercise discord.py's real state machine and serializer without credentials.
- GMGN-derived wallet, holder, actor, and buyer-cohort facts are `UNKNOWN` when GMGN is disabled, unavailable, stale, or omits those fields.
- Transfer restrictions or concentration unsupported by a chain provider remain explicit critical unknowns and can cap public signal quality.
- Tradeability and payoff are deterministic estimates based on observed inputs, not order simulation, execution, or future return evidence.
- The product remains read-only: there is no wallet connection, private-key/seed handling, signing, swap, approval, or fund transfer path.

## Deterministic runtime gate

The production image pins `discord.py==2.7.1`. Run `python scripts/production_acceptance.py` inside the container. It exits non-zero unless the installed package versions, complete migration chain, SQLite quick check/WAL, state reconciliation, V1.5 schema/fingerprint, exact 24-command source set, cards, views, raw automatic alert, provider states, and authoritative V1.5 routing all pass. Its output contains configuration booleans and state names only—never credential values or endpoint URLs.

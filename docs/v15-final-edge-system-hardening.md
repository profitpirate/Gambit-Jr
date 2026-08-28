# Gambit Jr V1.5 final edge and system-hardening checkpoint

This checkpoint is a **release-candidate engineering result, not a production deployment**. Live
model behaviour remains CONTROL_V15. CANDIDATE_V15 remains research-only and no historical feature
is approved.

## Data and research

1. **Data added:** 18 raw trade partitions plus the full pre-graduation snapshot file; 19 new
   artifacts and 6,312,131,110 bytes, all SHA-256 matched to the publisher manifests.
2. **Date range:** 2026-06-05 10:12:28+01:00 through 2026-07-14 16:02:54+01:00.
3. **Launch count:** 798,430 total; 740,800 have an actionable T+3m entry row.
4. **Outcome count:** 740,800 objective 48-hour price paths; 7-day outcomes are unavailable.
5. **Failure count:** 722,945 did not reach 2x in 48 hours; 161 source-published rugs. These are
   kept separate because a non-runner is not automatically a rug.
6. **Runner cohorts at T+3m:** 17,855 at 2x, 3,833 at 5x, 1,733 at 10x, 924 at 20x and 478 at 50x.
7. **Wallet coverage:** 1,006,641 distinct wallets in raw trades; stale aggregate activity fields
   remain excluded.
8. **Creator coverage:** 166,751 creators; 46,519 repeat creators and 15,998 with five launches.
9. **Funder coverage:** 67,722 token–funder relationships across 7,997 funders.
10. **Buyer coverage:** 9,017,065 wallet–mint first-hour states; 572,910/740,800 actionable launches
    have a reconstructed buyer trajectory.
11. **BNB coverage:** zero new unbiased BNB launch outcomes. Dune requires `DUNE_API_KEY` plus
    reviewed query SQL/IDs; Bitquery requires export access; archive reconstruction requires
    verified factory addresses, ABIs/event definitions and a budgeted decoder backfill.
12. **Model changes:** no live model behaviour changed. CONTROL_V15 was frozen before research.
13. **Removed features:** no live features removed; stale wallet aggregates and corrupt/raw
    concentration fields remain research-excluded.
14. **Added features:** no production-approved features. Raw T+30s/T+1m/T+3m/T+5m/T+10m/T+30m/
    T+1h buyer trajectories are research inputs only.

## Model comparison

15. **CONTROL_V15:** reconstructed 48-hour 2x precision 14.27% (1,128 signals).
16. **CANDIDATE_V15:** 16.22% (1,128 signals), research-only.
17. **Safety-filtered momentum:** 15.34% (1,128 signals).
18. **Primary 2x precision:** 16.22%; target is 80%; **FAIL TARGET**.
19. **5x precision:** candidate 2.39%.
20. **10x precision:** candidate 0.18%.
21. **Major-runner recall:** candidate 0.00%; unacceptable.
22. **Failure rate:** no source-published rug in the candidate cohort; this does not rescue its weak
    precision or recall.
23. **Signal frequency:** 99.95 core signals per 10,000 launches; 564 Premium and 564 Strong in the
    comparison contract.
24. **Confidence interval:** candidate 95% Wilson interval 14.19–18.49%.
25. **Walk-forward:** three disjoint 48-hour analysis windows, now retired after influencing design.
26. **Sealed holdout:** unavailable; no later three-window, seven-day corpus exists locally.
27. **Ablation:** buyer/wallet trajectory did not show stable incremental value over simple
    momentum; market-cap-only reached 34.22% and decisively beat the complex candidate.
28. **Drift:** severe calendar/regime instability was observed between June and July; no universal
    edge claim is permitted.

## Code, database and product audit

29. **Audit findings rectified:** false unconditional database `OK`, stale qualified-signal count,
    non-monotonic same-tick retries, dropped V1.5 failure reasons, unpersisted V1.5 tier truth,
    latency query-per-metric, missing destination-wallet/latest-decision/tier indexes and inaccurate
    operator model state.
30. **Duplicate/dead code:** compatibility state remains isolated; no migration compatibility was
    deleted. Vulture reported no production dead code beyond intentional HTTP handler overrides.
31. **Database:** migration 009 adds four targeted indexes and explicit signal tier storage; every
    connection enforces foreign keys, WAL, 30s busy timeout and NORMAL synchronous mode.
32. **Latency:** 7GB warehouse PIT lookup p95 0.0274ms; approved empty-context lookup 0.0348ms;
    coverage query 0.0650ms; Discord payload render 0.0778ms (100 local samples each).
33. **Provider resilience:** bounded timeout/retry/backoff/circuit behaviour remains; provider
    failures retain UNKNOWN and per-provider health state.
34. **Frontend:** operator status now separates active/control/candidate state, historical coverage,
    approved features and p95 latency. Normal signal cards remain signals-first.
35. **Discord commands:** all 24 registered command callbacks pass the single-response suite.
36. **Components:** menu/Home/Back/Refresh, scan Refresh/Watch, Copy CA, Watch and URL payloads pass,
    including restart persistence and exact CA recovery.
37. **Signal types:** deterministic V1.5 tier tests cover Premium, Strong, high-risk momentum,
    catalyst revival, silent watch and reject; Genesis/Radar remains non-public state.
38. **Runner E2E:** immutable signal reference, deduplicated 2x/5x/10x milestones and ATH tracking
    pass existing lifecycle/replay tests.
39. **Failure E2E:** independent FailureScore caps Premium and failure tracking remains separate.
40. **Missed-runner E2E:** reason and later right-tail attribution remain immutable.
41. **Restart E2E:** nine migrations are repeatable; command/components, outbox, milestones and
    candidate state survive reopen.
42. **Chaos E2E:** timeout, 429, corrupt payload, Discord payload rejection, interaction expiry and
    per-guild failure isolation pass the local failure-injection suites.
43. **Load/soak:** 10,000 sustained plus 3x burst; 10,256 persisted, 10,000 duplicate replays
    suppressed, 512 bounded drops, 1,611.76 events/s, 213,206 peak traced bytes.
44. **Soak state:** quick_check `ok`, WAL, zero duplicate event keys after restart.
45. **State reconciliation:** difference 0; no orphan candidates/signals, ghost signals or duplicate
    candidates in the soak acceptance database.

## Validation and release gates

46. **Pytest:** 177 passed, 5 subtests passed, one upstream Python audio deprecation warning.
47. **Ruff:** PASS (`ruff check .`: all checks passed).
48. **Package build:** PASS (sdist and wheel for `solana_memecoin_intelligence-1.5.0`).
49. **Docker:** the local Windows host has no Docker CLI; the unchanged Docker build gate is therefore required in the push-triggered GitHub workflow.
50. **GitHub Actions:** push-triggered validation is the release proof; production remains manual-only and is not dispatched by this work.
51. **Staging:** the isolated Compose contract, health check, and acceptance remain mandatory push-triggered workflow gates.
52. **Final commit:** this report and the implementation are committed together; the immutable SHA and workflow run are reported after the push.
53. **External limitations:** production Jr read-only DB copy, unbiased BNB history, seven-day
    outcomes, later sealed periods and historical social archive are absent.
54. **Before challenger:** >=250 core signals across >=3 untouched seven-day windows, point estimate
    >=80%, acceptable interval/recall/failure/frequency, two regimes, and approved features only.
55. **Before production:** prospective shadow pass, live Discord acceptance, green CI/staging,
    operator review and manual production approval.

## Completion truth

| Gate | State |
|---|---:|
| Code complete | PASS locally |
| Data complete | FAIL |
| System cohesion | PASS locally |
| Brutal E2E | PASS locally |
| Model >=80% primary target | **FAIL** |
| Research complete | FAIL |
| Challenger ready | FAIL |
| Prospective validation | FAIL / not started |
| Production ready | FAIL |

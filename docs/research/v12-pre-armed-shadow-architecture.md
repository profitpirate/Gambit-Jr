# V12 Pre-Armed shadow intelligence and causal confirmation contract

## Scope

V12 Pre-Armed remains the frozen benchmark. The shadow stack can observe, replay, score and persist research evidence, but it cannot alter the frozen selector, creator/handle intelligence, 5 ms modeled entry latency, entry-output guard, CREATE-to-fill chase cap, sizing, concurrency limit, cost assumptions or exit policy.

Real-money execution remains disabled.

## Frozen V12 decision path

The confirmation model keeps the existing requirements:

- repeat creator evidence is required;
- known creator-to-social-handle relationship is required;
- the social status must exist zero to ten seconds before CREATE;
- creator seed must satisfy the frozen two SOL floor;
- Mayhem launches are rejected;
- modeled entry latency is five milliseconds;
- entry output ratio must remain at or above 0.65;
- CREATE-to-fill price multiple must remain at or below 1.50;
- position sizing remains ten percent of available cash;
- maximum concurrent positions remains two;
- partial, stop, trail and maximum-hold exits remain frozen;
- modeled Axiom, Pump, priority, bribe and Solana base fees remain charged.

## Causal paper replay invariants

The paper simulator now fails closed when:

- the stored model fingerprint differs from the frozen model;
- a trade identity is duplicated;
- a rejection identity is duplicated;
- decision, fill and exit timestamps are not causal;
- an entry cost, proceeds value or PnL is non-numeric or non-finite.

A MAXIMUM_HOLD trade is timestamped at its actual modeled deadline. If the curve has no post-fill reserve update before that deadline, the latest causal fill reserve state is held forward; the simulator is forbidden from selling at a pre-fill timestamp.

This invariant is covered by unit tests and by the historical fifty-trade recertification replay.

## Creator concentration analytics

Every cumulative confirmation ledger is evaluated for:

- unique creators;
- trade-share HHI;
- effective creator count;
- top creator share;
- top three creator share;
- per-creator trades, wins, WR, PnL, expectancy and PF;
- full-sample metrics;
- metrics excluding the most-used creator;
- metrics excluding the three most-used creators.

These measurements determine whether V12 has a broad repeat-creator edge or a result dominated by a small number of wallets.

## Drift monitoring

The recertified fifty-trade baseline is the immutable comparison distribution.

The live confirmation is compared against it with:

- overall win rate, profit factor and expectancy;
- last-ten and last-twenty trade metrics;
- creator-mix Jensen-Shannon divergence;
- creator concentration.

The drift state is OBSERVING, GREEN, YELLOW or RED. Drift is diagnostic during the frozen confirmation test and cannot alter V12 decisions.

## Rejected-entry counterfactuals

Each entry-output-guard rejection is replayed without changing the original selection decision.

The sidecar reconstructs the chronological bankroll available at that decision and removes only the two execution guards for the counterfactual replay. It records whether the trade could have filled, hypothetical PnL, win/loss and exit reason.

This measures whether the guard is protecting expectancy or discarding too much upside without modifying the guard during the experiment.

## Execution stress matrix

Accepted trades are replayed using their actual entry budget across:

- 5, 10, 25, 50 and 100 ms entry latency;
- 1.0x, 1.5x and 2.0x modeled execution cost.

The same selector, guard and exit policy remain in force.

For every scenario the sidecar persists attempted trades, fills, guard rejections, fill rate, WR, net PnL and PF.

## Risk-of-ruin simulation

Observed net trade returns feed a deterministic bootstrap simulation.

Default simulation:

- 10,000 paths;
- 200 future trades;
- fresh three SOL bankroll;
- frozen ten percent bankroll sizing;
- deterministic seed derived from the model fingerprint.

Outputs include ending-bankroll percentiles, drawdown percentiles, losing-streak percentiles and probabilities of a twenty-percent drawdown, losing half the bankroll or falling to one SOL.

## Market-regime telemetry

Each immutable raw capture window is labelled from causal launch-flow observations.

Persisted fields include launches per minute, rolling launch-rate median, relative launch pressure, known-creator rate, selection rate and selector rejection counts.

After bootstrap, windows can be labelled QUIET, NORMAL or HOT. These labels are research metadata only.

## New Creator Analogue lane

Unknown creators remain rejected by V12 Pre-Armed.

The shadow lane evaluates whether an unseen creator resembles historically profitable V12 conditions.

Hard prerequisites include non-Mayhem launch, sufficient creator seed, usable metadata, valid social identity and a social status zero to ten seconds before CREATE.

To control metadata load, at most 250 seed-qualified unknown creators are researched per window in deterministic order.

The current score combines creator-seed strength, social recency, social-handle reputation learned only from the locked baseline, similarity to profitable creator-seed distribution and metadata quality.

The output tiers are SHADOW_ELIGIBLE, WATCH and REJECT.

The score is frozen before future reserve outcomes are evaluated. This lane has no bankroll or routing authority.

## Hard-Negative shadow veto

The hard-negative layer scores accepted V12 decisions using only causal or execution information available at the decision/fill boundary.

Inputs include output deterioration, CREATE-to-fill chase pressure, catalyst lateness, creator-seed weakness and launch-pressure regime.

A shadow veto is later classified as saved loss, killed winner, passed winner or passed loser.

Promotion requires positive incremental expectancy after accounting for winners wrongly vetoed.

## Sell-Absorption shadow intelligence

Accepted trades are observed at 50, 100, 250 and 500 ms after fill.

The sidecar computes buy/sell SOL, unique buyer/seller breadth, price resilience and trough recovery.

It emits HOLD_SUPPORT, NEUTRAL or EXIT_PRESSURE in shadow only. It cannot alter the frozen V12 exit.

## Failed-Aware shadow evidence

Raw capture rows are scanned for explicit failed-attempt semantics, success=false, error fields and rows compatible with the existing failed-intent normalizer.

For each V12 trade the sidecar records evidence available before decision and before fill.

No failed-intent evidence means the capture did not prove failed demand; it is not treated as proof that failed demand was zero.

## Operational guardrails

The guardrail policy emits CLEAR, WATCH or HALT for future execution integration.

Current hard-halt conditions include evidence-integrity failure, frozen-model mutation uncertainty, excessive closed-equity drawdown, red model drift after sufficient observations, unacceptable Monte Carlo risk, baseline execution-stress collapse and critical runtime dependency advisories.

Warnings include elevated drawdown risk, high dependency advisories, creator concentration and slower-latency execution degradation.

The guardrail output always records real_money_authorised=false during research.

## Dependency hardening

The E4 builder now uses exact direct dependency versions and a committed lockfile from the fully certified audit branch.

Forward workflows use npm ci instead of floating npm install.

The dependency-hardening workflow installs the locked graph, captures npm audit output, attempts only semver-safe lockfile remediation, reinstalls, syntax-checks every builder module, runs the builder self-test and rejects any increase in high or critical advisories.

The current graph still contains eight high and eight moderate transitive advisories. No safe non-breaking audit fix reduced them, so they remain explicitly unresolved rather than being hidden behind a forced breaking upgrade.

## Evidence persistence

Each causal confirmation window persists:

- cumulative V12 paper state;
- human-readable paper report;
- shadow analytics state;
- shadow report;
- raw launch batch;
- raw live event stream;
- metadata cache;
- progress evidence.

The sidecar stores input SHA-256 values for the model, paper state, baseline, raw batch and raw event stream.

Repeated run IDs are idempotent.

## Promotion rule

No shadow module is promoted because it looks good on the current sample.

A future V13 component must have causal timestamped decisions, independent forward observations, measured incremental value versus frozen V12, positive expectancy after costs, acceptable drawdown/concentration, acceptable execution stress and a separately frozen specification before its own untouched holdout.

V12 Pre-Armed remains the benchmark even after a future candidate is created.

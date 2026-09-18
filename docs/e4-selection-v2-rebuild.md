# E4 Unified Selection v2.2 — production rebuild

## Purpose

This rebuild replaces the weak Golden developer-library selector and the overly
simple production fallback with one authoritative E4 selection stack.

The governing rule is:

> A library record is evidence, never permission.

No developer win rate, buyer reputation row, social tag or discovered creator can
independently create a trade. A launch must first match an evidence-backed E4
entry family using causal information visible at decision time.

## Authoritative runtime

The real CLI remains `gambit-e4` / `memecoin_bot.e4_exec`.

Boot order is deliberate:

1. load the existing V9 hardening chain;
2. load final execution/restart/reconciliation patches;
3. install `UnifiedE4Policy` last;
4. wrap final buy execution so failed entries discard pending learning evidence;
5. wrap final sell execution so only genuinely closed positions update causal
   creator/buyer memory.

This removes a previously hidden boot-order risk where later monkey patches could
silently overwrite a newly installed selector or its learning hook.

The rebuild does not replace the signer, transaction builder, route racing,
one-entry-per-mint invariant, two-position concurrency ceiling, restart
reconciliation or vault sweep logic.

## 1. Hard pre-entry safety

Every candidate must satisfy all of the following before scoring:

- live untouched Pump curve;
- CREATE observed;
- no Gambit wallet interaction with that mint;
- no migration/completion;
- decision age <= 350 ms by default;
- FDV <= 8,500 USD by default and never above the engine-wide cap;
- no SELL visible before confirmation;
- creator seed >= 0.025 SOL;
- Mayhem rejected;
- robust-negative creator history vetoed.

These defaults come from the existing E4 V6/V9 hardening evidence rather than
from the failed Golden developer library.

## 2. Core E4 entry families

A launch must match at least one core family. This is what gives entry
permission.

### Public capital burst

Causal on-chain confirmation:

- age <= 300 ms;
- >= 3 non-creator buyers;
- >= 8 SOL total buys;
- >= 5 SOL non-creator buys;
- price multiple >= 1.15x.

The score retains the evidence-backed V6 weighting of FDV fit, creator seed,
buyer breadth, public capital and acceleration.

### Coordinated capital burst

Higher-intensity early structure:

- age <= 120 ms;
- >= 5 unique buyers;
- >= 10 SOL buys;
- >= 3 bundled/same-signature buys;
- price multiple >= 1.25x.

### Robust repeat E4 creator

This replaces the unsafe idea that 1/1, 2/2 or 3/3 history is enough.

The creator must first pass confidence calibration:

- >= 5 resolved E4 outcomes;
- Wilson lower confidence bound >= 0.55;
- Bayesian Beta(2,2) shrinkage;
- sample confidence weighting.

Even then, history alone cannot buy. The launch must also have:

- age <= 110 ms;
- >= 1 non-creator buyer;
- and >= 0.10 SOL non-creator confirmation or >= 1.05x price acceleration.

### Pre-announced social/community launch

Requires causal pre-launch provenance, not a Twitter link discovered after
launch:

- pre-launch social flag present;
- social/community authority >= 0.70;
- age <= 120 ms;
- >= 1 non-creator buyer.

### Trusted funder with confirmation

Funder identity remains corroborating evidence:

- funder score >= 0.80;
- public confirmation score >= 0.30;
- age <= 160 ms.

### Explicit authorized pre-armed launch

The narrow pre-armed formula remains a distinct fast family:

- explicit cached pre-armed/authorized intent;
- age <= 80 ms.

It is no longer mislabeled as “actual E4”; it is one high-confidence E4 family.

## 3. Creator library rebuild

The failed `gambit-library-live-runtime.json` developer-positive rule is not a
production permission source.

That experiment had 373 developers / 888 outcomes, but roughly 80% of developers
had at most two observations. A 3-trade record could become “positive,” and the
standalone selector subsequently finished around 16–17% WR over 100 trades.

Production creator memory now uses:

- `models/e4/e4-creator-expectancy.json` as the primary two-sided source;
- `models/e4/e4-winning-creators.json` only as weak supporting evidence;
- `models/e4/e4-discovered-creators.json` as weak external/watchlist context;
- causal forward outcomes learned by Gambit after its own positions resolve.

The current expectancy artifact is itself sparse: 184 creators / 316 E4 outcomes
and only five creators clear the robust-positive confidence bar. That is why
creator influence is intentionally bounded.

### Confidence rules

- Beta(2,2) posterior shrinkage pulls small samples toward neutral;
- Wilson intervals determine robust-positive/negative status;
- histories under five trades have only a tiny bounded score effect;
- robust-positive requires >=5 outcomes and Wilson lower >=0.55;
- robust-negative requires >=8 outcomes and Wilson upper <=0.50;
- robust-negative history can veto;
- robust-positive history can create the repeat-creator *family only with live
  confirmation*.

## 4. Causal buyer reputation

For every accepted decision Gambit records exactly what was visible then:

- mint;
- creator;
- public buyers;
- selection score;
- decision timestamp.

The record is pending until execution succeeds and the position later closes.

Rules:

- failed buys discard pending evidence;
- fewer than three resolved outcomes for a buyer have no influence;
- multiple statistically negative buyers can veto a normal candidate;
- duplicate mint outcomes cannot inflate history;
- state is persisted restart-safely.

This makes “the model learns over more trades” literal rather than rhetorical:
future decisions can change only from already-resolved past evidence.

## 5. Score fusion and conviction sizing

After a core E4 family exists:

- core family score is primary;
- creator posterior applies a bounded delta;
- buyer reputation applies a bounded delta;
- an optional calibrated logistic model, when present, contributes only a
  bounded +/-0.04 delta;
- recent bad live performance raises the acceptance threshold.

Sizing uses the retained E4 evidence tiers:

- probe: 0.75%;
- standard: 1.25%;
- strong: 1.85%;
- high: 3%;
- elite: 5%;
- exceptional: 10%.

A family can impose a minimum tier, but the final size remains capped at 10% and
can only be reduced by regime defense.

## 6. Regime defense

Recent resolved outcomes can make Gambit more conservative; they cannot
automatically make it more aggressive.

- cautious regime: threshold +0.04, size x0.65;
- defensive regime: threshold +0.08, size x0.40;
- five consecutive losses also tighten risk.

This prevents a short winning streak from causing automated over-sizing.

## 7. Exit hardening

The existing E4 runner/partial/flow logic remains the base exit policy.

Unified V2 adds:

- catastrophic -28% ceiling;
- <=2 second adverse-flow exit;
- post-partial flow-break protection;
- post-partial profit-floor protection.

Learning is triggered only after the final execution layer confirms the position
has transitioned to CLOSED.

## 8. Restart and failure correctness

The selection memory is persisted at:

`data/e4-selection-memory-v2.json`

Pending decision evidence survives a process restart. If the corresponding buy
does not create an open position, the final buy wrapper removes it. A failed buy
therefore cannot later be “resolved” as if it were a real trade.

Resolved mint IDs are deduplicated.

## 9. Operational controls

Kill switch:

`E4_SELECTION_V2_ENABLED=false`

Calibration controls:

- `E4_SELECTION_V2_THRESHOLD=0.70`
- `E4_SELECTION_V2_MAX_ENTRY_FDV_USD=8500`
- `E4_SELECTION_V2_MAX_ENTRY_AGE_MS=350`
- `E4_SELECTION_V2_MIN_CREATOR_SEED_SOL=0.025`
- `E4_SELECTION_V2_MAX_POSITION_FRACTION=0.10`

The core family geometry is intentionally not exposed as casual environment
tuning.

## 10. Certification / bulletproof gate

The branch is not accepted unless all gates pass:

- production modules compile;
- Ruff static audit passes;
- dedicated selection unit/integration tests pass;
- authoritative `gambit-e4` subprocess boots with Unified V2 as the final
  policy;
- final buy path contains failed-entry memory cleanup;
- final sell path contains causal close learning;
- every real repository test module passes in a fresh interpreter;
- 50,000 deterministic fuzz/stress decisions complete;
- stress cannot accept terminal, stale, over-FDV, no-seed or pre-sell states;
- stress cannot exceed the 10% size cap;
- stress cannot collapse to reject-all or accept-everything;
- duplicate outcomes cannot inflate memory;
- failed-entry memory can be discarded;
- p95 selection latency <= 2.5 ms;
- p99 selection latency <= 5 ms;
- strict architecture audit passes every invariant.

Certification artifacts contain the isolated regression report, stress report and
strict audit report.

## 11. What this does not claim

Passing certification proves the implementation is causal, deterministic,
bounded, restart-safe and internally consistent. It does not prove future
profitability.

More trades can improve buyer/creator memory and give us stronger estimates.
They do not mathematically force a losing strategy to become profitable.

Forward paper evidence remains required before any real-money deployment.

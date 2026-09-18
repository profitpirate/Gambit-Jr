# E4 Unified Selection v2 — production rebuild

## Objective

Replace the thin production entry gate with one end-to-end selector that uses actual E4 live flow as the core permission signal, historical creator evidence as calibrated context, causal buyer reputation as online memory, conservative conviction sizing, and adaptive exit protection.

The previous Golden developer-library experiment proved that a developer's historical win rate must not be an entry trigger. The production rule is therefore explicit:

> Library/creator history may boost, penalise or veto. It may never independently authorise a buy.

## Production path

The production entrypoint remains `memecoin_bot.e4_production`. At import time it installs `UnifiedE4Policy` into the existing E4 engine without changing signer, transaction builder, route racing, one-entry-per-mint, two-position concurrency, restart reconciliation or sweep behavior.

### Selection pipeline

1. **Hard safety**
   - untouched Pump curve only;
   - reject migrated/completed curves;
   - reject anything already touched by the Gambit wallet;
   - enforce observed E4 FDV ceiling.

2. **Actual E4 signal core**
   - optional selected-vs-ignored logistic model when a calibrated model is present;
   - otherwise the production fallback uses point-in-time FDV fit, first-second buy SOL, unique buyers and buy/sell dominance;
   - 250 ms acceleration and 1 s flow are evaluated separately.

3. **Creator memory**
   - primary evidence comes from `e4-creator-expectancy.json`, which contains both wins and losses;
   - Beta(2,2) shrinkage pulls small samples toward neutral;
   - Wilson intervals stop tiny samples from being treated as certainty;
   - robust positive creator status requires at least five resolved attempts and a Wilson lower bound of at least 0.55;
   - robust negative creator status requires at least eight resolved attempts and a Wilson upper bound at or below 0.50;
   - one-off winning-creator registry rows and externally discovered creators are weak context only.

4. **Buyer reputation**
   - buyers are captured at decision time;
   - reputation is updated only after that Gambit trade resolves;
   - fewer than three outcomes is ignored;
   - multiple strongly negative buyers can veto an otherwise ordinary candidate;
   - the state is restart-safe and duplicate mint outcomes cannot inflate history.

5. **Fusion**
   - actual E4 model/profile: 48%;
   - live flow: 30%;
   - FDV fit: 12%;
   - buyer posterior: 10%;
   - creator and buyer evidence apply bounded deltas;
   - the final score must clear a dynamic threshold.

6. **Live confirmation**
   - normal entries require live buyer count, buy SOL and buy/sell ratio;
   - a genuinely robust repeat creator can use a lower live-flow confirmation floor, but never zero live confirmation;
   - this prevents the old developer-library failure mode.

7. **Conviction sizing**
   - uses the evidence-backed `e4-selection-v2.json` tiers;
   - hard maximum is 10% of available capital for this selector, even if the broader engine maximum is higher;
   - poor recent live outcomes can only reduce size.

8. **Adaptive risk/exit**
   - preserves the existing E4 fast-failure, partial and runner logic;
   - adds a catastrophic loss ceiling;
   - adds early adverse-flow exit;
   - adds post-partial flow-break and profit-floor protection.

## Causal learning

This system genuinely learns, but only after outcomes resolve.

For every accepted entry the engine persists:
- creator;
- public buyers visible at decision time;
- score;
- decision timestamp.

When the position closes it stores the realised return and updates:
- creator runtime evidence;
- buyer evidence;
- recent regime history.

The same mint can never be counted twice.

### Regime defense

The system never becomes more aggressive merely because it has won recently. Bad resolved performance can make it more conservative:

- caution: threshold +0.04, size ×0.65;
- defense: threshold +0.08, size ×0.40;
- five consecutive losses also tighten the system.

This is deliberately asymmetric to avoid automated revenge sizing or overfitting to a short winning streak.

## Library rebuild

The old Golden runtime library is not used as a production permission source.

Production creator context is rebuilt from:
- `models/e4/e4-creator-expectancy.json` — two-sided E4 outcomes;
- `models/e4/e4-winning-creators.json` — weak positive context only;
- `models/e4/e4-discovered-creators.json` — externally supported context/watchlist;
- causal forward outcomes accumulated by the live engine.

No 3/3 or 2/3 developer can independently generate a trade.

## Certification standard

The branch is not considered acceptable unless all of these pass:

- Python compile of production selector/entrypoint/audit/stress paths;
- Ruff on every changed production and test file;
- dedicated selection unit/integration suite;
- full repository regression suite;
- 50,000 synthetic selection decisions;
- deterministic decision check;
- no invalid-curve acceptance;
- no size-cap violation;
- no duplicate-memory inflation;
- p95 selector latency <= 2.5 ms;
- p99 selector latency <= 5 ms;
- strict architecture audit;
- stress report must pass.

Certification evidence is uploaded as a GitHub Actions artifact.

## Operational controls

Kill switch:

`E4_SELECTION_V2_ENABLED=false`

This restores the previous E4 policy without changing transaction execution.

Causal memory:

`E4_SELECTION_MEMORY_PATH=data/e4-selection-memory-v2.json`

The file is runtime state and must not be treated as frozen research evidence.

## Non-goals

This rebuild does not claim that a profitable historical model is guaranteed to remain profitable. More trades improve the evidence and the online reputation state; they do not mathematically force PnL to turn positive.

The acceptance standard is therefore system correctness, causality, calibrated confidence, bounded risk and repeatable stress behavior. Profitability still requires forward evidence.

# V12 E4 + HG Trade Library

This directory is the canonical developer-centric research library for V12.

## What it contains

- Every resolved E4 source trade from the frozen creator corpus: 242 wins + 74 losses = 316 unique mints.
- E4 creator/developer association for every source trade.
- Explicit entry/exit/PnL enrichment where V12 evidence contains it.
- HG/Gambit Jr terminal closed paper trades from completed workflow checkpoints.
- Explicit HG rejections, kept as rejections rather than relabelled as losses.
- Post-exit `went_higher` / `went_lower` only where observed evidence exists.
- Unknown/incomplete post-exit information remains `UNKNOWN`.

Generated files:

- `e4-hg-trade-library.json` — record-level source of truth and category indexes.
- `e4-hg-developer-index.json` — combined history beside each developer.

## Categories

`wins` and `losses` refer to resolved trade PnL/outcome. `wins_after_exit` and `losses_after_exit` are separate, non-exclusive path flags: a coin can trade both above and below the exit after the position closes. Missing post-exit coverage is placed in `post_exit_unknown` rather than guessed.

`golden_bunch` uses the implementation rule **at least 2 resolved E4 wins and 0 E4 losses**. This was selected because no older numeric threshold was frozen; it also matches the existing creator-expectancy corpus' `repeat_pure_winner_creators` statistic. The rule is written into every snapshot so it cannot silently drift.

## HG update discipline

The workflow imports HG data only from GitHub Actions runs whose workflow status is terminal (`completed`). Within those checkpoints it imports only already-closed paper positions and explicit rejections. It never imports an open position as a result.

This means an interrupted/cancelled run can still contribute its already-closed factual rows while unresolved inventory is excluded.

## `FULL_3S_V2_LIBRARY`

`src/memecoin_bot/full3s_v2_library.py` is an isolated research competitor. It is **not** wired into the current Top4 or 2-second campaigns.

The index refuses retroactive use of the frozen E4 outcome corpus for decisions that occurred before the library snapshot. For future forward decisions, it exposes a causal developer profile. The initial policy preserves baseline admission except for a developer with at least two observed losses and no wins, which receives a repeat-loser veto. This policy has no performance claim until separately forward-tested.

There is no signer, `sendTransaction`, funded execution, or live-order path in the library builder or policy.

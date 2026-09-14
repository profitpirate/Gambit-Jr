# E4 V12 supervised forward-paper operations repair

## Verified defect, not a new performance result

The v1 online runner loaded `e4_live_market_stress.py` but never ran that module's `main_async` price initialisation. Consequently `hardening._SOL_USD` remained the workflow's fallback 150. Both `create_fdv_usd` and `fdv_0ms` could be supplied incorrectly to the frozen model.

A local diagnostic using the previously captured 3,000-launch artifact (run 34757637688), the original 66,000-row corpus and unchanged prototype settings found:

- B candidate mint `CwG7L5q9ENnBLqnRVY12hQA2HKQ7yf2hNHEfimcFpump` scored 0.9839045905735531 with the recorded market-cap inputs, above threshold 0.9838282174643644.
- Replacing only the USD market-cap inputs with values derived from the erroneous 150 SOL/USD default reduced that score to 0.8824422169987686.
- Correct recorded inputs produced A=0, B=1 signals in that diagnostic dataset; erroneous 150-derived inputs produced A=0, B=0.

This demonstrates a real input bug. It does NOT prove that every abstention was caused by that bug, that either thesis is profitable, or that forward performance will reproduce historical percentages.

## Implementation

`tools/e4_v12_live_ops.py` restores only the TWO frozen policies. There is no strategy search, threshold lowering, new historical profitability gate, batch-forward rescoring or transaction submission. Live SOL/USD must be fetched and refreshed; no unverified 150 fallback is accepted. Non-SOL quote markets are excluded explicitly rather than mislabelled as SOL. Features and all decisions, including rejections, are written to append-only JSONL evidence.

Scoring uses the same Euclidean neighbour mathematics with one worker and pre-sorted reference distributions; it runs off the market/exit event loop. Receipt timestamps are recorded before queueing. Creator-selection priors update only after received E4 buys. No unknown post-start net-PnL outcome priors are invented.

Each account starts at 2 SOL, uses the frozen 1.85% position fraction, locks cash at entry and settles it at exit. The delay floor is 10ms plus observed processing delay, NOT proven mainnet landing latency. Native reserve quotes use the archived fee assumptions (125bps and 0.00015 SOL priority/tip per side). Missing/zero liquidity, stale decisions and output shortfall are rejected. Quiet-market stale reserve snapshots trigger a fresh RPC account read with owner/layout/quote checks and minContextSlot. Migration invalidates the old bonding-curve quote. Unresolved exits remain visible as unpriced exposure, never fabricated closed trades.

## Independent Operations Supervisor

`tools/e4_v12_ops_watchdog.py` runs outside the trading process. Its polling interval is 5 seconds and it publishes compact status to `docs/research/e4-v12-live-ops-status.json` approximately once a minute. It checks runner heartbeat, live feed age, SOL/USD freshness, scoring backlog, invalid features, delayed decisions and execution errors. WebSocket workers reconnect automatically with bounded backoff; the supervisor terminates unrecovered technical failures, rather than restarting indefinitely or resetting accounts.

Inactivity warns after 15 minutes AND 250 scored launches. The resource guard stops after 60 minutes without another signal AND 1,000 additional scored launches. The absolute forward session is capped at 5.5 hours. The target is at least 20 closed trades per thesis; a time/resource stop before that is INCOMPLETE, not PASS. Process success is never a strategy-performance certificate.

Unit/async/process tests cover the price defect, invalid input, cash locks, account isolation, duplicate entries, stale/migrated quotes, task exceptions, independent watchdog termination and false-green prevention. A local frozen-model SOFTWARE contract check also passed on all 66,000 source rows without changing the thesis parameters. It is not a fresh historical performance qualification.

## Scope and limitations

This supervises these forward-paper runs, not an uninspected production VPS. Trading is simulated from current public market data; no wallet keys, signatures or funds are used. Fees and execution remain paper-model assumptions. Realised drawdown uses cash plus locked principal, not continuous mark-to-market. Historical reference ranking and model choices remain inherited; this repair does not establish that the old statistical selection was unbiased. The supervisor cannot guarantee qualifying signals, a minimum win rate or profit. It makes abstention and operational failure observable and stops blind resource consumption.

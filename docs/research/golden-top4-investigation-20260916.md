# Golden top4 live investigation — 16 Sep 2026

## Verified findings

1. The first four-hour campaign was healthy and ended cleanly with 5,318 fresh launches and seven qualified cohorts. Controls had seven verified forward closes each.
2. FULL_3S_V2 closed the same seven paper positions but all seven were excluded from forward evidence and its cost-aligned evidence index remained empty.
3. Root cause: the frozen V2 integration used a 500 ms wall-clock quote-age rule continuously while a position was open. On an event-driven reserve feed, a quiet coin produces no new reserve event because its reserves did not change. The wrapper therefore marked normal quiet periods as uncertain even while the global Solana event stream remained healthy.
4. This also delayed some V2 exits. Six of seven closed around 3.25–4.23 seconds after entry; one was delayed to about 20.93 seconds. That made V2 not cleanly comparable with FULL_3S.
5. The legacy Golden reputation gate is intentionally selective: >=10 aggregate prior buyer appearances and >=70% legacy historical win rate. In this live window only 7 / 5,318 launches qualified (~0.132%). This is not an execution bug.
6. The seven FULL_3S outcomes were 1 win / 6 losses but remained +0.1454 SOL net because the single winner was +0.3156 SOL and the average loser was -0.02837 SOL. This is highly concentrated and too small to establish a new win-rate regime.
7. The seven legacy reputation labels were based on the old 2-second small-benchmark economics, so a >=70% legacy reputation score is not expected to equal >=70% win rate under the present larger cost-aware FULL_3S account. V2's cost-aligned evidence layer was designed to address that mismatch, but the uncertainty bug prevented it from warming up.

## Fix

The frozen V2 source is preserved unchanged. The top4 adapter now uses global feed health and causal slot/event ordering for reserve-state liveness. A coin-specific reserve snapshot remains the latest observed executable state until a newer event changes it. Global feed silence, clock regression, invalid/out-of-order state, unresolved restart inventory and incomplete post-exit observation remain guarded. Entry quote freshness remains unchanged.

## Research integrity

The seven affected V2 trades from the first window are not retroactively promoted to verified evidence and are not used to seed the corrected cost-aligned index. The original control results remain preserved. Because the V2 execution semantics changed, the corrected four-model comparison must start as a new campaign with fresh 3 SOL paper accounts; old balances/results are retained only as prior evidence, not imported.

No Golden threshold, sizing tier, hold horizon, cost assumption or E4 independence rule is loosened by this bug fix. The next run should additionally report legacy-reputation calibration versus realised net FULL_3S outcomes as diagnostics; any future selection change must be tested prospectively rather than fitted to these seven trades.

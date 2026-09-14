# E4 high-resolution recent trade path analysis

Scope: five E4 source positions that overlap the run #66 live Pump event capture (`34809168521`). The capture contains high-resolution local `received_ns`, slot/event ordering, decoded Pump TradeEvents and post-event bonding-curve reserves. This is benchmark research only.

## Method

For each overlapping E4 position:

- locate E4's actual BUY and SELL TradeEvents in the live archive;
- use the decoded post-event virtual SOL/token reserves to quote the original full token position as though it were liquidated at each observed market event;
- include a 1.25% Pump sell fee and 0.000365 SOL estimated sell transaction cost in the mark;
- compare quoted liquidation value against the reconstructed total E4 wallet cost (which includes transaction/priority/tip overhead captured by wallet SOL delta);
- preserve E4's exact token partial fractions and sub-second `received_ns` timing;
- inspect the event path through E4's final exit and up to 20 seconds afterwards where events are available.

These are event-level executable-quote approximations, not continuous tick extrema. E4's own sells alter the actual subsequent curve, so post-partial full-position counterfactuals describe the market that actually occurred rather than a no-E4-impact alternative universe.

## Five recent paths

| Mint prefix | Realized return | CREATE→E4 buy | First partial | First partial timing | Mark immediately before initial | Full-position MFE during E4 lifecycle | Full-position MAE during E4 lifecycle | E4 final exit time |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Hztwz8 | +10.43% | 0.724 ms | 30% | +0.283 s | +13.87% | +24.07% | -4.50% | +3.262 s |
| 9QU6Sn | -16.02% | 0.423 ms | 30% | +0.495 s | -2.55% | +7.19% | -26.90% | +4.827 s |
| J9h5RA | -1.16% | 1.550 ms | 30% | +0.267 s | -1.45% | -1.45% | -8.56% | +2.072 s |
| B2hjwf | +26.86% | 6.180 ms | 20% | +0.299 s | +6.87% | +31.18% | -17.05% | +9.616 s |
| BCKYwS | -14.80% | 0.247 ms | 20% | +0.504 s | -15.08% | -15.08% | -34.83% | +3.975 s |

`CREATE→E4 buy` uses local high-resolution event receipt ordering, not Solana blockTime seconds.

## Exact sell paths

### Hztwz8 — realized +10.43%

- BUY about +0.724 ms after CREATE receipt.
- +0.283 s: sell 30%; immediately before the initial the quoted full-position return was about +13.87%.
- The observed full-position path later reached roughly +24.07% MFE.
- +3.262 s: sell final 70%; immediately before the final sell the full-position mark was about +6.57%.
- After E4 fully exited, the next observed 20-second window never restored the original fully loaded trade above break-even after costs (best observed mark roughly -1.79%) and later deteriorated to roughly -32.08%.

Interpretation: E4 did not capture the absolute event-level peak, but the initial banked profit and the eventual full exit materially protected the trade before a later collapse.

### 9QU6Sn — realized -16.02%

- BUY about +0.423 ms after CREATE receipt.
- +0.495 s: sell 30%; immediately before the initial, the quoted full-position return was about -2.55%.
- The path briefly reached approximately +7.19% MFE around the first-partial region, then deteriorated.
- +4.827 s: sell final 70%; immediately before final liquidation the full-position mark was approximately -23.54%.
- The actual partial-managed realized loss was -16.02%, materially better than the full-position mark around final exit.
- Following exit, the next observed events were roughly -29.6% to -34.8% versus original total cost.

Interpretation: the 30% initial reduced the later loss. E4 still allowed the runner to deteriorate too far in this example, but exiting prevented further observed damage.

### J9h5RA — realized -1.16%

- BUY about +1.550 ms after CREATE receipt.
- +0.267 s: sell 30%; full-position mark just before the initial was about -1.45%.
- No positive after-cost full-position excursion was observed after entry; best mark was approximately -1.45%, worst about -8.56%.
- +2.072 s: sell final 70%; full-position mark just before final exit was roughly -3.27%.
- Partial management left the realized result at only -1.16%.

Interpretation: this resembles a fast failed setup where the initial was not profit-taking at all; it was exposure reduction.

### B2hjwf — realized +26.86%

- BUY about +6.180 ms after CREATE receipt.
- +0.299 s: sell 20%; full-position mark immediately before initial ≈ +6.87%.
- +4.341 s: sell another 20%; prior full-position mark ≈ +18.20%.
- +5.729 s: sell 15%; prior mark ≈ +23.22%.
- +7.987 s: sell 11.25%; prior mark ≈ +9.58%.
- +9.616 s: sell final 33.75%; prior full-position mark ≈ +7.66%.
- Event-level MFE during the lifecycle was roughly +31.18%.
- Event-level MAE was roughly -17.05%, despite the trade ultimately realizing +26.86%.
- In the next 20 seconds after E4's final exit, best observed original-position mark was only about +9.31%, while worst fell to about -9.33%.

Interpretation: this is strong evidence against a simple hard -5%/-10% stop. A profitable E4 runner can experience a substantial adverse excursion and then recover. E4 progressively de-risked rather than applying a naïve fixed stop.

### BCKYwS — realized -14.80%

- BUY about +0.247 ms after CREATE receipt.
- +0.504 s: sell 20%; full-position mark immediately before initial ≈ -15.08%.
- +2.747 s: sell 26.4%; prior mark ≈ -19.36%.
- +3.975 s: sell final 53.6%; prior mark ≈ -25.02%.
- No positive after-cost excursion was observed in the captured post-entry path; best event-level full-position mark was approximately -15.08%, worst around -34.83%.
- The staged exits improved realized loss to -14.80% versus the much worse full-position mark around the final exit.

Interpretation: E4's initial occurred while already materially underwater. Again this is inconsistent with a fixed positive-profit TP; the initial behaves like structural exposure reduction.

## What the path data says about E4's first partial

The marks immediately before the first partial across these five trades were approximately:

- +13.87%
- -2.55%
- -1.45%
- +6.87%
- -15.08%

Yet the first partial was taken in every case within roughly 0.27-0.50 seconds after entry, using either 30% or 20% depending on size tier.

Therefore a fixed-profit trigger such as `take 30% at +10%` is inconsistent with this sample. The better-supported interpretation is that the initial is a rapid structural de-risk/capital-recovery action whose fraction is linked to entry-size tier. Exact private trigger/timing remains unknown.

## Stop-loss implication

The path data also rejects a simplistic conclusion that Golden should merely copy a tight fixed stop:

- B2hjwf eventually realized +26.86% but had an event-level after-cost MAE around -17.05% during its E4 lifecycle.
- Hztwz8 realized +10.43% after an early MAE around -4.50%.
- 9QU6Sn briefly had positive MFE before eventually losing -16.02%.

The better research question is whether a combination of initial de-risking plus flow/curve invalidation can distinguish recoverable volatility from true failure. Fixed percentage stops must be tested against MFE/MAE paths before adoption.

## Main conclusion

Recent high-resolution paths support the behavioral architecture inferred from wallet legs:

1. E4 enters once and extremely early.
2. It removes a fixed-ish fraction almost immediately even when the trade is not yet profitable.
3. Initial fraction changes with position-size tier (30% standard; 20% high-size in these captured examples).
4. The remaining runner is managed dynamically; sell timing is not a universal fixed hold.
5. Partial exits materially improve some losing outcomes versus holding the full original position until final exit.
6. Tight naïve percentage stops would also destroy at least some strong winners, so the missing ingredient is likely state/flow-based invalidation rather than only a fixed SL.

# E4 position-sizing / initial-exit rule confirmation v2

This supplements the 501-position wallet study with the 214 normal-size E4 selections in the frozen 66k corpus for which E4's decoded source BUY amount is also available. It is behavior reconstruction, not private strategy extraction.

## Clean source-BUY subset

214 normal-size landed E4 positions (`1 <= reconstructed wallet cost < 50 SOL`) have decoded E4 source BUY information including the Pump `sol_amount` and reconstructed source priority fee.

For each row, `observed_source_sol` is the Pump TradeEvent SOL amount. Multiplying by 1.0125 reverses the 1.25% Pump protocol fee in this research model and gives an approximate gross swap budget before priority/tip overhead.

The resulting gross budgets are strongly tiered/rounded rather than continuously random. The most frequent approximate budgets are:

- 1.3 SOL: 34 positions
- 3.0 SOL: 39
- 2.0 SOL: 21
- 4.0 SOL: 16
- 6.0 SOL: 10
- 1.5 SOL: 10
- 2.4 SOL: 8
- 1.2 SOL: 6
- 3.2 SOL: 5
- 5.0 SOL: 5
- 7.0 SOL: 5

There are additional less-frequent steps (1.4, 1.6, 1.7, 1.8, 2.2, 2.6, 2.8, 3.1, 3.4, 3.5, 6.4, 6.7, 6.9, 8, 10 SOL, etc.). This is consistent with a tiered/dynamic sizing engine rather than one constant order size.

## Clean boundary for the dominant first partial

Within this decoded-source subset, looking only at the two dominant initial-exit regimes:

### 30% initial cohort

- 174 positions
- approximate gross swap budget range: **1.19 to 4.00 SOL**
- no 30% initial in this subset had a gross swap budget above ~4 SOL.

### 20% initial cohort

- 38 positions
- approximate gross swap budget range: **4.961 to 11.907 SOL**
- no 20% initial in this subset had a gross swap budget below ~4.96 SOL.

That produces a near-clean gap around a **5 SOL gross swap budget**:

> standard-size regime (~1.2-4 SOL swap budget) -> normally sell 30% first
>
> high-size regime (~5 SOL+) -> normally sell 20% first

This is stronger evidence than using reconstructed wallet cost alone because wallet cost also contains variable priority/tip/transaction overhead.

It does **not** prove that E4 literally contains code `if size >= 5: initial = 20%`; there may be a shared conviction tier that independently determines both size and initial fraction. But behaviorally, the two are almost perfectly coupled in this decoded subset.

## Position size contains conviction information, but not certainty

The larger 501-position wallet sample shows source WR by reconstructed wallet-cost band:

- 1-2 SOL: 57.14%
- 2-3 SOL: 67.97%
- 3-5 SOL: 79.02%
- 5-10 SOL: 71.21%
- 10-50 SOL: 76.47% (small sample)

In the 214 decoded-source subset, the most common approximate swap-budget tiers also show materially different outcomes, e.g.:

- ~1.3 SOL budget: 34 trades, ~58.8% WR
- ~2.0 SOL budget: 21 trades, ~81.0% WR
- ~3.0 SOL budget: 39 trades, ~79.5% WR
- ~4.0 SOL budget: 16 trades, 75.0% WR
- ~6.0 SOL budget: 10 trades, 80.0% WR

Small per-tier counts and regime differences mean these numbers should not become hard Golden sizing rules by themselves. They do, however, confirm that E4's order size is not arbitrary noise and is suitable as a post-entry conviction label for research.

## Priority fee / wallet-cost distinction

The wallet SOL delta is not the same as the Pump swap amount. In many decoded trades:

`wallet cost ≈ gross swap budget + source priority/tip/other tx overhead`

For example, several rows have a reconstructed residual within roughly 0.002 SOL of the decoded source priority fee, while other rows contain additional unparsed overhead/tip. Therefore any inference based on a raw wallet-cost threshold alone can blur the actual size tier. The decoded gross swap budget is the cleaner quantity for studying E4's intended position size.

## Confirmed behavioral rule to date

The evidence now supports this description with high confidence:

1. E4 chooses a variable, tier-like swap budget at entry rather than one fixed order size.
2. It buys the position in one shot; the 501-position sample contains one reconstructed BUY per position.
3. Standard-size positions overwhelmingly receive a ~30% initial token reduction.
4. High-size / ~5 SOL+ swap-budget positions overwhelmingly receive a ~20% initial token reduction.
5. Remaining exposure is dynamically managed: losing positions are normally flattened sooner/fewer legs, while winners are held longer and scaled out over more legs.
6. Size/conviction increases expected quality on average but can still be badly wrong; it is not certainty.

What remains unconfirmed is the private feature score that chooses the swap-budget tier and the exact market-state trigger for later runner exits.
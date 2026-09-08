# E4 V12 joint choice-MoE selection report

Frozen validation-selected model: `restricted_mixture_of_experts`

The conditional-choice result is not an autonomous trading verdict. The separate 
NO_TRADE, execution, abstention, and economic gates remain controlling.

## Historical holdout selection

- Top-1 accuracy: 0.9829
- Top-3 recall: 1.0000
- MRR: 0.9914
- Pairwise accuracy: 0.9993

## Safeguards

- Chronological window split; no row/pair random splitting.
- All transforms, expert gates, thresholds, and calibration are training-only.
- Labels, execution outcomes, signatures, source IDs, and future outcomes are excluded.
- Failed-fill lower-bound uncertainty is preserved and excluded from exact hazard fitting.
- Raw-identity performance is diagnostic only.
- Funder expert absent because funder coverage is zero.

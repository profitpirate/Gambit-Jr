# E4 selection cohort forensics

This report explains E4 intent; it is not an approved production model.

## Developer recurrence

- E4 attempts: 498
- Unique selected creators: 233
- Creators selected more than once: 72
- Attempt share from repeat-selected creators: 67.67%

## Compact scoring frontier

| model | validation AP | precision | recall | median score time | exploratory holdout AP |
|---|---:|---:|---:|---:|---:|
| extra_trees_32 | 0.4823 | 58.75% | 46.53% | 1332.9 us | 0.3268 |
| extra_trees_64 | 0.4798 | 57.50% | 45.54% | 2131.9 us | 0.3425 |
| logistic | 0.3398 | 51.25% | 40.59% | 128.0 us | 0.3437 |
| decision_tree_depth_6 | 0.3283 | 59.04% | 48.51% | 50.5 us | 0.1703 |
| decision_tree_depth_4 | 0.2621 | 46.09% | 52.48% | 49.8 us | 0.1470 |

The final ten captures are exploratory for this revision because an earlier model already opened them.
A new future epoch is required for approval.

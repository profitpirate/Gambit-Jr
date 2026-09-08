# E4 V12 canonical causal choice-risk sets V2

Integrity gate: **PASS**.
No model was trained. Production V12 paths changed: zero.

## Corpus

- Capture runs: 22
- Captured launches: 66,000
- Decision groups: 498
- Rows: 12,927
- Alternatives: 12,429
- Mean alternatives: 24.96
- Median alternatives: 24.0
- Groups with >=3 alternatives: 99.60%

## Selection backfill

- Successful selections recovered from V1 exclusions: 57
- Failed-fill selections recovered from V1 exclusions: 52
- Expanded selected events: 498
- Clean V1 resolved-wallet coverage: 60.36%

## Feature coverage

- buyer_cluster: 72.12%
- creator_history: 99.37%
- first_buyer_history: 72.12%
- funder_history: 0.00%
- metadata: 25.36%
- reserve: 100.00%
- social: 8.20%
- topology: 100.00%
- transaction_index: 0.93%
- website: 0.48%

## Integrity

- Future leakage: 0
- Ambiguous labels: 0
- Missing selected rows: 0
- Duplicate group rows: 0
- V1 hash changes: 0

## Evidence limitations

- Creator funding transactions and candidate fee-payer account lists are absent, so funder history is unavailable rather than inferred.
- Failed-fill decision times remain conservative CREATE-receipt lower bounds because failed transactions emit no capture event.
- 283 V1 exclusions remain outside retrievable capture history or lack an authoritative mint/CREATE event.
- Groups within 60 seconds of a capture start are explicitly marked left-truncated.
- Mutable metadata without immutable pre-decision timestamp proof is excluded from causal social features.

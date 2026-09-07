# E4 V12 canonical decision choice sets

## Result

Integrity gate: **PASS**.
No model was trained and no production code or live-money path was changed.

## Clean causal sample

- Frozen capture windows: 12
- Captured launches: 36000
- Decision groups: 322
- Landed selections: 116
- Failed-fill selections: 206
- Choice-set rows: 496
- Total alternatives: 174
- Median alternatives per group: 0.0
- Candidate range per group: 1–5
- True-ignore launches: 35678
- Captured-selection coverage: 100.00%

## Selection audit

- Successful buys discovered in resolved wallet history: 265
- Successful buys included: 116
- Successful buys excluded: 149
- Failed attempts discovered in resolved wallet history: 449
- Failed attempts authoritatively mapped: 371
- Failed attempts included: 206
- Failed attempts excluded: 243
- Audited resolved-wallet selections: 100.00%
- Clean resolved-wallet modelling sample: 45.10%

### Exclusion reasons

- `SUCCESSFUL_BUY` / `OUTSIDE_AUTHORITATIVE_CAPTURE_INTERVAL`: 48
- `SUCCESSFUL_BUY` / `TARGET_LAUNCH_OUTSIDE_AUTHORITATIVE_CAPTURE`: 101
- `FAILED_ATTEMPT` / `OUTSIDE_AUTHORITATIVE_CAPTURE_INTERVAL`: 79
- `FAILED_ATTEMPT` / `TARGET_LAUNCH_OUTSIDE_AUTHORITATIVE_CAPTURE`: 164

## Causal field coverage

- Rows with decision `received_ns`: 496
- Decision groups missing transaction index: 0
- Rows missing candidate transaction index: 399
- Rows with reserve state: 496
- Rows missing reserve state: 0
- Rows with causal social evidence: 158
- Rows with causal creator history: 66
- Rows with first-buyer history: 159
- Rows with causal funder history: 0

## Integrity checks

- Future-leakage violations: 0
- Ambiguous labels: 0
- Missing chosen rows: 0
- Duplicate source events: 0
- Source parse errors: 0
- Reserve reproductions within 500 bps: 116

## Chronology policy

`received_ns` is the primary clock. Slot, transaction index, event index, and signature
are used only for exact receipt-time ties. Landed buys use their observed receipt time.
Failed fills have no event receipt in the JSONL, so their group uses the selected launch
receipt as a documented lower bound and retains the failed transaction slot/index separately.
Same-receipt alternatives without transaction-order proof are conservatively omitted.

## Explicit evidence gaps

- 164 mapped failed attempts and 101 in-interval landed buys target launches outside the frozen 36,000-launch corpus; they are explicitly excluded.
- The full wallet ledger has 23,025 unresolved transaction details; these cannot be silently asserted to be entry attempts.
- Capture JSONLs do not carry transaction indexes for most launch events; exact receipt-time ties without independent block order are conservatively omitted.
- No causal funding-wallet registry exists in the audited artifacts, so funder fields remain unavailable.
- Social and website fields are exposed only when the launch-time URI is content-addressed; mutable post-hoc metadata is not used as a causal feature.

## Reproduction

The source manifest pins every run, artifact name, split, byte size, and SHA-256.
Re-running the builder against those restored artifacts reproduces every stable
`decision_group_id` and JSONL row order. The coverage JSON contains every exclusion
with a machine-readable `reason_code`.

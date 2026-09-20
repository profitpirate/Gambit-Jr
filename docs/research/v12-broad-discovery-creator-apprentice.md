# V12 broad discovery + creator apprenticeship

This upgrade is deliberately **shadow-only**. It expands what V12 can observe without changing what the frozen V12 Pre-Armed selector is allowed to trade.

## What changes

1. A separate broad-discovery workflow captures **6,000 strictly future Pump launches by default**, configurable up to 12,000, instead of the causal proof's fixed 3,000-launch window.
2. Every unknown creator launch in that broader cohort is written to a persistent apprenticeship registry. There is no 250-creator observation cap.
3. Every unknown coin gets causal runner telemetry at 2s, 10s and 60s, even when the creator is not eligible for V12.
4. Every unknown coin gets an all-unknown shadow trade attempt using the frozen execution costs, output guard, chase guard and exit policy. A separate V12-compatible hypothetical is recorded only when non-creator V12 conditions such as mayhem rejection and creator-seed floor pass.
5. Creator history persists across windows so later launches can confirm or disprove the first result.

## Creator lifecycle

`DISCOVERED -> RUNNER_OBSERVED -> SHORTLISTED -> CONFIRMED_REPEAT_WINNER -> PROMOTION_CANDIDATE`

- First profitable V12-compatible hypothetical fill: **SHORTLISTED**.
- Second profitable launch on a different mint: **CONFIRMED_REPEAT_WINNER**.
- Promotion additionally requires at least three V12-compatible fills, at least 66.7% win rate, PF >= 1.5 and positive net P&L.
- **Promotion never edits the frozen creator list automatically.** It creates a separate evidence queue for later forward validation.

The extra promotion gate is intentional. With thousands of unknown creators, auto-promoting every 2-for-2 sequence would create multiple-testing/false-discovery risk and could destroy V12's selectivity.

## Isolation guarantees

- Frozen model fingerprint stays `69df86eaf386fd37d699928a95d25aaeb2053e743b59c9e687c8a7c49d14f977`.
- Current causal 100-trade confirmation is untouched.
- No real-money execution.
- No production execution paths changed.
- Broad discovery has a separate branch, workflow, concurrency group, evidence state and artifacts.
- Auto-continuation is disabled by default in `research/v12-broad-discovery-control.json`.

## Operational files

- `scripts/v12_creator_apprentice.py`
- `tests/test_v12_creator_apprentice.py`
- `.github/workflows/v12-broad-discovery-shadow.yml`
- `.github/workflows/v12-broad-discovery-ci.yml`
- `research/v12-broad-discovery-control.json`

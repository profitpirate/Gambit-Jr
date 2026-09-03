# Gambit Jr E4 V12 — recent role-model trade autopsy

Date: 2026-09-03  
Version policy: V12 is permanent; this is a new evidence epoch, not V13.

## Evidence window

The analysis used eight consecutive frozen V12 holdout artifacts, each covering exactly 3,000 fresh Pump launches:

- GitHub Actions runs `33605109571`, `33627431540`, `33644544746`, `33656916992`
- GitHub Actions runs `33669514091`, `33681664773`, `33692043170`, `33704841852`

Combined window:

- 24,000 fresh launches
- 83 closed E4 positions
- 53 E4 wins / 30 losses = 63.86% net win rate
- +39.239759 SOL net E4 P&L
- 79 positions linked exactly to captured CREATE/BUY/SELL events
- Linked cohort: 53 wins / 26 losses = 67.09%, +39.619185 SOL, profit factor 7.93
- V12: 8 closed positions, all in the first two windows, then zero entries for six consecutive windows
- V12 P&L across the eight windows: -0.027388 SOL

These figures describe this recent sampled period only. They are not a lifetime-performance claim.

## What E4 actually selected

### Entry position in the launch sequence

Of the 79 exactly linked trades:

- 27 entered as the second buy; 18 won, producing +21.40 SOL
- 7 entered as the third buy; 4 won, producing +5.36 SOL
- 9 entered as the fourth buy; 4 won
- 28 entered as the fifth buy; 20 won, producing +7.47 SOL
- 71 of 79 selections occurred by the fifth buy; all occurred by the tenth
- No selected launch had a sell before E4 entered

This is not a conventional late momentum strategy. The dominant behaviour is selection within the first two-to-five buys, after the creator seed and sometimes a small same-slot public/bot cluster.

### Entry timing

- Median observed entry age: approximately 4.8 ms
- Same creation slot or immediate next slot dominated
- Profitable next-slot entries were still observed in the 180–400 ms range
- The existing 150–180 ms V12 creator horizon therefore removed valid E4-like entries

The exact millisecond values depend on provider delivery and batching, but the order relationship is robust: creator launch/seed, then E4 very early, before any sell.

### Creator seed and FDV

- Overall median creator seed: 3.0 SOL
- Winner median creator seed: 3.0 SOL
- Loser median creator seed: 1.75 SOL
- Seed 2.0–3.1 SOL: 17 wins from 23 E4 selections, +18.16 SOL
- Seed 3.1–5.0 SOL: 15 wins from 18 selections, +8.36 SOL
- Seed <=0.5 SOL: 2 wins from 10 selections, negative net P&L

Best recent entry-FDV concentration:

- $4,000–$4,500: 7 wins from 10, +11.43 SOL
- $4,500–$5,000: 14 wins from 19, +10.95 SOL
- $5,500–$6,500: 7 wins from 8, +6.00 SOL
- $6,500–$8,500: 8 wins from 10, +8.70 SOL

An entry above approximately $12,000 FDV produced one of the largest losses, supporting retention of V12's $8,500 cap rather than broadening it indiscriminately.

### Creator recurrence

E4 did not depend exclusively on a frozen historical registry:

- 50 of 79 linked selections came from creators not previously seen in the eight-window chronology; these still returned 68% WR and +27.17 SOL
- 29 selections had at least one previous E4 creator trade; 65.5% WR and +12.45 SOL
- Creators with at least two previous E4 trades produced 80% WR in the sample
- Creators with at least two previous E4 wins produced 81.8% WR

The strongest recent creators included addresses with 6 wins from 7 trades and 4 wins from 4 trades that were absent from the frozen creator files. One historically known creator remained active despite an old 66.7% record, below V12's static 75% autonomous threshold.

## Root causes of the V12 recall failure

1. The holdout never registered E4 wallet BUY/SELL events with the copy pipeline. It updated generic launch learning only, so a zero-copy result was guaranteed.
2. Production could start the pipeline supervisor with no E4 wallet WebSocket workers because the expected WS environment variable was absent and existing RPC variables were not inherited.
3. Production started the background supervisor but not the live creator/social/intent runtime services.
4. The copy-exit module was not imported by the V12 production entrypoint.
5. The legacy copy-exit family name did not match the family returned by the active manager.
6. The manager converted a future E4 observation into age zero, allowing replay leakage if signals persisted across scenarios.
7. The creator pipeline used stale static files and horizons narrower than recent profitable E4 entry timing.
8. Generic early-flow imitation could not recover the hidden E4 selector with acceptable precision; first-seen creators make direct E4 observation indispensable.

## V12 pipeline design decision

V12 now treats the pipelines in this order:

1. Direct E4 BUY on the primary Pump event path: authoritative same-mint copy candidate.
2. Recent live-E4 creator outcome plus strong current launch seed: narrow assist path.
3. Existing historical creator and social/narrative authorities: retained with no public-flow-only shortcut.

The direct role-model path preserves the observed no-pre-entry-sell and FDV protections. It maps E4's observed stake band onto V12 risk tiers and mirrors E4's cumulative sell fraction while retaining independent failure protection.

## Evidence integrity

The role-model module is pinned by its SHA-256 in both hashed V12 entrypoints. Changing it requires changing the frozen V12 strategy fingerprint. Existing pre-upgrade evidence is archived and a clean V12 evidence epoch begins; results from different fingerprints are never mixed.

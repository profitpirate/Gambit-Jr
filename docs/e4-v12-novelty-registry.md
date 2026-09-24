# E4 V12 novelty-aware research registry

The ten-minute watchdog treats a golden-thesis run as a scientific experiment,
not as a scheduled task. Before dispatch, it hashes the complete experiment
identity and consults the durable registry at
`artifacts/e4-v12-experiment-registry.json`.

The identity contains the thesis family, relevant source tree, exact
dataset/source manifest, evidence epoch, features, model, complete workflow
parameters, causal horizon and risk set, chronological split, bankroll,
position sizing, fees, output guard, latency assumptions, execution policy and
exit policy. Canonical JSON and SHA-256 make the ID deterministic.

Frozen workflows are keyed by their explicit evidence run IDs. Rolling
workflows are keyed by the successful evidence runs visible to their declared
risk-set policy. A scheduler run by itself does not change the committed-data
fingerprint, and new rolling evidence does not make frozen experiments novel.

Scientific failures are retired and cannot run again under the same identity.
Only `INFRASTRUCTURE_FAILURE` can retry unchanged after its configured backoff.
Active runs can be restarted after the stale limit. Fresh evidence collection
remains an independent lane and is not registered as a thesis experiment.

The integrity report on `codex/e4-v12-canonical-choice-set` must pass before
any scientific dispatch or golden stop decision. Research stops only when a
recognised sentinel and an explicit untouched-live verdict both pass while the
integrity gate is green.

Every watchdog tick atomically updates the registry and writes the standard
holdout-first leaderboard in JSON and Markdown. Experiments without
chronological holdout evidence are always ranked below those with holdout
evidence.

Run the local contract checks with:

```bash
python -m unittest discover -s tests -p 'test_e4_v12_experiment_registry.py' -v
ruff check .
```

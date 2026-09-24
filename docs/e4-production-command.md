# Canonical E4 production command

## Fail-closed boundary

The execution engine now requires **both** the command-line acknowledgement and the environment
interlock. The checked-in example keeps `E4_LIVE=false`. Do not change that value merely to make a
test pass.

Before any external execution integration, run the credential-free checks:

```bash
python scripts/v12_prearmed_readiness.py --no-fail
python scripts/v12_prearmed_brutal_stress.py --iterations 100000
python scripts/e4_security_audit.py
```

The frozen V12 Pre-Armed causal confirmation is still research-only until the fresh 100-trade
completion and acceptance fields say otherwise. The readiness gate intentionally reports that as a
blocker.

## Existing E4 execution subsystem

The separate E4 execution subsystem uses the hardened entrypoint:

```bash
python -m memecoin_bot.e4_prod migrate
E4_LIVE=true python -m memecoin_bot.e4_prod run --live
```

Those commands describe the existing execution subsystem; they do **not** mean the frozen V12
Pre-Armed research selector has been granted transaction authority.

Container deployment is defined in `docker-compose.e4-prod.yml`. It uses a read-only root
filesystem, dropped capabilities, `no-new-privileges`, a read-only secret mount, bounded logs and
explicit writable runtime mounts. The local Pump builder is the default; remote builder fallback is
off unless an operator explicitly opts in.

`e4_prod` applies exact-schema position persistence, V1.5 nested-event normalization, explicit
canonical-table override support, and restart/on-chain position reconciliation before the live event
loop begins.

See [`e4-security-hardening.md`](e4-security-hardening.md) for secret handling and the remaining
external operator boundary.

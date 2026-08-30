# Canonical E4 production command

Use the hardened entrypoint:

```bash
python -m memecoin_bot.e4_prod migrate
E4_LIVE=true python -m memecoin_bot.e4_prod run --live
```

Container deployment:

```bash
E4_KEYPAIR_HOST_PATH=/secure/path/id.json \
  docker compose -f docker-compose.e4-prod.yml up -d --build
```

`e4_prod` applies exact-schema position persistence, V1.5 nested-event normalization, explicit canonical-table override support, and restart/on-chain position reconciliation before the live event loop begins.

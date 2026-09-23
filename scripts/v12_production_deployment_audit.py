"""Static fail-closed production deployment audit for V12 NextGen."""
from __future__ import annotations
import json, os
from pathlib import Path

REQUIRED_FILES=[
"src/memecoin_bot/v12_axiom_wallet.py","src/memecoin_bot/v12_vault_signer.py",
"src/memecoin_bot/v12_execution_journal.py","src/memecoin_bot/v12_recovery.py",
"src/memecoin_bot/v12_live_readiness.py","src/memecoin_bot/v12_safety.py",
"src/memecoin_bot/v12_route_health.py","src/memecoin_bot/v12_backup.py",
"src/memecoin_bot/v12_watchdogs.py","src/memecoin_bot/e4_production_guard_v12.py",
"src/memecoin_bot/e4_sub10ms_transport_final_v12.py","scripts/v12_supervisor.py",
]
FORBIDDEN_ENV=["AXIOM_PASSWORD","AXIOM_SESSION","AXIOM_COOKIE","AXIOM_AUTH_TOKEN"]

def audit(root: Path)->dict:
    missing=[p for p in REQUIRED_FILES if not (root/p).is_file()]
    env_example=(root/".env.e4.example").read_text(encoding="utf-8")
    forbidden=[k for k in FORBIDDEN_ENV if k in env_example]
    local_default="E4_KEYPAIR_PATH=/run/secrets/e4-solana-keypair.json" in env_example
    return {
      "status":"PASS" if not missing and not forbidden and not local_default else "FAIL",
      "missing_required_files":missing,
      "forbidden_axiom_credential_surface":forbidden,
      "legacy_plaintext_keypair_default":local_default,
      "execution_architecture":"NATIVE_SOLANA",
      "axiom_role":"DISPLAY_ONLY",
      "requires_causal_100_gate":True,
      "requires_external_vault_signer":True,
      "requires_rpc_redundancy":True,
      "requires_route_redundancy":True,
      "requires_encrypted_backups":True,
      "requires_tamper_evident_audit":True,
    }

if __name__=="__main__":
    result=audit(Path.cwd())
    print(json.dumps(result,indent=2,sort_keys=True))
    Path("research/v12-production-deployment-audit.json").write_text(json.dumps(result,indent=2,sort_keys=True)+"\n")
    raise SystemExit(0 if result["status"]=="PASS" else 2)

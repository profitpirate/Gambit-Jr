"""Build and optionally Ed25519-sign the V12 critical-code integrity manifest."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from memecoin_bot.v12_security import build_manifest

DEFAULT_PATHS = (
    "src/memecoin_bot/e4_exec/__init__.py",
    "src/memecoin_bot/e4_exec/__main__.py",
    "src/memecoin_bot/e4_prod/__init__.py",
    "src/memecoin_bot/e4_prod/__main__.py",
    "src/memecoin_bot/e4_live.py",
    "src/memecoin_bot/e4_final.py",
    "src/memecoin_bot/e4_production.py",
    "src/memecoin_bot/e4_hardening.py",
    "src/memecoin_bot/e4_hardening_v2.py",
    "src/memecoin_bot/e4_hardening_v3.py",
    "src/memecoin_bot/e4_hardening_v4.py",
    "src/memecoin_bot/e4_hardening_v5.py",
    "src/memecoin_bot/e4_hardening_v6.py",
    "src/memecoin_bot/e4_hardening_v7.py",
    "src/memecoin_bot/e4_hardening_v8.py",
    "src/memecoin_bot/e4_hardening_v9.py",
    "src/memecoin_bot/e4_hardening_v10.py",
    "src/memecoin_bot/e4_hardening_v12.py",
    "src/memecoin_bot/e4_pipeline/__init__.py",
    "src/memecoin_bot/e4_pipeline/coordinator.py",
    "src/memecoin_bot/e4_pipeline/models.py",
    "src/memecoin_bot/e4_pipeline/narrative.py",
    "src/memecoin_bot/e4_pipeline/registry.py",
    "src/memecoin_bot/e4_pipeline/runtime.py",
    "src/memecoin_bot/e4_pipeline/teacher.py",
    "src/memecoin_bot/e4_pipeline/x_stream.py",
    "src/memecoin_bot/e4_pipelines_v10.py",
    "src/memecoin_bot/e4_pipeline_manager_v11.py",
    "src/memecoin_bot/e4_pipeline_runtime_v10.py",
    "src/memecoin_bot/e4_runtime_services_v10.py",
    "src/memecoin_bot/e4_v12_authority.py",
    "src/memecoin_bot/e4_role_model_v12.py",
    "src/memecoin_bot/e4_direct_copy_v12.py",
    "src/memecoin_bot/e4_tight_output_v12.py",
    "src/memecoin_bot/e4_strict_output_v12.py",
    "src/memecoin_bot/e4_strict_output_deferred_v12.py",
    "src/memecoin_bot/e4_sub10ms_repairs_v12.py",
    "src/memecoin_bot/e4_nextgen_creator_authority_v12.py",
    "src/memecoin_bot/e4_adaptive_exit_v12.py",
    "src/memecoin_bot/e4_sub10ms_transport_final_v12.py",
    "src/memecoin_bot/e4_sub10ms_runtime_final_v12.py",
    "src/memecoin_bot/e4_production_guard_v12.py",
    "src/memecoin_bot/v12_security.py",
    "src/memecoin_bot/v12_live_readiness.py",
    "src/memecoin_bot/v12_vault_signer.py",
    "src/memecoin_bot/v12_execution_journal.py",
    "src/memecoin_bot/v12_recovery.py",
    "src/memecoin_bot/v12_safety.py",
    "src/memecoin_bot/v12_route_health.py",
    "src/memecoin_bot/v12_capacity.py",
    "src/memecoin_bot/v12_backup.py",
    "src/memecoin_bot/v12_watchdogs.py",
    "src/memecoin_bot/v12_observability.py",
    "src/memecoin_bot/v12_marketdata.py",
    "src/memecoin_bot/v12_creator_library.py",
    "src/memecoin_bot/v12_creator_lifecycle.py",
    "src/memecoin_bot/v12_learning_service.py",
    "src/memecoin_bot/v12_operator_graph.py",
    "src/memecoin_bot/v12_postcert_risk.py",
    "src/memecoin_bot/v12_forensics.py",
    "src/memecoin_bot/database/store.py",
    "src/memecoin_bot/service.py",
    "src/memecoin_bot/realtime/__init__.py",
    "src/memecoin_bot/realtime/events.py",
    "src/memecoin_bot/realtime/providers.py",
    "src/memecoin_bot/realtime/fabric.py",
    "src/memecoin_bot/realtime/pumpfun.py",
    "src/memecoin_bot/realtime/lanes.py",
    "src/memecoin_bot/realtime/features.py",
    "src/memecoin_bot/realtime/incremental.py",
    "src/memecoin_bot/providers/base.py",
    "src/memecoin_bot/providers/solana_rpc.py",
    "src/memecoin_bot/main.py",
    "src/memecoin_bot/config.py",
    "scripts/v12_supervisor.py",
    "tools/e4-builder/race-proxy-v3.mjs",
    "tools/e4-builder/daemon-v2.mjs",
    "tools/e4-builder/daemon.mjs",
    "tools/e4-builder/strict-output-guard-v12.mjs",
    "tools/e4-builder/strict-race-proxy-v12.mjs",
    "tools/e4-builder/fast-preload-v4.mjs",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("security/v12-integrity-manifest.json"),
    )
    parser.add_argument("--signing-key", type=Path)
    parser.add_argument("--signature-output", type=Path)
    args = parser.parse_args()

    payload = build_manifest(args.root, DEFAULT_PATHS)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(args.output, 0o644)

    if args.signing_key:
        from solders.keypair import Keypair

        raw = args.signing_key.read_text(encoding="utf-8").strip()
        keypair = (
            Keypair.from_bytes(bytes(json.loads(raw)))
            if raw.startswith("[")
            else Keypair.from_base58_string(raw)
        )
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        signature = str(keypair.sign_message(canonical))
        destination = args.signature_output or args.output.with_suffix(".sig")
        destination.write_text(signature + "\n", encoding="utf-8")
        print(json.dumps({
            "manifest": str(args.output),
            "signature": str(destination),
            "public_key": str(keypair.pubkey()),
        }))
    else:
        print(json.dumps({
            "manifest": str(args.output),
            "unsigned": True,
        }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Static fail-closed production deployment audit for V12 NextGen."""
from __future__ import annotations

import json
from pathlib import Path

REQUIRED_FILES = [
    "src/memecoin_bot/v12_axiom_wallet.py",
    "src/memecoin_bot/v12_vault_signer.py",
    "src/memecoin_bot/v12_execution_journal.py",
    "src/memecoin_bot/v12_recovery.py",
    "src/memecoin_bot/v12_live_readiness.py",
    "src/memecoin_bot/v12_safety.py",
    "src/memecoin_bot/v12_route_health.py",
    "src/memecoin_bot/v12_backup.py",
    "src/memecoin_bot/v12_watchdogs.py",
    "src/memecoin_bot/e4_production_guard_v12.py",
    "src/memecoin_bot/e4_sub10ms_transport_final_v12.py",
    "scripts/v12_supervisor.py",
]
FORBIDDEN_ENV = ["AXIOM_PASSWORD", "AXIOM_SESSION", "AXIOM_COOKIE", "AXIOM_AUTH_TOKEN"]
INTEGRITY_REQUIRED = [
    "src/memecoin_bot/e4_exec/__main__.py",
    "src/memecoin_bot/e4_live.py",
    "src/memecoin_bot/e4_final.py",
    "src/memecoin_bot/e4_production_guard_v12.py",
    "src/memecoin_bot/v12_live_readiness.py",
    "src/memecoin_bot/v12_vault_signer.py",
    "src/memecoin_bot/v12_execution_journal.py",
    "src/memecoin_bot/v12_recovery.py",
    "src/memecoin_bot/v12_safety.py",
    "src/memecoin_bot/v12_route_health.py",
    "src/memecoin_bot/v12_backup.py",
    "src/memecoin_bot/v12_watchdogs.py",
    "scripts/v12_supervisor.py",
]


def _read(root: Path, relative: str) -> str:
    return (root / relative).read_text(encoding="utf-8")


def audit(root: Path) -> dict:
    missing = [path for path in REQUIRED_FILES if not (root / path).is_file()]
    env_example = _read(root, ".env.e4.example")
    exec_main = _read(root, "src/memecoin_bot/e4_exec/__main__.py")
    exec_init = _read(root, "src/memecoin_bot/e4_exec/__init__.py")
    prod_init = _read(root, "src/memecoin_bot/e4_prod/__init__.py")
    prod_main = _read(root, "src/memecoin_bot/e4_prod/__main__.py")
    compose_exec = _read(root, "docker-compose.e4-exec.yml")
    compose_prod = _read(root, "docker-compose.e4-prod.yml")
    docker_exec = _read(root, "Dockerfile.e4-exec")
    docker_prod = _read(root, "Dockerfile.e4-prod")
    supervisor = _read(root, "scripts/v12_supervisor.py")
    pyproject = _read(root, "pyproject.toml")
    integrity_builder = _read(root, "scripts/v12_integrity_manifest.py")

    forbidden = [key for key in FORBIDDEN_ENV if key in env_example]
    local_keypair_surface = any(
        token in env_example or token in compose_exec or token in compose_prod
        for token in ("E4_KEYPAIR_PATH=", "E4_KEYPAIR_HOST_PATH")
    )
    integrity_missing = [
        path for path in INTEGRITY_REQUIRED if f'"{path}"' not in integrity_builder
    ]

    canonical_cli_gate = all(
        token in exec_main
        for token in (
            "def main() -> None:",
            "_preflight_live_if_requested()",
            "_start_v12_pipelines()",
            "_engine_main()",
        )
    )
    package_main_safe = "canonical_main()" in exec_init
    legacy_alias_safe = (
        "memecoin_bot.e4_exec.__main__" in prod_init
        and "canonical_main()" in prod_init
        and "from memecoin_bot.e4_prod import main" in prod_main
    )

    compose_checks = {}
    for name, payload in (("exec", compose_exec), ("prod", compose_prod)):
        compose_checks[name] = {
            "canonical_entrypoint": "memecoin_bot.e4_exec" in payload,
            "vault_secret": "VAULT_TOKEN_FILE: /run/secrets/vault-token" in payload
            and "vault_token:" in payload,
            "no_plaintext_keypair": "E4_KEYPAIR_HOST_PATH" not in payload,
            "read_only": "read_only: true" in payload,
            "no_new_privileges": "no-new-privileges:true" in payload,
            "cap_drop_all": "cap_drop:" in payload and "- ALL" in payload,
            "healthcheck": "healthcheck:" in payload and "/healthz" in payload,
        }

    docker_checks = {}
    for name, payload in (("exec", docker_exec), ("prod", docker_prod)):
        docker_checks[name] = {
            "canonical_entrypoint": "memecoin_bot.e4_exec" in payload,
            "locked_node_install": "npm ci --omit=dev" in payload,
            "non_root_user": "USER gambit" in payload,
        }

    env_checks = {
        "external_signer": "E4_SIGNER_COMMAND=python -m memecoin_bot.v12_vault_signer" in env_example,
        "vault_reference": "V12_SIGNER_SECRET_REF=vault://" in env_example,
        "vault_token_file": "VAULT_TOKEN_FILE=/run/secrets/vault-token" in env_example,
        "canonical_builder": "E4_BUILDER_COMMAND=node tools/e4-builder/race-proxy-v3.mjs" in env_example,
        "fail_closed_live_default": "V12_LIVE_TRADING_ENABLED=false" in env_example,
        "no_literal_newline_escape": "\\nV12_SIGNER_SECRET_REF" not in env_example,
    }

    other_checks = {
        "supervisor_uses_exec": '"memecoin_bot.e4_exec"' in supervisor,
        "console_script_uses_safe_wrapper": 'gambit-e4 = "memecoin_bot.e4_exec.__main__:main"' in pyproject,
    }

    checks = {
        "canonical_cli_gate": canonical_cli_gate,
        "package_main_safe": package_main_safe,
        "legacy_alias_safe": legacy_alias_safe,
        "compose": compose_checks,
        "docker": docker_checks,
        "env": env_checks,
        "other": other_checks,
    }

    def all_true(value) -> bool:
        if isinstance(value, dict):
            return all(all_true(item) for item in value.values())
        return bool(value)

    passed = (
        not missing
        and not forbidden
        and not local_keypair_surface
        and not integrity_missing
        and all_true(checks)
    )
    return {
        "status": "PASS" if passed else "FAIL",
        "missing_required_files": missing,
        "forbidden_axiom_credential_surface": forbidden,
        "legacy_plaintext_keypair_surface": local_keypair_surface,
        "integrity_manifest_missing_critical_paths": integrity_missing,
        "checks": checks,
        "execution_architecture": "NATIVE_SOLANA",
        "canonical_live_entrypoint": "memecoin_bot.e4_exec",
        "axiom_role": "DISPLAY_ONLY",
        "requires_causal_100_gate": True,
        "requires_external_vault_signer": True,
        "requires_rpc_redundancy": True,
        "requires_route_redundancy": True,
        "requires_encrypted_backups": True,
        "requires_tamper_evident_audit": True,
    }


if __name__ == "__main__":
    result = audit(Path.cwd())
    print(json.dumps(result, indent=2, sort_keys=True))
    Path("research/v12-production-deployment-audit.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    raise SystemExit(0 if result["status"] == "PASS" else 2)

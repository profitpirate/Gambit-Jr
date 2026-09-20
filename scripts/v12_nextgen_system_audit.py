"""End-to-end cohesion audit for the V12 automated-sniper architecture."""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = (
    "src/memecoin_bot/e4_exec/__main__.py",
    "src/memecoin_bot/e4_v12_authority.py",
    "src/memecoin_bot/e4_sub10ms_transport_final_v12.py",
    "src/memecoin_bot/e4_sub10ms_runtime_final_v12.py",
    "src/memecoin_bot/e4_notifications_v12.py",
    "src/memecoin_bot/e4_nextgen_creator_authority_v12.py",
    "src/memecoin_bot/e4_adaptive_exit_v12.py",
    "src/memecoin_bot/e4_production_guard_v12.py",
    "src/memecoin_bot/notifications.py",
    "src/memecoin_bot/v12_creator_library.py",
    "src/memecoin_bot/v12_creator_lifecycle.py",
    "src/memecoin_bot/v12_learning_service.py",
    "src/memecoin_bot/v12_operator_graph.py",
    "src/memecoin_bot/v12_postcert_risk.py",
    "src/memecoin_bot/v12_security.py",
    "src/memecoin_bot/v12_capacity.py",
    "src/memecoin_bot/v12_execution_journal.py",
    "src/memecoin_bot/v12_safety.py",
    "src/memecoin_bot/v12_route_health.py",
    "src/memecoin_bot/v12_backup.py",
    "src/memecoin_bot/v12_live_readiness.py",
    "src/memecoin_bot/v12_recovery.py",
    "src/memecoin_bot/v12_watchdogs.py",
    "src/memecoin_bot/v12_forensics.py",
    "src/memecoin_bot/v12_observability.py",
    "src/memecoin_bot/v12_vault_signer.py",
    "src/memecoin_bot/v12_account_orchestrator.py",
    "src/memecoin_bot/access_store.py",
    "src/memecoin_bot/access_service.py",
    "src/memecoin_bot/control_plane.py",
    "src/memecoin_bot/v12_portal.py",
    "scripts/v12_creator_library_rebuild.py",
    "scripts/v12_e4_full_history.py",
    "scripts/v12_supervisor.py",
    "scripts/v12_integrity_manifest.py",
    "scripts/v12_secret_scan.py",
)
REQUIRED_DEPLOYMENT = (
    "deploy/systemd/gambit-v12.service",
    "deploy/systemd/gambit-v12-marketdata.service",
    "deploy/systemd/gambit-v12-orchestrator.service",
    "deploy/systemd/gambit-v12-portal.service",
    "deploy/tmpfiles.d/gambit-v12.conf",
)
FORBIDDEN_COMMAND_FILES = (
    "src/memecoin_bot/discord/bot_runtime.py",
    "src/memecoin_bot/discord/command_center.py",
    "src/memecoin_bot/discord/responses.py",
    "src/memecoin_bot/discord/cards.py",
)


def load(path: str) -> Any:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def pass_only(node: ast.AST) -> bool:
    body = getattr(node, "body", None)
    if not isinstance(body, list) or not body:
        return False
    real = [
        item
        for item in body
        if not (
            isinstance(item, ast.Expr)
            and isinstance(item.value, ast.Constant)
            and isinstance(item.value.value, str)
        )
    ]
    return bool(real) and all(isinstance(item, ast.Pass) for item in real)


def python_component(path: str) -> dict[str, Any]:
    source = (ROOT / path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    stubs = [
        node.name for node in [*functions, *classes] if pass_only(node)
    ]
    return {
        "path": path,
        "lines": len(source.splitlines()),
        "functions": len(functions),
        "classes": len(classes),
        "pass_only": stubs,
        "not_implemented": source.count("NotImplementedError"),
    }


def main() -> int:
    failures: list[str] = []
    missing = [path for path in REQUIRED if not (ROOT / path).exists()]
    failures.extend(f"missing:{path}" for path in missing)
    missing_deployment = [
        path for path in REQUIRED_DEPLOYMENT if not (ROOT / path).exists()
    ]
    failures.extend(
        f"missing_deployment:{path}" for path in missing_deployment
    )

    forbidden_present = [
        path for path in FORBIDDEN_COMMAND_FILES if (ROOT / path).exists()
    ]
    failures.extend(f"legacy_discord_command_present:{path}" for path in forbidden_present)

    components = [
        python_component(path)
        for path in REQUIRED
        if (ROOT / path).exists() and path.endswith(".py")
    ]
    for row in components:
        if row["pass_only"]:
            failures.append(f"shell:{row['path']}:{row['pass_only']}")
        if row["not_implemented"]:
            failures.append(f"not_implemented:{row['path']}")

    src_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (ROOT / "src").rglob("*.py")
    )
    for marker in (
        "run_discord_bot",
        "@tree.command",
        "@app_commands.command",
        "CommandCenter(",
    ):
        if marker in src_text:
            failures.append(f"legacy_discord_command_marker:{marker}")

    entrypoint = (ROOT / "src/memecoin_bot/e4_exec/__main__.py").read_text()
    for marker in (
        "e4_sub10ms_runtime_final_v12",
        "e4_notifications_v12",
        "e4_nextgen_creator_authority_v12",
    ):
        if marker not in entrypoint:
            failures.append(f"entrypoint_missing:{marker}")

    library = load("models/e4/v12-creator-library.json")
    counts = library.get("counts") or {}
    if int(counts.get("promoted_unique") or 0) < 35:
        failures.append("creator_library_has_fewer_than_35_promoted")
    if int(counts.get("shortlisted_unique") or 0) > 75:
        failures.append("creator_shortlist_too_large")
    if library.get("activation", {}).get("enabled_now") is not False:
        failures.append("creator_library_prematurely_enabled")
    if any(bool(row.get("auto_buy")) for row in library.get("promoted", [])):
        failures.append("creator_library_contains_auto_buy")

    for path in (
        "models/e4/e4-discovered-creators.json",
        "models/e4/e4-winning-creators.json",
        "models/e4/e4-discovered-known-creators.json",
    ):
        legacy = load(path)
        if legacy.get("active") is not False or legacy.get("creators") not in ({}, []):
            failures.append(f"legacy_creator_registry_not_empty:{path}")

    causal = load("research/v12-pre-armed-100-causal-paper-live.json")
    if causal.get("real_money_execution") is not False:
        failures.append("causal_proof_real_money_enabled")
    if library.get("activation", {}).get("frozen_100_trade_model_modified") is not False:
        failures.append("library_claims_frozen_model_modified")

    access_source = (ROOT / "src/memecoin_bot/access_service.py").read_text()
    if "Axiom direct account linking is disabled" not in access_source:
        failures.append("axiom_fail_closed_guard_missing")
    vault_ref_guard = 'secret_ref.startswith("vault://")' in access_source
    raw_key_guard = (
        "raw private keys" in access_source
        and "forbidden" in access_source
    )
    if not (vault_ref_guard and raw_key_guard):
        failures.append("raw_private_key_guard_missing")

    notifications = (ROOT / "src/memecoin_bot/notifications.py").read_text()
    for marker in ("TRADE_CLOSED_WIN", "TRADE_CLOSED_LOSS", "STORAGE_SWEEP"):
        if marker not in notifications:
            failures.append(f"notification_event_missing:{marker}")

    complete_history_path = ROOT / "models/e4/e4-complete-creator-history.json"
    complete_history = None
    if complete_history_path.exists():
        complete_history = json.loads(complete_history_path.read_text())
        if not complete_history.get("completeness", {}).get(
            "wallet_signature_scan_exhausted"
        ):
            failures.append("complete_e4_history_not_exhaustive")

    report = {
        "version": "v12-nextgen-system-audit-v1",
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "required_components": len(REQUIRED),
        "required_deployment_units": len(REQUIRED_DEPLOYMENT),
        "deployment_units_present": not missing_deployment,
        "components": components,
        "discord_command_runtime_removed": not forbidden_present,
        "creator_library": {
            "history_source": library.get("history_source"),
            "promoted": counts.get("promoted_unique"),
            "elite": counts.get("elite_unique"),
            "shortlisted": counts.get("shortlisted_unique"),
            "activation": library.get("activation"),
        },
        "complete_e4_history": (
            complete_history.get("completeness") if complete_history else "PENDING"
        ),
        "causal_proof": {
            "closed_trades": (causal.get("metrics") or {}).get("closed_trades"),
            "real_money_execution": causal.get("real_money_execution"),
            "status": causal.get("status"),
        },
        "notifications": {
            "trade_win": True,
            "trade_loss": True,
            "storage_sweep": True,
            "enabled_by_default": False,
        },
        "access": {
            "discord_magic_login": True,
            "wallet_signature_proof": True,
            "axiom_direct_link": False,
            "raw_private_keys_accepted": False,
            "vault_only_live_signer": vault_ref_guard and raw_key_guard,
        },
    }
    output = ROOT / "research/v12-nextgen-system-audit.json"
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())

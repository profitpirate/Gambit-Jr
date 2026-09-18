#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from memecoin_bot.config import Settings
from memecoin_bot.database import Store
from memecoin_bot.e4_live import Settings as E4Settings

ROOT = Path(__file__).resolve().parents[1]
PINNED_ACTION = re.compile(
    r"uses:\s+actions/[A-Za-z0-9_.-]+@([0-9a-f]{40})(?:\s+#.*)?$"
)
APPROVED_WRITE_WORKFLOWS = {
    "e4-creator-expectancy.yml",
    "e4-winning-creator-registry.yml",
}


def workflow_audit() -> dict[str, Any]:
    failures: list[str] = []
    rows: list[dict[str, Any]] = []
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        source = path.read_text()
        action_lines = [
            line.strip() for line in source.splitlines() if "uses: actions/" in line
        ]
        unpinned = [line for line in action_lines if not PINNED_ACTION.search(line)]
        explicit_permissions = "permissions:" in source
        write_permission = bool(re.search(r"(?m)^\s*contents:\s*write\s*$", source))
        if unpinned:
            failures.append(f"{path.name}: unpinned actions {unpinned}")
        if not explicit_permissions:
            failures.append(f"{path.name}: missing explicit permissions")
        if write_permission and path.name not in APPROVED_WRITE_WORKFLOWS:
            failures.append(f"{path.name}: unexpected contents:write permission")
        if "pull_request_target:" in source:
            failures.append(f"{path.name}: pull_request_target is forbidden")
        if re.search(r"(?:curl|wget)[^\n]*\|\s*(?:bash|sh)", source):
            failures.append(f"{path.name}: remote script pipe detected")
        rows.append(
            {
                "workflow": path.name,
                "actions": len(action_lines),
                "permissions_explicit": explicit_permissions,
                "contents_write": write_permission,
                "unpinned": len(unpinned),
            }
        )
    return {"passed": not failures, "failures": failures, "workflows": rows}


def dependency_audit() -> dict[str, Any]:
    package_path = ROOT / "tools" / "e4-builder" / "package.json"
    lock_path = ROOT / "tools" / "e4-builder" / "package-lock.json"
    package = json.loads(package_path.read_text())
    lock = json.loads(lock_path.read_text()) if lock_path.exists() else {}
    dependencies = package.get("dependencies") or {}
    floating = {
        name: version
        for name, version in dependencies.items()
        if any(marker in str(version) for marker in ("^", "~", "*", "latest"))
    }
    lock_root = ((lock.get("packages") or {}).get("") or {}).get("dependencies") or {}
    mismatch = {
        name: {"package": version, "lock": lock_root.get(name)}
        for name, version in dependencies.items()
        if lock_root.get(name) != version
    }
    return {
        "passed": lock_path.exists() and not floating and not mismatch,
        "lockfile": lock_path.exists(),
        "lockfile_version": lock.get("lockfileVersion"),
        "direct_dependencies": len(dependencies),
        "floating_direct_dependencies": floating,
        "root_lock_mismatch": mismatch,
    }


def container_audit() -> dict[str, Any]:
    failures: list[str] = []
    compose_rows: list[dict[str, Any]] = []
    e4_composes = {
        "docker-compose.e4.yml",
        "docker-compose.e4-exec.yml",
        "docker-compose.e4-prod.yml",
    }
    for name in (
        "docker-compose.yml",
        "docker-compose.e4.yml",
        "docker-compose.e4-exec.yml",
        "docker-compose.e4-prod.yml",
    ):
        source = (ROOT / name).read_text()
        requirements = {
            "read_only": "read_only: true" in source,
            "cap_drop_all": "cap_drop:" in source and "- ALL" in source,
            "no_new_privileges": "no-new-privileges:true" in source,
            "tmpfs": "tmpfs:" in source,
            "non_root_runtime": (
                'user: "${E4_RUNTIME_UID:-1000}:${E4_RUNTIME_GID:-1000}"' in source
                if name in e4_composes
                else True
            ),
        }
        if not all(requirements.values()):
            failures.append(f"{name}: {requirements}")
        compose_rows.append({"file": name, **requirements})
    docker_rows = []
    for name in ("Dockerfile.e4", "Dockerfile.e4-exec", "Dockerfile.e4-prod"):
        source = (ROOT / name).read_text()
        requirements = {
            "node22_stage": "FROM node:22-slim AS node-runtime" in source,
            "npm_ci": "npm ci --omit=dev" in source,
            "python312": "FROM python:3.12-slim" in source,
        }
        if not all(requirements.values()):
            failures.append(f"{name}: {requirements}")
        docker_rows.append({"file": name, **requirements})
    return {
        "passed": not failures,
        "failures": failures,
        "compose": compose_rows,
        "dockerfiles": docker_rows,
    }


def database_audit() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="fullbot-audit-") as directory:
        path = Path(directory) / "audit.db"
        store = Store(path, ROOT / "migrations")
        store.migrate()
        store.migrate()
        columns = {row["name"] for row in store.conn.execute("PRAGMA table_info(outbox)")}
        migration_count = int(
            store.conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        )
        integrity = store.database_integrity()
        reconciliation = store.state_reconciliation()
        required = {
            "claim_token",
            "claimed_at",
            "next_attempt_at",
            "dead_lettered_at",
        }
        passed = (
            required <= columns
            and integrity["healthy"]
            and reconciliation["reconciled"]
            and migration_count >= 14
        )
        store.close()
        return {
            "passed": passed,
            "migration_count": migration_count,
            "outbox_hardening_columns": sorted(required & columns),
            "database_integrity": integrity,
            "state_reconciliation": reconciliation,
        }


def config_audit() -> dict[str, Any]:
    Settings().validate()
    E4Settings().validate()
    return {
        "passed": True,
        "main_defaults_valid": True,
        "e4_defaults_valid": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    sections = {
        "workflows": workflow_audit(),
        "dependencies": dependency_audit(),
        "containers": container_audit(),
        "database": database_audit(),
        "configuration": config_audit(),
    }
    report = {
        "version": "fullbot-architecture-audit-v1",
        "passed": all(section["passed"] for section in sections.values()),
        "sections": sections,
    }
    encoded = json.dumps(report, indent=2, sort_keys=True)
    print(encoded)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Static security audit for Gambit Jr E4/Pre-Armed infrastructure.

The audit is deliberately credential-free and never opens a wallet or network route.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BANNED_TRACKED = {
    ".env",
    ".env.e4",
    "id.json",
    "keypair.json",
    "solana-keypair.json",
}
SECRET_ASSIGNMENT = re.compile(
    r"(?im)^\s*(E4_PRIVATE_KEY|E4_SECRET_KEY|E4_SEED_PHRASE|SOLANA_PRIVATE_KEY|PHANTOM_SEED_PHRASE)\s*=\s*(.+?)\s*$"
)
LONG_JSON_ARRAY = re.compile(r"\[(?:\s*\d+\s*,){31,}\s*\d+\s*\]")


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def audit() -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    files = tracked_files()

    for name in files:
        base = Path(name).name.lower()
        if name in BANNED_TRACKED or base in BANNED_TRACKED:
            findings.append({"severity": "critical", "code": "TRACKED_SECRET_FILE", "path": name})

    for name in files:
        path = ROOT / name
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for match in SECRET_ASSIGNMENT.finditer(text):
            value = match.group(2).strip().strip('"').strip("'")
            if value and value not in {"", "<redacted>", "<secret>", "CHANGE_ME"}:
                findings.append(
                    {
                        "severity": "critical",
                        "code": "RAW_SECRET_ASSIGNMENT",
                        "path": name,
                        "detail": match.group(1),
                    }
                )
        if LONG_JSON_ARRAY.search(text) and "test" not in name.lower() and "fixture" not in name.lower():
            findings.append(
                {
                    "severity": "warning",
                    "code": "POSSIBLE_RAW_KEY_ARRAY",
                    "path": name,
                }
            )

    env_example = ROOT / ".env.e4.example"
    if env_example.exists():
        env_text = env_example.read_text(encoding="utf-8")
        if re.search(r"(?m)^E4_LIVE\s*=\s*true\s*$", env_text):
            findings.append({"severity": "high", "code": "LIVE_DEFAULT_ENABLED", "path": ".env.e4.example"})
        if re.search(r"(?m)^E4_REMOTE_BUILDER_FALLBACK\s*=\s*true\s*$", env_text):
            findings.append({"severity": "medium", "code": "REMOTE_BUILDER_FALLBACK_DEFAULT_ENABLED", "path": ".env.e4.example"})

    compose = ROOT / "docker-compose.e4-prod.yml"
    if compose.exists():
        body = compose.read_text(encoding="utf-8")
        required = {
            "KEYPAIR_MOUNT_READ_ONLY": ":/run/secrets/e4-solana-keypair.json:ro",
            "NO_NEW_PRIVILEGES": "no-new-privileges:true",
            "CAP_DROP_ALL": "cap_drop:",
            "READ_ONLY_ROOT": "read_only: true",
        }
        for code, needle in required.items():
            if needle not in body:
                findings.append({"severity": "high", "code": code + "_MISSING", "path": compose.name})

    counts: dict[str, int] = {}
    for row in findings:
        counts[row["severity"]] = counts.get(row["severity"], 0) + 1
    failed = any(row["severity"] in {"critical", "high"} for row in findings)
    return {
        "version": "e4-security-audit-v1",
        "tracked_files": len(files),
        "findings": findings,
        "counts": counts,
        "passed": not failed,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--no-fail", action="store_true")
    args = parser.parse_args()
    result = audit()
    body = json.dumps(result, indent=2, sort_keys=True)
    print(body)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(body + "\n", encoding="utf-8")
    return 0 if result["passed"] or args.no_fail else 1


if __name__ == "__main__":
    raise SystemExit(main())

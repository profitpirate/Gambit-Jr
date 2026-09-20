#!/usr/bin/env python3
"""High-confidence secret scan for tracked Gambit source/config files."""
from __future__ import annotations

import math
import re
import subprocess
from pathlib import Path


PATTERNS = (
    ("private_pem", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "discord_bot_token",
        re.compile(r"\b(?:M|N)[A-Za-z0-9_-]{23,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{25,}\b"),
    ),
    (
        "assigned_secret",
        re.compile(
            r"(?im)^\s*(?:V12_AUDIT_HMAC_KEY|V12_BACKUP_AES_KEY|DISCORD_TOKEN|"
            r"PUSHOVER_APP_TOKEN|PUSHOVER_USER_KEY|GAMBIT_PORTAL_ADMIN_KEY)\s*=\s*"
            r"['\"]?([^'\"\s#]{16,})"
        ),
    ),
)
SKIP_SUFFIXES = {".lock", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".pyc"}
SKIP_PATH_PARTS = {".git", "node_modules", ".venv", "venv"}


def tracked_files() -> list[Path]:
    output = subprocess.check_output(["git", "ls-files", "-z"])
    return [Path(item.decode()) for item in output.split(b"\0") if item]


def entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = {char: value.count(char) for char in set(value)}
    return -sum(
        (count / len(value)) * math.log2(count / len(value))
        for count in counts.values()
    )


def main() -> int:
    failures: list[tuple[str, str, int]] = []
    for path in tracked_files():
        if path.suffix.lower() in SKIP_SUFFIXES or SKIP_PATH_PARTS.intersection(path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for name, pattern in PATTERNS:
            for match in pattern.finditer(text):
                value = match.group(1) if match.lastindex else match.group(0)
                lowered = value.lower()
                if any(
                    marker in lowered
                    for marker in ("example", "placeholder", "changeme", "environment", "<")
                ):
                    continue
                failures.append((str(path), name, match.start()))
        for match in re.finditer(
            r"(?i)(?:secret|token|private[_-]?key|api[_-]?key)\s*[:=]\s*['\"]([^'\"]{32,})['\"]",
            text,
        ):
            value = match.group(1)
            nearby = text[max(0, match.start() - 40) : match.start() + 20]
            if entropy(value) >= 4.2 and "os.getenv" not in nearby:
                failures.append(
                    (str(path), "high_entropy_secret_assignment", match.start())
                )

    if failures:
        for path, kind, offset in failures:
            print(f"SECRET_SCAN_FAIL {path} {kind} offset={offset}")
        return 2
    print("SECRET_SCAN_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

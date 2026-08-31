#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

HANDLE_RE = re.compile(r"(?<![A-Za-z0-9_])@?([A-Za-z][A-Za-z0-9_]{1,14})(?![A-Za-z0-9_])")
PATH_WORDS = ("twitter", "social", "kol", "meme", "news", "tracker", "accounts", "x_")
EXCLUDED = {
    "example", "username", "handle", "twitter", "solana", "everyone", "here", "channel",
    "pytest", "classmethod", "staticmethod", "property", "dataclass", "override", "media",
}


def values(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in {"handle", "username", "twitter", "x", "account", "screen_name"}:
                if isinstance(item, str):
                    yield item
            yield from values(item)
    elif isinstance(value, list):
        for item in value:
            yield from values(item)


def handles_from_text(text: str) -> set[str]:
    output: set[str] = set()
    for match in HANDLE_RE.finditer(text):
        handle = match.group(1).lower()
        if handle not in EXCLUDED:
            output.add(handle)
    for url_match in re.finditer(r"(?:x|twitter)\.com/([A-Za-z][A-Za-z0-9_]{1,14})", text, re.IGNORECASE):
        handle = url_match.group(1).lower()
        if handle not in EXCLUDED:
            output.add(handle)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Build V10 curated social-stream account allowlist")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, default=Path("models/e4/e4-social-accounts.txt"))
    parser.add_argument("--minimum", type=int, default=25)
    args = parser.parse_args()

    handles: set[str] = set()
    scanned: list[str] = []
    for path in args.root.rglob("*"):
        if not path.is_file() or path == args.output:
            continue
        lowered = str(path).lower()
        if not any(word in lowered for word in PATH_WORDS):
            continue
        if path.suffix.lower() not in {".txt", ".md", ".json", ".jsonl", ".csv", ".tsv", ".py", ".ts"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scanned.append(str(path))
        handles.update(handles_from_text(text))
        if path.suffix.lower() in {".json", ".jsonl"}:
            rows = text.splitlines() if path.suffix.lower() == ".jsonl" else [text]
            for row in rows:
                try:
                    payload = json.loads(row)
                except json.JSONDecodeError:
                    continue
                for item in values(payload):
                    handles.update(handles_from_text(str(item)))

    # Core sources are present even when an imported tracker file is absent.
    handles.update({"pumpdotfun", "solana", "jito_sol", "heliuslabs"})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "# Generated from existing Gambit social/KOL/news tracker assets.\n"
        "# One X handle per line. Review before synchronising paid X stream rules.\n"
        + "\n".join(sorted(handles))
        + "\n",
        encoding="utf-8",
    )
    report = {
        "output": str(args.output),
        "handles": len(handles),
        "files_scanned": len(scanned),
        "scanned": scanned,
    }
    args.output.with_suffix(".report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report), flush=True)
    return 0 if len(handles) >= args.minimum else 2


if __name__ == "__main__":
    raise SystemExit(main())

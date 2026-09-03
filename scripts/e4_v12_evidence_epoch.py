#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path
from typing import Any, Mapping

from e4_v12_forward_accumulate import FINGERPRINT_PATHS, fingerprint


def _load(path: Path) -> Mapping[str, Any] | None:
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"evidence must be a JSON object: {path}")
    return value


def _slug(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-._")
    return value or "unknown-epoch"


def _initial(epoch: str, strategy_fingerprint: str) -> dict[str, Any]:
    return {
        "version": "e4-v12-forward-evidence-v1",
        "evidence_epoch": epoch,
        "strategy_fingerprint": strategy_fingerprint,
        "fingerprint_paths": list(FINGERPRINT_PATHS),
        "batches": [],
        "gambit_positions": {},
        "same_window_e4_positions": {},
    }


def prepare_epoch(
    evidence_path: Path,
    markdown_path: Path,
    epoch_path: Path,
    archive_dir: Path,
) -> dict[str, Any]:
    epoch = epoch_path.read_text(encoding="utf-8").strip()
    if not epoch:
        raise ValueError(f"empty V12 evidence epoch: {epoch_path}")
    current_fingerprint = fingerprint()
    existing = _load(evidence_path)

    if existing is None:
        active = _initial(epoch, current_fingerprint)
        action = "INITIALISED"
    else:
        old_fingerprint = str(existing.get("strategy_fingerprint") or "")
        old_epoch = str(existing.get("evidence_epoch") or "v12-pre-role-model-pipeline")
        if old_fingerprint == current_fingerprint:
            if existing.get("evidence_epoch") not in (None, "", epoch):
                raise RuntimeError(
                    "strategy fingerprint is unchanged but evidence epoch differs: "
                    f"active={old_epoch} requested={epoch}"
                )
            active = dict(existing)
            active["evidence_epoch"] = epoch
            action = "CONTINUED"
        else:
            if old_epoch == epoch:
                raise RuntimeError(
                    "V12 strategy changed inside the same evidence epoch; bump "
                    f"{epoch_path} before collecting more evidence"
                )
            archive_dir.mkdir(parents=True, exist_ok=True)
            stem = f"{_slug(old_epoch)}-{_slug(old_fingerprint[:12] or 'no-fingerprint')}"
            archive_json = archive_dir / f"{stem}.json"
            archive_md = archive_dir / f"{stem}.md"
            archive_json.write_text(
                json.dumps(dict(existing), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            if markdown_path.exists():
                shutil.copyfile(markdown_path, archive_md)
            active = _initial(epoch, current_fingerprint)
            action = "ROLLED_OVER"

    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(active, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if action != "CONTINUED":
        markdown_path.write_text(
            "\n".join(
                [
                    "# Gambit E4 V12 forward evidence",
                    "",
                    f"**Evidence epoch:** `{epoch}`",
                    f"**Strategy fingerprint:** `{current_fingerprint}`",
                    "**Classification:** AWAITING_FIRST_FRESH_BATCH",
                    "",
                    "V12 remains the permanent version. Evidence from a different "
                    "strategy fingerprint is archived rather than mixed into this epoch.",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
    return {
        "action": action,
        "evidence_epoch": epoch,
        "strategy_fingerprint": current_fingerprint,
        "active_batches": len(active.get("batches") or []),
        "active_positions": len(active.get("gambit_positions") or {}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare an auditable Gambit E4 V12 evidence epoch")
    parser.add_argument("--evidence", default="models/e4/e4-v12-forward-evidence.json")
    parser.add_argument("--markdown", default="models/e4/e4-v12-forward-evidence.md")
    parser.add_argument("--epoch", default="models/e4/e4-v12-evidence-epoch.txt")
    parser.add_argument("--archive-dir", default="models/e4/evidence-epochs")
    args = parser.parse_args()
    result = prepare_epoch(
        Path(args.evidence),
        Path(args.markdown),
        Path(args.epoch),
        Path(args.archive_dir),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    tests = sorted((root / "tests").glob("test_*.py"))
    skipped = {
        "test_e4_000_hardening_bootstrap.py",
        "test_e4_000_v7_bootstrap.py",
    }
    rows = []
    failures = []
    started = time.monotonic()
    for path in tests:
        if path.name in skipped:
            rows.append({"file": path.name, "status": "bootstrap-only"})
            continue
        t0 = time.monotonic()
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", str(path)],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        row = {
            "file": path.name,
            "status": "pass" if completed.returncode == 0 else "fail",
            "returncode": completed.returncode,
            "seconds": round(time.monotonic() - t0, 3),
        }
        rows.append(row)
        if completed.returncode != 0:
            failures.append(
                {
                    **row,
                    "stdout_tail": completed.stdout[-5000:],
                    "stderr_tail": completed.stderr[-5000:],
                }
            )
            print(json.dumps(failures[-1], indent=2), flush=True)
    report = {
        "version": "isolated-regression-v1",
        "test_modules": len(tests),
        "executed_modules": sum(row["status"] != "bootstrap-only" for row in rows),
        "bootstrap_only_modules": sorted(skipped),
        "failed_modules": len(failures),
        "seconds": round(time.monotonic() - started, 3),
        "passed": not failures,
        "results": rows,
        "failures": failures,
    }
    encoded = json.dumps(report, indent=2, sort_keys=True)
    print(encoded)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Each module runs in a fresh interpreter. Several historical E4 layers install
# process-wide policy/transport patches at import time, so a single unittest
# process makes the result depend on module order rather than the tested V12
# contract.
AUTHORITY_MODULES = (
    "tests.test_v12_true_latency_replay",
    "tests.test_v12_tight_output",
    "tests.test_v12_strict_output_deferred",
    "tests.test_v12_independent_exit_replay",
    "tests.test_v12_transport",
    "tests.test_v12_transport_epoch",
    "tests.test_e4_final",
    "tests.test_e4_hardening_v4",
    "tests.test_e4_hardening_v5",
    "tests.test_e4_production",
    "tests.test_e4_stress_hardening",
    "tests.test_e4_v10_direct_ca_social",
    "tests.test_e4_v10_learning_finalizer",
    "tests.test_e4_v10_production_wiring",
    "tests.test_e4_v12_sub10ms_repairs",
    "tests.test_e4_v12_true_latency_replay",
    "tests.test_e4_v12_sub10ms_transport_final",
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run current V12 authority tests in isolated interpreters"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    environment = os.environ.copy()
    environment.setdefault("PYTHONHASHSEED", "0")
    environment.setdefault("E4_PIPELINES_BACKGROUND", "false")

    started = time.perf_counter()
    rows: list[dict[str, Any]] = []
    for module in AUTHORITY_MODULES:
        module_started = time.perf_counter()
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", "-v", module],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            env=environment,
        )
        elapsed = time.perf_counter() - module_started
        row = {
            "module": module,
            "returncode": completed.returncode,
            "passed": completed.returncode == 0,
            "elapsed_seconds": round(elapsed, 6),
            "output": completed.stdout,
        }
        rows.append(row)
        print(f"\n===== {module} =====", flush=True)
        print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n", flush=True)

    failed = [row for row in rows if not row["passed"]]
    report = {
        "version": "e4-v12-isolated-authority-regressions-v2",
        "modules": len(rows),
        "passed_modules": len(rows) - len(failed),
        "failed_modules": len(failed),
        "failed": [row["module"] for row in failed],
        "pythonhashseed": environment["PYTHONHASHSEED"],
        "pipelines_background": environment["E4_PIPELINES_BACKGROUND"],
        "elapsed_seconds": round(time.perf_counter() - started, 6),
        "results": rows,
    }
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "results"}, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence


def run_step(name: str, command: Sequence[str], timeout: int, cwd: Path | None = None) -> dict[str, Any]:
    started = time.time()
    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        code = result.returncode
        stdout = result.stdout
        stderr = result.stderr
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        code = 124
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        timed_out = True
    elapsed = time.time() - started
    combined = (stdout + "\n" + stderr).strip().splitlines()
    return {
        "name": name,
        "command": " ".join(shlex.quote(item) for item in command),
        "returncode": code,
        "passed": code == 0,
        "timed_out": timed_out,
        "elapsed_seconds": round(elapsed, 3),
        "tail": "\n".join(combined[-120:]),
    }


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Self-reporting E4 V10 certification")
    parser.add_argument("--output", type=Path, default=Path("reports/e4-v10-certification/latest.json"))
    parser.add_argument("--skip-docker", action="store_true")
    args = parser.parse_args()
    output = args.output
    artifact_dir = output.parent / "run-artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    python = sys.executable
    steps: list[dict[str, Any]] = []
    steps.append(
        run_step(
            "compile",
            [
                python,
                "-m",
                "compileall",
                "-q",
                "src/memecoin_bot/e4_pipelines_v10.py",
                "src/memecoin_bot/e4_hardening_v10.py",
                "src/memecoin_bot/e4_fast_execution_v10.py",
                "src/memecoin_bot/e4_runtime_services_v10.py",
                "src/memecoin_bot/e4_exec_v10.py",
                "scripts/e4_social_stream_v10.py",
                "scripts/e4_creator_learner_v10.py",
                "scripts/e4_v10_supervisor.py",
                "scripts/e4_v10_live_holdout.py",
                "scripts/e4_v10_pipeline_stress.py",
            ],
            120,
        )
    )
    steps.append(
        run_step(
            "unit_and_regression",
            [python, "-m", "unittest", "discover", "-s", "tests", "-p", "test_e4*.py", "-v"],
            300,
        )
    )

    stress_specs = [
        ("stress_single", 100_000, 1, 20_000),
        ("stress_concurrent", 200_000, 16, 30_000),
        ("stress_repeat_1", 50_000, 8, 10_000),
        ("stress_repeat_2", 50_000, 8, 10_000),
        ("stress_repeat_3", 50_000, 8, 10_000),
    ]
    for name, iterations, workers, narratives in stress_specs:
        target = artifact_dir / f"{name}.json"
        steps.append(
            run_step(
                name,
                [
                    python,
                    "scripts/e4_v10_pipeline_stress.py",
                    "--iterations",
                    str(iterations),
                    "--workers",
                    str(workers),
                    "--narrative-iterations",
                    str(narratives),
                    "--output",
                    str(target),
                ],
                480,
            )
        )

    builder_dir = Path("tools/e4-builder")
    steps.append(run_step("builder_syntax", ["node", "--check", "daemon-v2.mjs"], 60, builder_dir))
    for index in range(1, 6):
        steps.append(
            run_step(
                f"builder_self_test_{index}",
                ["node", "daemon-v2.mjs", "--self-test"],
                120,
                builder_dir,
            )
        )

    social_journal = Path("var/e4/social-stream.jsonl")
    if social_journal.exists():
        social_journal.unlink()
    social_payload = json.dumps(
        {
            "source": "x-test",
            "source_account": "authority",
            "text": "Purple Monkey Dishwasher is coming",
            "authority": 0.99,
            "engagement_velocity": 0.9,
        }
    ) + "\n"
    social_started = time.time()
    try:
        social = subprocess.run(
            [python, "scripts/e4_social_stream_v10.py", "--stdin-jsonl"],
            input=social_payload,
            text=True,
            capture_output=True,
            timeout=30,
            env=os.environ.copy(),
        )
        social_code = social.returncode if social_journal.exists() and social_journal.stat().st_size > 0 else 1
        social_tail = (social.stdout + "\n" + social.stderr).strip()
    except subprocess.TimeoutExpired as exc:
        social_code = 124
        social_tail = str(exc)
    steps.append(
        {
            "name": "social_journal_e2e",
            "command": f"{python} scripts/e4_social_stream_v10.py --stdin-jsonl",
            "returncode": social_code,
            "passed": social_code == 0,
            "timed_out": social_code == 124,
            "elapsed_seconds": round(time.time() - social_started, 3),
            "tail": social_tail[-8_000:],
        }
    )

    steps.append(
        run_step(
            "runtime_services_lifecycle",
            [
                python,
                "-c",
                (
                    "import os,time;os.environ['E4_V10_SERVICES_ENABLED']='true';"
                    "os.environ['E4_LAUNCH_INTENT_SECRET']='ci-only-secret';"
                    "from memecoin_bot.e4_runtime_services_v10 import start_runtime_services,stop_runtime_services;"
                    "start_runtime_services();time.sleep(.1);stop_runtime_services();print('PASS')"
                ),
            ],
            60,
        )
    )

    if not args.skip_docker:
        steps.append(
            run_step(
                "production_container",
                ["docker", "build", "-f", "Dockerfile.e4-exec", "-t", "gambit-jr-e4:v10-cert", "."],
                900,
            )
        )

    stress_reports = {
        path.stem: load_json(path)
        for path in sorted(artifact_dir.glob("stress_*.json"))
    }
    passed = all(step["passed"] for step in steps)
    report = {
        "version": "e4-v10-certification-v1",
        "generated_at_unix": time.time(),
        "python": sys.version,
        "passed": passed,
        "steps": steps,
        "stress_reports": stress_reports,
        "gates": {
            "all_steps_pass": passed,
            "decision_p99_under_36ms": all(
                isinstance(row, dict)
                and row.get("decision_p99_ns") is not None
                and row["decision_p99_ns"] <= 36_000_000
                and row.get("decision_correctness_failures") == 0
                for row in stress_reports.values()
            ) if stress_reports else False,
            "narrative_p99_under_36ms": all(
                isinstance(row, dict)
                and isinstance(row.get("narrative"), dict)
                and row["narrative"].get("p99_ns") is not None
                and row["narrative"]["p99_ns"] <= 36_000_000
                for row in stress_reports.values()
            ) if stress_reports else False,
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
    temporary.replace(output)
    markdown = output.with_suffix(".md")
    markdown.write_text(
        "# E4 V10 certification\n\n"
        f"Overall: **{'PASS' if passed else 'FAIL'}**\n\n"
        + "\n".join(
            f"- {'✅' if step['passed'] else '❌'} **{step['name']}** — {step['elapsed_seconds']}s, code {step['returncode']}"
            for step in steps
        )
        + "\n\n## Gates\n\n"
        + "\n".join(f"- {key}: **{value}**" for key, value in report["gates"].items())
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"passed": passed, "output": str(output), "failed": [s["name"] for s in steps if not s["passed"]]}))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())

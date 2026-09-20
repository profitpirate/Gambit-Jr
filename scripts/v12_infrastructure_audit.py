"""Static + structural audit for the active V12 Pre-Armed infrastructure."""
from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path
from typing import Any

ACTIVE_PYTHON = (
    "scripts/e4_v12_fast_prearmed_selector.py",
    "scripts/e4_v12_axiom_paper_live.py",
    "scripts/v12_prearmed_shadow_suite.py",
    "scripts/v12_prearmed_guardrail_policy.py",
    "scripts/v12_creator_apprentice.py",
    "scripts/v12_creator_history_enrich.py",
    "scripts/v12_e4_recent_compare.py",
    "scripts/e4_300_launch_holdout.py",
    "scripts/e4_300_launch_holdout_v12.py",
    "scripts/e4_300_launch_holdout_v12_sub10ms.py",
    "src/memecoin_bot/e4_role_model_v12.py",
    "src/memecoin_bot/e4_direct_copy_v12.py",
    "src/memecoin_bot/e4_sub10ms_repairs_v12.py",
    "src/memecoin_bot/e4_sub10ms_runtime_final_v12.py",
    "src/memecoin_bot/e4_sub10ms_transport_final_v12.py",
)
ACTIVE_JS = (
    "tools/e4-builder/race-proxy-v3.mjs",
    "tools/e4-builder/strict-race-proxy-v12.mjs",
    "tools/e4-builder/fast-preload-v4.mjs",
)
ACTIVE_WORKFLOWS = (
    ".github/workflows/v12-pre-armed-100-trade-causal.yml",
    ".github/workflows/v12-pre-armed-50-recertify.yml",
    ".github/workflows/v12-broad-discovery-shadow.yml",
    ".github/workflows/v12-broad-discovery-ci.yml",
    ".github/workflows/v12-creator-history-backfill.yml",
    ".github/workflows/v12-e4-48h-comparison.yml",
)
FORBIDDEN_JS = re.compile(r"\b(?:TODO|FIXME)\b|not\s+implemented", re.I)


def pass_only(node: ast.AST) -> bool:
    body = getattr(node, "body", None)
    if not isinstance(body, list) or not body:
        return True
    real = [
        item for item in body
        if not (
            isinstance(item, ast.Expr)
            and isinstance(item.value, ast.Constant)
            and isinstance(item.value.value, str)
        )
    ]
    if not real:
        return False
    if all(isinstance(item, ast.Pass) for item in real):
        return True
    if len(real) == 1 and isinstance(real[0], ast.Raise):
        exc = real[0].exc
        if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
            return exc.func.id == "NotImplementedError"
    return False


def python_audit(path: Path) -> dict[str, Any]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    funcs = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    stubs = [
        getattr(node, "name", "<unknown>")
        for node in [*funcs, *classes]
        if pass_only(node)
    ]
    return {
        "path": str(path),
        "lines": len(source.splitlines()),
        "functions": len(funcs),
        "classes": len(classes),
        "stub_bodies": stubs,
        "not_implemented_mentions": source.count("NotImplementedError"),
        "todo_mentions": len(re.findall(r"\bTODO\b|\bFIXME\b", source)),
    }


def js_audit(path: Path) -> dict[str, Any]:
    source = path.read_text(encoding="utf-8")
    return {
        "path": str(path),
        "lines": len(source.splitlines()),
        "forbidden_shell_markers": FORBIDDEN_JS.findall(source),
        "exports": len(re.findall(r"\bexport\b", source)),
        "async_functions": len(re.findall(r"\basync\s+(?:function|\()", source)),
    }


def workflow_audit(path: Path) -> dict[str, Any]:
    source = path.read_text(encoding="utf-8")
    run_steps = len(re.findall(r"^\s*-\s+(?:name:\s*)?.*$", source, flags=re.M))
    return {
        "path": str(path),
        "lines": len(source.splitlines()),
        "has_checkout": "actions/checkout@" in source,
        "has_tests": "pytest" in source or "unittest" in source,
        "has_frozen_model_guard": (
            "frozen-model" in source.lower()
            or "frozen model" in source.lower()
            or "model_sha" in source.lower()
            or "69df86eaf386fd37d699928a95d25aaeb2053e743b59c9e687c8a7c49d14f977" in source
        ),
        "has_artifact_persistence": "upload-artifact" in source or "git add" in source,
        "step_like_lines": run_steps,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, default=Path("."))
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    root = args.root

    py = [python_audit(root / value) for value in ACTIVE_PYTHON]
    js = [js_audit(root / value) for value in ACTIVE_JS]
    workflows = [workflow_audit(root / value) for value in ACTIVE_WORKFLOWS]
    missing = [
        value for value in [*ACTIVE_PYTHON, *ACTIVE_JS, *ACTIVE_WORKFLOWS]
        if not (root / value).exists()
    ]

    failures = []
    for row in py:
        if row["stub_bodies"]:
            failures.append(f"{row['path']}: stub bodies {row['stub_bodies']}")
        if row["not_implemented_mentions"]:
            failures.append(f"{row['path']}: NotImplementedError present")
    for row in js:
        if row["forbidden_shell_markers"]:
            failures.append(f"{row['path']}: shell markers present")
    for row in workflows:
        if not row["has_checkout"]:
            failures.append(f"{row['path']}: no checkout")
        if row["step_like_lines"] < 3:
            failures.append(f"{row['path']}: suspiciously small workflow")
    if missing:
        failures.append("missing active components: " + ",".join(missing))

    report = {
        "version": "v12-infrastructure-audit-v1",
        "active_python": py,
        "active_javascript": js,
        "active_workflows": workflows,
        "missing": missing,
        "failures": failures,
        "status": "PASS" if not failures else "FAIL",
        "shell_free": not failures,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": report["status"], "failures": failures}, indent=2))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())

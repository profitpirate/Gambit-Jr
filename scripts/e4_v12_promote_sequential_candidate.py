#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

RESEARCH = "origin/codex/e4-v12-matched-controls"
AUTHORITATIVE = "origin/codex/e4-v12-selection-reconstruction"
CANDIDATE = "codex/e4-v12-causal-entry-candidate"


def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(args), flush=True)
    return subprocess.run(args, check=True, text=True, env=env)


def read_json(path: str) -> dict:
    raw = subprocess.check_output(["git", "show", f"{RESEARCH}:{path}"], text=True)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def show(source: str, destination: str) -> None:
    raw = subprocess.check_output(["git", "show", f"{RESEARCH}:{source}"])
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def require_selected() -> tuple[dict, dict, dict, dict]:
    selector = read_json("docs/research/e4-v12-conclusive-thesis-selector-v2.json")
    if selector.get("status") != "SCREENING_CONFIRMED":
        raise RuntimeError(f"selector not confirmed: {selector.get('status')}")
    if selector.get("selected_thesis") != "SEQUENTIAL_HAZARD":
        raise RuntimeError(f"selector chose {selector.get('selected_thesis')}, not sequential")
    report = read_json("docs/research/e4-v12-sequential-hazard-report.json")
    model = read_json("models/e4/research/e4-v12-sequential-hazard-model.json")
    state = read_json("models/e4/research/e4-v12-causal-runtime-state.json")
    verdict = read_json("docs/research/e4-v12-sequential-global-verdict.json")
    if report.get("status") != "LIVE_HOLDOUT_CONFIRMED" or report.get("safe_to_implement") is not True:
        raise RuntimeError(f"sequential research rejected: {report.get('status')}")
    if model.get("status") != "LIVE_HOLDOUT_CONFIRMED":
        raise RuntimeError(f"sequential model rejected: {model.get('status')}")
    if model.get("version") != "e4-v12-sequential-hazard-v1":
        raise RuntimeError(f"unexpected sequential model version: {model.get('version')}")
    if (model.get("model") or {}).get("kind") not in {"tree_ensemble", "logistic"}:
        raise RuntimeError("sequential model is not runtime-exportable")
    if state.get("version") != "e4-v12-causal-runtime-state-v1":
        raise RuntimeError(f"unexpected state version: {state.get('version')}")
    if verdict.get("status") != "GLOBAL_SCREENING_CONFIRMED":
        raise RuntimeError(f"sequential global screening rejected: {verdict.get('status')}")
    print(json.dumps({
        "selected_thesis": selector.get("selected_thesis"),
        "research_status": report.get("status"),
        "spec": report.get("spec"),
        "gate": report.get("gate"),
        "holdout": report.get("live_holdout"),
        "global_verdict": verdict,
        "state_coverage": state.get("coverage"),
    }, indent=2, sort_keys=True), flush=True)
    return selector, report, model, state


def patch_entrypoint(copy_hash: str) -> None:
    path = Path("src/memecoin_bot/e4_exec/__main__.py")
    text = path.read_text(encoding="utf-8")
    builder = (
        'os.environ.setdefault(\n'
        '    "E4_BUILDER_COMMAND",\n'
        '    "node tools/e4-builder/race-proxy-v3.mjs",\n'
        ')\n'
    )
    if "E4_V12_SEQUENTIAL_ENTRY_ENABLED" not in text:
        if builder not in text:
            raise RuntimeError("builder environment anchor missing")
        text = text.replace(
            builder,
            builder + 'os.environ.setdefault("E4_V12_SEQUENTIAL_ENTRY_ENABLED", "true")\n',
            1,
        )
    direct = "from memecoin_bot import e4_direct_copy_v12  # noqa: E402 - forced recognized-E4 execution\n"
    copy_import = "from memecoin_bot import e4_copy_fidelity_v12  # noqa: E402 - exact E4 exits + warm route fanout\n"
    sequential_import = "from memecoin_bot import e4_sequential_hazard_v12  # noqa: E402 - causal sequential pre-intent authority\n"
    if "import e4_copy_fidelity_v12" not in text:
        if direct not in text:
            raise RuntimeError("direct-copy import anchor missing")
        text = text.replace(direct, direct + copy_import + sequential_import, 1)
    elif "import e4_sequential_hazard_v12" not in text:
        text = text.replace(copy_import, copy_import + sequential_import, 1)
    if "E4_V12_COPY_FIDELITY_POLICY_SHA256" not in text:
        marker = "E4_V12_DIRECT_COPY_POLICY_SHA256 = "
        index = text.index("\n", text.index(marker)) + 1
        text = text[:index] + f'E4_V12_COPY_FIDELITY_POLICY_SHA256 = "{copy_hash}"\n' + text[index:]
        assertion = "e4_direct_copy_v12.assert_policy_fingerprint(E4_V12_DIRECT_COPY_POLICY_SHA256)\n"
        if assertion not in text:
            raise RuntimeError("direct-copy assertion anchor missing")
        text = text.replace(
            assertion,
            assertion + "e4_copy_fidelity_v12.assert_policy_fingerprint(E4_V12_COPY_FIDELITY_POLICY_SHA256)\n",
            1,
        )
    path.write_text(text, encoding="utf-8")


def patch_holdout(copy_hash: str) -> None:
    path = Path("scripts/e4_300_launch_holdout_v12.py")
    text = path.read_text(encoding="utf-8")
    if "\nimport os\n" not in text:
        text = text.replace("import importlib.util\n", "import importlib.util\nimport os\n", 1)
    path_anchor = "from pathlib import Path\n\n"
    if "E4_V12_SEQUENTIAL_ENTRY_ENABLED" not in text:
        if path_anchor not in text:
            raise RuntimeError("holdout Path anchor missing")
        text = text.replace(
            path_anchor,
            path_anchor + 'os.environ.setdefault("E4_V12_SEQUENTIAL_ENTRY_ENABLED", "true")\n\n',
            1,
        )
    direct = "from memecoin_bot import e4_direct_copy_v12 as direct_copy\n"
    copy_import = "from memecoin_bot import e4_copy_fidelity_v12 as copy_fidelity\n"
    sequential_import = "from memecoin_bot import e4_sequential_hazard_v12 as sequential_hazard  # noqa: F401\n"
    if "import e4_copy_fidelity_v12" not in text:
        if direct not in text:
            raise RuntimeError("holdout direct-copy import anchor missing")
        text = text.replace(direct, direct + copy_import + sequential_import, 1)
    elif "import e4_sequential_hazard_v12" not in text:
        text = text.replace(copy_import, copy_import + sequential_import, 1)
    if "E4_V12_COPY_FIDELITY_POLICY_SHA256" not in text:
        marker = "E4_V12_DIRECT_COPY_POLICY_SHA256 = "
        index = text.index("\n", text.index(marker)) + 1
        text = text[:index] + f'E4_V12_COPY_FIDELITY_POLICY_SHA256 = "{copy_hash}"\n' + text[index:]
        assertion = "direct_copy.assert_policy_fingerprint(E4_V12_DIRECT_COPY_POLICY_SHA256)\n"
        if assertion not in text:
            raise RuntimeError("holdout direct-copy assertion anchor missing")
        text = text.replace(
            assertion,
            assertion + "copy_fidelity.assert_policy_fingerprint(E4_V12_COPY_FIDELITY_POLICY_SHA256)\n",
            1,
        )
    path.write_text(text, encoding="utf-8")


def patch_fingerprint() -> None:
    path = Path("scripts/e4_v12_forward_accumulate.py")
    text = path.read_text(encoding="utf-8")
    additions = [
        "src/memecoin_bot/e4_copy_fidelity_v12.py",
        "src/memecoin_bot/e4_sequential_hazard_v12.py",
        "models/e4/e4-v12-sequential-entry-model.json",
        "models/e4/e4-v12-sequential-entry-state.json",
    ]
    start = text.index("FINGERPRINT_PATHS")
    assignment = text.index("=", start)
    open_index = text.index("(", assignment)
    close_index = text.index(")", open_index)
    block = text[open_index + 1 : close_index]
    missing = [value for value in additions if value not in block]
    if missing:
        text = text[:close_index] + "".join(f'    "{value}",\n' for value in missing) + text[close_index:]
    path.write_text(text, encoding="utf-8")


def patch_environment() -> None:
    path = Path(".env.e4.example")
    text = path.read_text(encoding="utf-8")
    if "E4_V12_SEQUENTIAL_ENTRY_ENABLED" not in text:
        text += """
# V12 sequential pre-intent hazard. Runtime inputs are strictly on-chain launch
# events plus creator/first-buyer E4 intent history available before entry.
E4_V12_SEQUENTIAL_ENTRY_ENABLED=true
E4_V12_SEQUENTIAL_MODEL_PATH=models/e4/e4-v12-sequential-entry-model.json
E4_V12_SEQUENTIAL_STATE_PATH=models/e4/e4-v12-sequential-entry-state.json
E4_V12_SEQUENTIAL_ENTRY_FRACTION=0.0185
E4_V12_SEQUENTIAL_CONFIRMATION_MS=1500
"""
    if "E4_ALLENHARK_RELAY_URL" not in text:
        text += """
# Optional low-latency relay. Existing V12 routes remain active when omitted.
E4_ALLENHARK_RELAY_URL=
E4_ALLENHARK_API_KEY=
E4_ALLENHARK_KEEPALIVE_URL=
"""
    path.write_text(text, encoding="utf-8")


def main() -> int:
    run(
        "git",
        "fetch",
        "origin",
        "codex/e4-v12-selection-reconstruction",
        "codex/e4-v12-matched-controls",
        "codex/e4-v12-causal-entry-candidate",
    )
    require_selected()
    run("git", "checkout", "-B", CANDIDATE, AUTHORITATIVE)

    copies = {
        "src/memecoin_bot/e4_copy_fidelity_v12.py": "src/memecoin_bot/e4_copy_fidelity_v12.py",
        "src/memecoin_bot/e4_sequential_hazard_v12.py": "src/memecoin_bot/e4_sequential_hazard_v12.py",
        "tests/test_v12_copy_fidelity.py": "tests/test_v12_copy_fidelity.py",
        "tests/test_v12_sequential_hazard_runtime.py": "tests/test_v12_sequential_hazard_runtime.py",
        "scripts/e4_v12_live_sequential_validate.py": "scripts/e4_v12_live_sequential_validate.py",
        "scripts/e4_v12_causal_choice_pnl_replay.py": "scripts/e4_v12_causal_choice_pnl_replay.py",
        "models/e4/research/e4-v12-sequential-hazard-model.json": "models/e4/e4-v12-sequential-entry-model.json",
        "models/e4/research/e4-v12-causal-runtime-state.json": "models/e4/e4-v12-sequential-entry-state.json",
        "research/candidate-workflows/e4-v12-sequential-entry-candidate-ci.yml": ".github/workflows/e4-v12-sequential-entry-candidate-ci.yml",
        "research/candidate-workflows/e4-v12-sequential-entry-fresh-live.yml": ".github/workflows/e4-v12-sequential-entry-fresh-live.yml",
    }
    for source, destination in copies.items():
        show(source, destination)

    copy_hash = hashlib.sha256(Path("src/memecoin_bot/e4_copy_fidelity_v12.py").read_bytes()).hexdigest()
    patch_entrypoint(copy_hash)
    patch_holdout(copy_hash)
    patch_fingerprint()
    patch_environment()
    Path("models/e4/e4-v12-sequential-entry-trigger.txt").write_text(
        "v12-sequential-preintent-hazard-001\n", encoding="utf-8"
    )
    Path("models/e4/e4-v12-evidence-epoch.txt").write_text(
        "v12-sequential-preintent-hazard-2026-09-04\n", encoding="utf-8"
    )

    run("python", "-m", "unittest", "tests.test_v12_copy_fidelity", "-v")
    run("python", "-m", "unittest", "tests.test_v12_sequential_hazard_runtime", "-v")
    run(
        "python",
        "-m",
        "unittest",
        "tests.test_v12_selection",
        "tests.test_v12_role_model_pipeline",
        "-v",
    )
    run(
        "python",
        "-m",
        "compileall",
        "-q",
        "src/memecoin_bot/e4_copy_fidelity_v12.py",
        "src/memecoin_bot/e4_sequential_hazard_v12.py",
        "scripts/e4_v12_live_sequential_validate.py",
        "scripts/e4_v12_causal_choice_pnl_replay.py",
    )
    environment = dict(os.environ)
    environment["E4_V12_SEQUENTIAL_ENTRY_ENABLED"] = "true"
    environment["E4_PIPELINES_BACKGROUND"] = "false"
    subprocess.run(
        [
            "python",
            "-c",
            "import memecoin_bot.e4_exec.__main__; "
            "import memecoin_bot.e4_sequential_hazard_v12 as m; assert m.RUNTIME.active",
        ],
        check=True,
        env=environment,
    )

    paths = list(copies.values()) + [
        "src/memecoin_bot/e4_exec/__main__.py",
        "scripts/e4_300_launch_holdout_v12.py",
        "scripts/e4_v12_forward_accumulate.py",
        "models/e4/e4-v12-sequential-entry-trigger.txt",
        "models/e4/e4-v12-evidence-epoch.txt",
        ".env.e4.example",
    ]
    run("git", "add", *paths)
    run("git", "config", "user.name", "gambit-v12-sequential-entry")
    run("git", "config", "user.email", "actions@users.noreply.github.com")
    run(
        "git",
        "commit",
        "-m",
        "feat(e4-v12): sequential pre-intent hazard candidate",
    )
    run(
        "git",
        "push",
        "--force-with-lease",
        "origin",
        f"HEAD:{CANDIDATE}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

REPOSITORY = "profitpirate/Gambit-Jr"
RESEARCH = "origin/codex/e4-v12-matched-controls"
AUTHORITATIVE = "origin/codex/e4-v12-selection-reconstruction"
CANDIDATE = "codex/e4-v12-causal-entry-candidate"


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(args), flush=True)
    return subprocess.run(args, check=check, text=True)


def show(source: str, destination: str) -> None:
    value = subprocess.check_output(
        ["git", "show", f"{RESEARCH}:{source}"],
        text=False,
    )
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def read_json_from_ref(path: str) -> dict:
    raw = subprocess.check_output(["git", "show", f"{RESEARCH}:{path}"], text=True)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def require_confirmed() -> tuple[dict, dict, dict]:
    report = read_json_from_ref("docs/research/e4-v12-runtime-choice-v3-report.json")
    model = read_json_from_ref("models/e4/research/e4-v12-runtime-choice-v3-model.json")
    state = read_json_from_ref("models/e4/research/e4-v12-causal-runtime-state.json")
    if report.get("status") != "LIVE_HOLDOUT_CONFIRMED":
        raise RuntimeError(f"runtime choice v3 not confirmed: {report.get('status')}")
    if report.get("safe_to_implement") is not True:
        raise RuntimeError("runtime choice v3 is not marked safe_to_implement")
    if model.get("status") != "LIVE_HOLDOUT_CONFIRMED":
        raise RuntimeError("model status is not confirmed")
    if model.get("version") != "e4-v12-conditional-choice-ranker-v1":
        raise RuntimeError(f"unsupported model version {model.get('version')}")
    if state.get("version") != "e4-v12-causal-runtime-state-v1":
        raise RuntimeError(f"unsupported state version {state.get('version')}")
    features = set((model.get("ranker") or {}).get("features") or [])
    forbidden = {
        "status_present",
        "fresh_status_1s",
        "fresh_status_5s",
        "fresh_status_30s",
        "fresh_status_120s",
        "tweet_age_log",
        "prior_handle_log",
        "website_present",
        "visible_competitors_log",
    }
    leaked = features & forbidden
    if leaked:
        raise RuntimeError(f"runtime-infeasible features present: {sorted(leaked)}")
    causality = report.get("causality") or {}
    if not str(causality.get("target") or "").startswith("state immediately before"):
        raise RuntimeError("positive target is not pre-intent")
    if not str(causality.get("controls") or "").startswith("launches created within"):
        raise RuntimeError("controls are not exact-time launch alternatives")
    print(
        json.dumps(
            {
                "status": report.get("status"),
                "features": len(features),
                "validation": report.get("validation"),
                "walk_forward": report.get("walk_forward"),
                "live_holdout": report.get("live_holdout"),
                "state_coverage": state.get("coverage"),
            },
            indent=2,
            sort_keys=True,
        ),
        flush=True,
    )
    return report, model, state


def patch_entrypoint(copy_hash: str) -> None:
    path = Path("src/memecoin_bot/e4_exec/__main__.py")
    text = path.read_text(encoding="utf-8")
    builder = (
        'os.environ.setdefault(\n'
        '    "E4_BUILDER_COMMAND",\n'
        '    "node tools/e4-builder/race-proxy-v3.mjs",\n'
        ')\n'
    )
    if "E4_V12_CAUSAL_ENTRY_ENABLED" not in text:
        if builder not in text:
            raise RuntimeError("builder environment anchor missing")
        text = text.replace(
            builder,
            builder + 'os.environ.setdefault("E4_V12_CAUSAL_ENTRY_ENABLED", "true")\n',
            1,
        )
    direct = (
        "from memecoin_bot import e4_direct_copy_v12  "
        "# noqa: E402 - forced recognized-E4 execution\n"
    )
    copy_import = (
        "from memecoin_bot import e4_copy_fidelity_v12  "
        "# noqa: E402 - exact E4 exits + warm route fanout\n"
    )
    causal_import = (
        "from memecoin_bot import e4_causal_entry_v12  "
        "# noqa: E402 - causal pre-impact choice authority\n"
    )
    if "import e4_copy_fidelity_v12" not in text:
        if direct not in text:
            raise RuntimeError("direct-copy import anchor missing")
        text = text.replace(direct, direct + copy_import + causal_import, 1)
    elif "import e4_causal_entry_v12" not in text:
        text = text.replace(copy_import, copy_import + causal_import, 1)
    if "E4_V12_COPY_FIDELITY_POLICY_SHA256" not in text:
        marker = "E4_V12_DIRECT_COPY_POLICY_SHA256 = "
        index = text.index("\n", text.index(marker)) + 1
        text = (
            text[:index]
            + f'E4_V12_COPY_FIDELITY_POLICY_SHA256 = "{copy_hash}"\n'
            + text[index:]
        )
        assertion = (
            "e4_direct_copy_v12.assert_policy_fingerprint("
            "E4_V12_DIRECT_COPY_POLICY_SHA256)\n"
        )
        if assertion not in text:
            raise RuntimeError("direct-copy assertion anchor missing")
        text = text.replace(
            assertion,
            assertion
            + "e4_copy_fidelity_v12.assert_policy_fingerprint("
            "E4_V12_COPY_FIDELITY_POLICY_SHA256)\n",
            1,
        )
    path.write_text(text, encoding="utf-8")


def patch_holdout(copy_hash: str) -> None:
    path = Path("scripts/e4_300_launch_holdout_v12.py")
    text = path.read_text(encoding="utf-8")
    if "\nimport os\n" not in text:
        text = text.replace("import importlib.util\n", "import importlib.util\nimport os\n", 1)
    marker = "from pathlib import Path\n\n"
    if "E4_V12_CAUSAL_ENTRY_ENABLED" not in text:
        if marker not in text:
            raise RuntimeError("holdout Path anchor missing")
        text = text.replace(
            marker,
            marker + 'os.environ.setdefault("E4_V12_CAUSAL_ENTRY_ENABLED", "true")\n\n',
            1,
        )
    direct = "from memecoin_bot import e4_direct_copy_v12 as direct_copy\n"
    copy_import = "from memecoin_bot import e4_copy_fidelity_v12 as copy_fidelity\n"
    causal_import = (
        "from memecoin_bot import e4_causal_entry_v12 as causal_entry  # noqa: F401\n"
    )
    if "import e4_copy_fidelity_v12" not in text:
        if direct not in text:
            raise RuntimeError("holdout direct-copy anchor missing")
        text = text.replace(direct, direct + copy_import + causal_import, 1)
    elif "import e4_causal_entry_v12" not in text:
        text = text.replace(copy_import, copy_import + causal_import, 1)
    if "E4_V12_COPY_FIDELITY_POLICY_SHA256" not in text:
        marker = "E4_V12_DIRECT_COPY_POLICY_SHA256 = "
        index = text.index("\n", text.index(marker)) + 1
        text = (
            text[:index]
            + f'E4_V12_COPY_FIDELITY_POLICY_SHA256 = "{copy_hash}"\n'
            + text[index:]
        )
        assertion = (
            "direct_copy.assert_policy_fingerprint("
            "E4_V12_DIRECT_COPY_POLICY_SHA256)\n"
        )
        if assertion not in text:
            raise RuntimeError("holdout direct-copy assertion anchor missing")
        text = text.replace(
            assertion,
            assertion
            + "copy_fidelity.assert_policy_fingerprint("
            "E4_V12_COPY_FIDELITY_POLICY_SHA256)\n",
            1,
        )
    path.write_text(text, encoding="utf-8")


def patch_fingerprint() -> None:
    path = Path("scripts/e4_v12_forward_accumulate.py")
    text = path.read_text(encoding="utf-8")
    additions = [
        "src/memecoin_bot/e4_copy_fidelity_v12.py",
        "src/memecoin_bot/e4_causal_entry_v12.py",
        "models/e4/e4-v12-causal-entry-model.json",
        "models/e4/e4-v12-causal-entry-state.json",
    ]
    start = text.index("FINGERPRINT_PATHS")
    assignment = text.index("=", start)
    open_index = text.index("(", assignment)
    close_index = text.index(")", open_index)
    block = text[open_index + 1 : close_index]
    missing = [value for value in additions if value not in block]
    if missing:
        text = (
            text[:close_index]
            + "".join(f'    "{value}",\n' for value in missing)
            + text[close_index:]
        )
    path.write_text(text, encoding="utf-8")


def patch_environment() -> None:
    path = Path(".env.e4.example")
    text = path.read_text(encoding="utf-8")
    if "E4_V12_CAUSAL_ENTRY_ENABLED" not in text:
        text += """
# V12 causal pre-impact choice authority. Runtime inputs are strictly on-chain
# launch state plus prior E4 creator/first-buyer topology.
E4_V12_CAUSAL_ENTRY_ENABLED=true
E4_V12_CAUSAL_MODEL_PATH=models/e4/e4-v12-causal-entry-model.json
E4_V12_CAUSAL_STATE_PATH=models/e4/e4-v12-causal-entry-state.json
E4_V12_CAUSAL_ENTRY_FRACTION=0.0185
E4_V12_CAUSAL_CONFIRMATION_MS=1500
"""
    if "E4_ALLENHARK_RELAY_URL" not in text:
        text += """
# Optional low-latency relay. Standard V12 routes remain active when omitted.
E4_ALLENHARK_RELAY_URL=
E4_ALLENHARK_API_KEY=
E4_ALLENHARK_KEEPALIVE_URL=
"""
    path.write_text(text, encoding="utf-8")


def main() -> int:
    run("git", "fetch", "origin", "codex/e4-v12-selection-reconstruction", "codex/e4-v12-matched-controls")
    require_confirmed()
    run("git", "checkout", "-B", CANDIDATE, AUTHORITATIVE)

    copies = {
        "src/memecoin_bot/e4_copy_fidelity_v12.py": "src/memecoin_bot/e4_copy_fidelity_v12.py",
        "src/memecoin_bot/e4_causal_entry_v12.py": "src/memecoin_bot/e4_causal_entry_v12.py",
        "tests/test_v12_copy_fidelity.py": "tests/test_v12_copy_fidelity.py",
        "tests/test_v12_causal_entry_runtime.py": "tests/test_v12_causal_entry_runtime.py",
        "scripts/e4_v12_live_causal_choice_validate.py": "scripts/e4_v12_live_causal_choice_validate.py",
        "scripts/e4_v12_causal_choice_pnl_replay.py": "scripts/e4_v12_causal_choice_pnl_replay.py",
        "models/e4/research/e4-v12-runtime-choice-v3-model.json": "models/e4/e4-v12-causal-entry-model.json",
        "models/e4/research/e4-v12-causal-runtime-state.json": "models/e4/e4-v12-causal-entry-state.json",
        "research/candidate-workflows/e4-v12-causal-entry-candidate-ci.yml": ".github/workflows/e4-v12-causal-entry-candidate-ci.yml",
        "research/candidate-workflows/e4-v12-causal-entry-fresh-live-v2.yml": ".github/workflows/e4-v12-causal-entry-fresh-live-v2.yml",
    }
    for source, destination in copies.items():
        show(source, destination)

    copy_hash = hashlib.sha256(
        Path("src/memecoin_bot/e4_copy_fidelity_v12.py").read_bytes()
    ).hexdigest()
    patch_entrypoint(copy_hash)
    patch_holdout(copy_hash)
    patch_fingerprint()
    patch_environment()
    Path("models/e4/e4-v12-causal-entry-trigger-v2.txt").write_text(
        "v12-causal-preimpact-choice-v3-002\n",
        encoding="utf-8",
    )
    Path("models/e4/e4-v12-evidence-epoch.txt").write_text(
        "v12-causal-preimpact-choice-v3-2026-09-04\n",
        encoding="utf-8",
    )

    run("python", "-m", "unittest", "tests.test_v12_copy_fidelity", "-v")
    run("python", "-m", "unittest", "tests.test_v12_causal_entry_runtime", "-v")
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
        "src/memecoin_bot/e4_causal_entry_v12.py",
        "scripts/e4_v12_live_causal_choice_validate.py",
        "scripts/e4_v12_causal_choice_pnl_replay.py",
    )
    environment = dict(os.environ)
    environment["E4_V12_CAUSAL_ENTRY_ENABLED"] = "true"
    environment["E4_PIPELINES_BACKGROUND"] = "false"
    subprocess.run(
        [
            "python",
            "-c",
            "import memecoin_bot.e4_exec.__main__; "
            "import memecoin_bot.e4_causal_entry_v12 as m; assert m.RUNTIME.active",
        ],
        check=True,
        env=environment,
    )

    paths = list(copies.values()) + [
        "src/memecoin_bot/e4_exec/__main__.py",
        "scripts/e4_300_launch_holdout_v12.py",
        "scripts/e4_v12_forward_accumulate.py",
        "models/e4/e4-v12-causal-entry-trigger-v2.txt",
        "models/e4/e4-v12-evidence-epoch.txt",
        ".env.e4.example",
    ]
    run("git", "add", *paths)
    run("git", "config", "user.name", "gambit-v12-causal-entry")
    run("git", "config", "user.email", "actions@users.noreply.github.com")
    run(
        "git",
        "commit",
        "-m",
        "feat(e4-v12): causal pre-impact ranked-choice candidate",
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

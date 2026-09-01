#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

# Load the persisted V11 production policy first. V10 hardening now imports the
# canonical V11 PipelineManager from e4_pipelines_v10, so the shared core is
# patched to the exact V11 selection/runtime stack before the original exact-
# 300 capture harness is imported. Its later V7 import is a cached module and
# therefore cannot overwrite the V11 patches.
from memecoin_bot import e4_hardening_v10  # noqa: F401


def _load_holdout():
    path = Path(__file__).with_name("e4_300_launch_holdout.py")
    name = "e4_300_launch_holdout_v11_base"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    holdout = _load_holdout()
    raise SystemExit(asyncio.run(holdout.main()))

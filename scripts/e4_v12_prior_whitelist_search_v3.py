#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path


def load_repaired():
    source = Path(__file__).with_name("e4_v12_prior_whitelist_search.py")
    text = source.read_text(encoding="utf-8")
    broken = '''            if (
                not state.creator
                or state.sell_count > 0
                or state.fdV_usd <= 0 if False else False
            ):
                continue
'''
    repaired = '''            if not state.creator or state.sell_count > 0:
                continue
'''
    if broken not in text:
        raise RuntimeError("expected whitelist eligibility guard was not found")
    text = text.replace(broken, repaired)
    target = Path(tempfile.gettempdir()) / "e4_v12_prior_whitelist_search_v3_base.py"
    target.write_text(text, encoding="utf-8")
    name = "e4_v12_prior_whitelist_search_v3_base"
    spec = importlib.util.spec_from_file_location(name, target)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load repaired whitelist search")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    module = load_repaired()
    original_rules = module.rules

    def comparable_rules():
        # Output-floor sensitivity is handled by the common reserve replay.
        # Keep a single production candidate floor here so equivalent coin
        # selections are not silently discarded before evaluation.
        return [
            rule
            for rule in original_rules()
            if int(rule.output_shortfall_bps) == 600
        ]

    module.rules = comparable_rules
    raise SystemExit(module.main())

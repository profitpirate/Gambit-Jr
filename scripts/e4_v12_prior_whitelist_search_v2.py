#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


if __name__ == "__main__":
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
        raise SystemExit("expected whitelist eligibility guard was not found")
    text = text.replace(broken, repaired)
    namespace = {
        "__name__": "__main__",
        "__file__": str(source),
        "__package__": None,
    }
    exec(compile(text, str(source), "exec"), namespace)

#!/usr/bin/env python3
from __future__ import annotations

import e4_v12_conclusive_entry_rerun as base
from e4_v12_strict_causal_history import add_history_strict

base.add_history = add_history_strict

import e4_v12_ranked_multimode_fast as fast  # noqa: E402,F401
import e4_v12_ranked_multimode_search as search  # noqa: E402

search.base.add_history = add_history_strict

if __name__ == "__main__":
    raise SystemExit(search.main())

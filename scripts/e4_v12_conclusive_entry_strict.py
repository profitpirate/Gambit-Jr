#!/usr/bin/env python3
from __future__ import annotations

import e4_v12_conclusive_entry_rerun as thesis
from e4_v12_strict_causal_history import add_history_strict

thesis.add_history = add_history_strict

if __name__ == "__main__":
    raise SystemExit(thesis.main())

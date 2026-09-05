#!/usr/bin/env python3
from __future__ import annotations

# Importing V2 patches the shared replay primitive before the search module is
# loaded, so both search and untouched-live application use scaled E4 token
# output rather than a post-impact spot reference.
from scripts import e4_v12_true_latency_replay_v2  # noqa: F401
from scripts import e4_v12_reactive_guard_search as search


if __name__ == "__main__":
    raise SystemExit(search.main())

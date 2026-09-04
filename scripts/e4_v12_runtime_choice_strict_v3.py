#!/usr/bin/env python3
from __future__ import annotations

import e4_v12_runtime_choice_strict_v2 as strict_v2

# `visible_competitors_log` in the offline row table is affected by repeated
# control rows across choice sets. Production computes competitors from unique
# live mints, so the feature is excluded rather than allowing a research/runtime
# mismatch. Competition itself remains explicitly represented by the pairwise
# candidate set and minimum score margin.
strict_v2.choice.FEATURES = [
    name for name in strict_v2.RUNTIME_FEATURES
    if name != "visible_competitors_log"
]

if __name__ == "__main__":
    raise SystemExit(strict_v2.choice.main())

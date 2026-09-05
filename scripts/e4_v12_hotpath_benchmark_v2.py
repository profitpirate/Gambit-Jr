#!/usr/bin/env python3
from __future__ import annotations

from memecoin_bot import e4_strict_output_deferred_v12 as deferred
from scripts import e4_v12_hotpath_benchmark as base

base.strict.guarded_request = deferred.guarded_request


if __name__ == "__main__":
    raise SystemExit(base.main())

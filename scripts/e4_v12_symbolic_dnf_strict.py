#!/usr/bin/env python3
"""Strict-economic entry point for the symbolic DNF search."""
from __future__ import annotations

from scripts import e4_v12_allout_profit_hazard_strict  # noqa: F401
from scripts import e4_v12_symbolic_dnf_search as search


if __name__ == "__main__":
    raise SystemExit(search.main())

#!/usr/bin/env python3
from __future__ import annotations

# Importing this module patches the shared reactive source-row builder with a
# creator registry reconstructed only from repository state that existed before
# the earliest tested launch window.
from scripts import e4_v12_reactive_profit_registry  # noqa: F401
from scripts import e4_v12_reactive_lattice_search as lattice


if __name__ == "__main__":
    raise SystemExit(lattice.main())

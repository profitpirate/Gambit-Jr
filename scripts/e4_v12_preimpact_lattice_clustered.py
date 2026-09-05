#!/usr/bin/env python3
from __future__ import annotations

from scripts import e4_v12_clustered_preimpact  # noqa: F401
from scripts import e4_v12_preimpact_lattice_search as lattice


if __name__ == "__main__":
    raise SystemExit(lattice.main())

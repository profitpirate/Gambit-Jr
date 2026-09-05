#!/usr/bin/env python3
from __future__ import annotations

# Import order is deliberate: the V2 failed-intent layer patches the shared
# causal dataset before the transparent lattice search module is imported.
# Failed E4 submissions are labelled only when their mapped attempt occurs
# after the snapshot and within the configured horizon; they are not removed
# globally using future knowledge.
from scripts import e4_v12_failed_aware_preimpact_v2  # noqa: F401
from scripts import e4_v12_preimpact_lattice_search as lattice


if __name__ == "__main__":
    raise SystemExit(lattice.main())

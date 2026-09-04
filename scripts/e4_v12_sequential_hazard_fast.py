#!/usr/bin/env python3
from __future__ import annotations

import e4_v12_sequential_hazard_search as hazard


def exportable_specs():
    return [
        hazard.Spec("logit", 0, 0, 0),
        hazard.Spec("extra", 5, 3, 160),
        hazard.Spec("extra", 7, 4, 200),
        hazard.Spec("extra", 9, 6, 240),
        hazard.Spec("forest", 6, 4, 180),
    ]


hazard.specs = exportable_specs

if __name__ == "__main__":
    raise SystemExit(hazard.main())

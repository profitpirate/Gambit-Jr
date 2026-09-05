#!/usr/bin/env python3
from __future__ import annotations

import e4_v12_recurrence_shape_search as base


def focused_rules() -> list[base.Rule]:
    output = []
    creator_priors = (
        (1, 0, 0.60, 0.00),
        (2, 0, 0.75, 0.20),
        (2, 1, 0.66, 0.20),
        (3, 1, 0.75, 0.25),
    )
    identity_requirements = (
        (0, 0, 0, False),
        (0, 0, 0, True),
        (1, 1, 0, False),
        (1, 1, 0, True),
        (1, 2, 1, True),
    )
    for wins, losses, rate, wilson in creator_priors:
        for seed_distance in (0.10, 0.20, 0.40):
            for fdv_distance in (0.40, 1.00):
                for overlap, frequency, prefix, shape_match in identity_requirements:
                    for top in (False, True):
                        for seed in (0.02, 0.50, 1.00):
                            for age in (50.0, 150.0, 400.0):
                                output.append(base.Rule(
                                    wins,
                                    losses,
                                    rate,
                                    wilson,
                                    seed_distance,
                                    fdv_distance,
                                    overlap,
                                    frequency,
                                    prefix,
                                    shape_match,
                                    top,
                                    seed,
                                    age,
                                    600,
                                ))
    return output


base.rules = focused_rules


if __name__ == "__main__":
    raise SystemExit(base.main())

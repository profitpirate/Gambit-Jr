#!/usr/bin/env python3
from __future__ import annotations

import e4_v12_recurrence_shape_search as base


def focused_rules() -> list[base.Rule]:
    output = []
    identity_requirements = (
        (0, 0, 0, False),
        (0, 0, 0, True),
        (1, 1, 0, False),
        (1, 1, 0, True),
        (1, 2, 1, False),
        (1, 2, 1, True),
        (2, 2, 1, True),
    )
    # Creator rate/Wilson are consequences of wins/losses and do not need an
    # independent Cartesian explosion.  These paired priors cover permissive,
    # proven and elite histories while keeping the search auditable.
    creator_priors = (
        (1, 0, 0.60, 0.00),
        (2, 0, 0.75, 0.20),
        (2, 1, 0.66, 0.20),
        (3, 0, 0.80, 0.30),
        (3, 1, 0.75, 0.25),
        (5, 1, 0.80, 0.35),
    )
    for wins, losses, rate, wilson in creator_priors:
        for seed_distance in (0.05, 0.10, 0.20, 0.40, 1.00):
            for fdv_distance in (0.20, 0.40, 1.00):
                for overlap, frequency, prefix, shape_match in identity_requirements:
                    for top in (False, True):
                        for seed in (0.02, 0.25, 0.50, 1.0, 2.0):
                            for age in (50.0, 150.0, 400.0):
                                for floor in (200, 400, 600, 800, 1_000):
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
                                        floor,
                                    ))
    return output


base.rules = focused_rules


if __name__ == "__main__":
    raise SystemExit(base.main())

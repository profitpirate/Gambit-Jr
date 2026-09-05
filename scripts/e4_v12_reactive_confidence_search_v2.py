#!/usr/bin/env python3
from __future__ import annotations

import e4_v12_reactive_confidence_search as base


def focused_rules(candidates):
    source_values = sorted({round(row.source_sol, 6) for row in candidates if row.source_sol > 0})
    relative_values = sorted({
        round(row.source_sol / max(1.0, row.fdv_usd) * 10_000.0, 6)
        for row in candidates if row.source_sol > 0 and row.fdv_usd > 0
    })

    def at(values, probability):
        if not values:
            return 0.0
        return values[min(len(values)-1, max(0, round((len(values)-1)*probability)))]

    def distinct_bands(values):
        q0,q25,q50,q75,q90,q100=(at(values,p) for p in (0.0,0.25,0.50,0.75,0.90,1.0))
        return sorted(set((
            (q0,q100),
            (q25,q100),
            (q50,q100),
            (q75,q100),
            (q90,q100),
            (q0,q50),
            (q25,q75),
            (q50,q90),
            (q75,q100),
        )))

    source_bands=distinct_bands(source_values)
    relative_bands=distinct_bands(relative_values)
    output=[]
    for floor in (200,400,600,800,1_000,1_500):
        for source_min,source_max in source_bands:
            for relative_min,relative_max in relative_bands:
                for fdv_min,fdv_max in (
                    (2_750.0,5_000.0),
                    (3_200.0,7_500.0),
                    (2_750.0,10_000.0),
                ):
                    for creator_wins,creator_losses in ((0,99),(1,1),(2,1)):
                        for seed,outside,buyers in (
                            (0.0,0.0,0),
                            (0.5,0.25,1),
                            (1.5,1.0,1),
                        ):
                            for age in (50.0,150.0,400.0):
                                output.append(base.Rule(
                                    floor,source_min,source_max,
                                    relative_min,relative_max,
                                    fdv_min,fdv_max,
                                    creator_wins,creator_losses,
                                    seed,outside,buyers,age,
                                ))
    return output


base.rules=focused_rules


if __name__=="__main__":
    raise SystemExit(base.main())

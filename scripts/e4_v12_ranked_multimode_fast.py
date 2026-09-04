#!/usr/bin/env python3
from __future__ import annotations

import json

import e4_v12_ranked_multimode_search as model


def bounded_specs():
    return [
        model.ModelSpec("logit", 0, 0),
        model.ModelSpec("extra", 5, 3, 220),
        model.ModelSpec("extra", 7, 4, 220),
        model.ModelSpec("extra", 9, 6, 220),
        model.ModelSpec("forest", 6, 4, 220),
        model.ModelSpec("hist", 5, 12, 180),
    ]


def bounded_search(train, validation):
    best = None
    for spec in bounded_specs():
        authority_model = model.fit_model(train, model.AUTHORITY, spec)
        cluster_model = model.fit_model(train, model.CLUSTER, spec)
        union_model = model.fit_model(train, model.UNION, spec)
        scored = model.score(validation, authority_model, cluster_model, union_model)
        for threshold in (0.80, 0.86, 0.90, 0.94, 0.97, 0.99):
            for split in (-0.03, 0.0, 0.03):
                authority_threshold = min(0.999, max(0.50, threshold + split))
                cluster_threshold = min(0.999, max(0.50, threshold - split))
                for union_delta in (0.0, 0.04):
                    union_threshold = min(0.999, threshold + union_delta)
                    for margin in (0.0, 0.05, 0.10):
                        for window in (100.0, 250.0, 500.0):
                            for top_k in (1, 2):
                                for combination in ("MAX", "BLEND"):
                                    gate = model.RankedGate(
                                        authority_threshold,
                                        cluster_threshold,
                                        union_threshold,
                                        margin,
                                        window,
                                        top_k,
                                        combination,
                                    )
                                    result = model.evaluate(validation, model.predict(scored, gate))
                                    if result["true"] < 5 or result["recall"] < 0.10:
                                        continue
                                    valid = result["precision"] >= 0.60 and result["precision_wilson_low"] >= 0.32
                                    objective = (
                                        int(valid),
                                        result["precision_wilson_low"],
                                        result["precision"],
                                        result["recall"],
                                        result["true"],
                                        -result["false_positives"],
                                    )
                                    if best is None or objective > best[0]:
                                        best = (
                                            objective,
                                            spec,
                                            authority_model,
                                            cluster_model,
                                            union_model,
                                            gate,
                                            result,
                                        )
        print(json.dumps({"evaluated_spec": spec.as_dict(), "best_objective": best[0] if best else None}), flush=True)
    if best is None:
        raise RuntimeError("bounded ranked search produced no viable rule")
    return best[1], best[2], best[3], best[4], best[5], best[6]


model.search = bounded_search

if __name__ == "__main__":
    raise SystemExit(model.main())

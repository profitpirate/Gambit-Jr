"""Render the immutable manifest for the fresh six-arm loss2s A/B campaign."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from golden_loss2s_engine import ARMS

CAMPAIGN = "golden-loss2s-ab-fresh-20260916"
DEST = Path("config/golden-loss2s-live.json")


def prepare():
    cfg = json.loads(Path("config/golden-horizon.json").read_text())
    sources = list(Path("tools").glob("golden_loss2s_*.py")) + [
        Path("tools/golden_top4_solana.py"),
        Path("tools/golden_top4_engine.py"),
        Path("tools/golden_top4_v2_eventstream.py"),
        Path("tools/golden_horizon_live.py"),
        Path("tools/golden_horizon_engine.py"),
    ]
    cfg.update(
        experiment="golden-loss2s-forward-ab-v1",
        campaign_name=CAMPAIGN,
        models=list(ARMS),
        execution="PAPER_ONLY",
        target_per_model=100,
        market_source="DIRECT_SOLANA_NEW_CREATIONS",
        actual_axiom_execution=False,
        families=["FULL"],
        horizons_seconds=[3, 4, 7],
        model_semantics=(
            "Fresh six-account forward A/B: the four existing top4 models are "
            "unchanged; FULL_3S_2S_LOSS_EXIT and FULL_3S_V2_2S_LOSS_EXIT use "
            "one causal 2000ms net-after-costs loss checkpoint, then retain the "
            "parent 3000ms horizon when the checkpoint is non-negative."
        ),
        implementation_sha256={
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(set(sources))
        },
    )
    cfg["completion"].update(
        same_mint_cohort_across_12_arms=False,
        minimum_verified_forward_per_model=100,
    )
    cfg["limitations"] = [
        x for x in cfg["limitations"] if "twelve" not in x.lower()
    ] + [
        "The 2s rule is causal: it can only classify a trade by its observed net mark at the checkpoint, not by its eventual hindsight outcome.",
        "A 2000ms exit decision still uses the campaign's modeled inclusion delay; it is not a claim of an on-chain fill exactly at 2000ms.",
        "Shadow arms start with separate 3 SOL paper accounts; later admission divergence caused by their own bankroll/concurrency is retained as a policy consequence.",
    ]
    rendered = json.dumps(cfg, sort_keys=True, indent=2) + "\n"
    if DEST.exists() and DEST.read_text() != rendered:
        raise ValueError(
            "registered loss2s manifest changed: start a separately named experiment"
        )
    DEST.write_text(rendered)
    return cfg


if __name__ == "__main__":
    prepare()

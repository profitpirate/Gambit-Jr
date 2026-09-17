from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import e4_v12_selection_cohort_forensics as subject


def test_oracle_is_explicitly_not_a_selection_model() -> None:
    assert subject.SCHEMA_VERSION.endswith("v1")
    result = subject.json.loads(
        (ROOT / "artifacts/e4-v12-selection-cohort-forensics.json").read_text(
            encoding="utf-8"
        )
    )
    assert result["oracle_is_selection_model"] is False
    assert result["production_paths_changed"] == 0


def test_compact_models_are_selected_without_holdout() -> None:
    result = subject.json.loads(
        (ROOT / "artifacts/e4-v12-selection-cohort-forensics.json").read_text(
            encoding="utf-8"
        )
    )
    frontier = result["compact_model_frontier"]
    assert frontier["selection_used_holdout"] is False
    assert frontier["holdout_is_exploratory_because_prior_experiment_opened_it"] is True
    assert {row["family"] for row in frontier["models"]} == {
        "logistic",
        "decision_tree_depth_4",
        "decision_tree_depth_6",
        "extra_trees_32",
        "extra_trees_64",
    }

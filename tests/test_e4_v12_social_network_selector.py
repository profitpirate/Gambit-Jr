from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import e4_v12_social_network_selector as subject


def test_post_entry_fields_are_excluded() -> None:
    result = subject.json.loads(
        (ROOT / "artifacts/e4-v12-social-network-selector.json").read_text(
            encoding="utf-8"
        )
    )
    assert "outside_sol" in result["post_entry_fields_excluded"]
    assert "price_multiple" in result["post_entry_fields_excluded"]
    assert result["golden_thesis_approved"] is False
    assert result["production_paths_changed"] == 0


def test_strict_model_does_not_use_untimed_buyer_identities() -> None:
    result = subject.json.loads(
        (ROOT / "artifacts/e4-v12-social-network-selector.json").read_text(
            encoding="utf-8"
        )
    )
    strict = [row for row in result["models"] if row["family"].startswith("strict_")]
    assert strict
    assert all("known_buyer_count" not in row["feature_names"] for row in strict)

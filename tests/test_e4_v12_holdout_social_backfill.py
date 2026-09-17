from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import e4_v12_holdout_social_backfill as subject


def test_social_url_parser_extracts_status_and_handle() -> None:
    url = "https://x.com/Example_User/status/2095654190861185445?s=20"
    assert subject.social_handle(url) == "example_user"
    assert subject.social_status_id(url) == "2095654190861185445"
    assert subject.snowflake_timestamp_ns("2095654190861185445") > 0


def test_ipfs_uri_uses_retrievable_gateway() -> None:
    assert subject.normalise_metadata_uri("ipfs://bafy-test") == (
        "https://gateway.pinata.cloud/ipfs/bafy-test"
    )
    assert subject.normalise_metadata_uri("https://ipfs.io/ipfs/bafy-test") == (
        "https://gateway.pinata.cloud/ipfs/bafy-test"
    )


def test_committed_backfill_remains_research_only() -> None:
    result = json.loads(
        (ROOT / "artifacts/e4-v12-holdout-social-backfill.json").read_text(
            encoding="utf-8"
        )
    )
    assert result["scientific_status"] == "EXPLORATORY_BACKFILL_NOT_UNTOUCHED"
    assert result["golden_thesis_approved"] is False
    assert result["production_paths_changed"] == 0

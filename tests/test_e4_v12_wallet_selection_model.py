from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import e4_v12_profit_survival_search as base
import e4_v12_wallet_selection_model as subject


def test_text_hash_is_deterministic_and_fixed_width() -> None:
    left = subject.text_hash_features("Alpha Dog", "ADOG")
    right = subject.text_hash_features("Alpha Dog", "ADOG")
    changed = subject.text_hash_features("Alpha Cat", "ACAT")
    assert left == right
    assert len(left) == subject.TEXT_BUCKETS
    assert left != changed
    assert np.isfinite(left).all()


def test_selector_features_exclude_e4_authored_buy() -> None:
    create = base.Point(1, "CREATE", "creator", "create-sig", 7, 0, 1e-8, 30, 1e9, 8e8, False)
    creator_buy = base.Point(1, "BUY", "creator", "create-sig", 7, 1.0, 1.1e-8, 31, 9e8, 8e8, False)
    e4_buy = base.Point(2, "BUY", subject.E4_WALLET, "e4-sig", 7, 2.0, 1.2e-8, 33, 8e8, 7e8, False)
    public_buy = base.Point(2, "BUY", "public", "public-sig", 7, 0.5, 1.2e-8, 33, 8e8, 7e8, False)
    trace = base.Trace("1", "train", "mint", "creator", 1, 7, "create-sig", False, False, True, 0, [create, creator_buy, e4_buy, public_buy])
    row = {"raw": {"name": "Token", "symbol": "TOK", "uri": "ipfs://x"}}
    features = subject.selector_features(trace, row, 1, creator_prior=(0, 0, 0, 0, None), launches_1s=0, launches_10s=0)
    values = dict(zip(subject.FEATURE_NAMES, features))
    assert values["creator_seed_sol"] == 1.0
    assert values["public_buy_sol"] == 0.5
    assert values["public_buy_count"] == 1.0
    assert values["unique_public_buyers"] == 1.0


def test_failed_fill_is_a_positive_intent_label() -> None:
    labels = subject.load_labels(
        ROOT / "artifacts/e4-v12-canonical-choice-risksets-v2.jsonl"
    )
    assert len(labels) == 498
    assert any(not landed for landed in labels.values())


def test_partial_exit_policy_is_independent_of_e4_future_sells() -> None:
    policy = subject.ExitPolicy(0.8, 1.3, 0.3, 4_000, 0.2)
    assert policy.first_fraction == 0.3
    assert "first=1.30:0.30" in policy.key


def test_committed_research_result_cannot_promote_production() -> None:
    development = subject.json.loads(
        (ROOT / "artifacts/e4-v12-wallet-selection-development.json").read_text(
            encoding="utf-8"
        )
    )
    holdout = subject.json.loads(
        (ROOT / "artifacts/e4-v12-wallet-selection-holdout.json").read_text(
            encoding="utf-8"
        )
    )
    assert development["status"] == "EXPLORATORY_POST_HOLDOUT_REVISION"
    assert development["holdout_rows_read_before_this_revision"] == 30_000
    assert holdout["golden_gate"]["golden_thesis_approved"] is False
    assert holdout["golden_gate"]["evaluation_role"] == (
        "contaminated_replay_not_certification"
    )
    assert development["production_paths_changed"] == 0
    assert holdout["production_paths_changed"] == 0


def test_latency_frontier_exposes_sub_five_millisecond_dependency() -> None:
    holdout = subject.json.loads(
        (ROOT / "artifacts/e4-v12-wallet-selection-holdout.json").read_text(
            encoding="utf-8"
        )
    )
    frontier = holdout["exact_wallet_choice_oracle"]["results"]["selected"][
        "causal_horizon_frontier"
    ]
    assert frontier["5"]["win_rate"] >= 0.65
    assert frontier["20"]["win_rate"] < 0.55
    assert frontier["250"]["win_rate"] < 0.50


def test_holdout_extraction_warms_creator_history(monkeypatch, tmp_path: Path) -> None:
    specs = [
        base.CaptureSpec("train", "train", 0, 10, tmp_path / "train", "hash"),
        base.CaptureSpec("hold", "holdout", 11, 20, tmp_path / "hold", "hash"),
    ]

    def trace(run_id: str, split: str, mint: str, timestamp: int) -> base.Trace:
        point = base.Point(
            timestamp, "CREATE", "creator", f"{mint}-sig", 1, 1.0,
            1e-8, 30.0, 1e9, 8e8, False,
        )
        return base.Trace(
            run_id, split, mint, "creator", timestamp, 1, f"{mint}-sig",
            False, False, True, 0, [point],
        )

    traces = {
        "train": [trace("train", "train", "mint-train", 1)],
        "hold": [trace("hold", "holdout", "mint-hold", 12)],
    }
    creates = {
        "train": {"mint-train": {"raw": {"name": "A", "symbol": "A"}}},
        "hold": {"mint-hold": {"raw": {"name": "B", "symbol": "B"}}},
    }
    monkeypatch.setattr(subject, "load_labels", lambda _path: {("train", "mint-train"): True})
    monkeypatch.setattr(subject.base, "capture_specs", lambda *_args, **_kwargs: specs)
    monkeypatch.setattr(subject.base, "sha256_path", lambda _path: "hash")
    monkeypatch.setattr(subject, "load_create_rows", lambda path: creates[path.name])
    monkeypatch.setattr(
        subject.base,
        "load_capture",
        lambda spec, _counts: (traces[spec.run_id], 0),
    )

    rows, _, audit = subject.extract_dataset(
        tmp_path, include_holdout=True, only_holdout=True
    )
    attempts_index = subject.FEATURE_NAMES.index("creator_prior_e4_attempts")
    assert {row.split for row in rows} == {"holdout"}
    assert {row.features[attempts_index] for row in rows} == {1.0}
    assert audit["launches"] == 1
    assert audit["history_warmup_launches"] == 1
    assert audit["runs"][0]["history_warmup_only"] is True

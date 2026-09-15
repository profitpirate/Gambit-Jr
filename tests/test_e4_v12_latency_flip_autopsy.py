from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


autopsy = load_module(
    "e4_v12_latency_flip_autopsy_tests",
    SCRIPTS / "e4_v12_latency_flip_autopsy.py",
)


def report() -> dict:
    return json.loads(
        (ROOT / "artifacts/e4-v12-latency-flip-autopsy.json").read_text(
            encoding="utf-8"
        )
    )


def test_identity_binds_source_selection_features_and_latencies() -> None:
    result = report()
    identity = result["identity"]
    assert result["diagnostic_id"] == (
        f"e4d-{autopsy.base.stable_hash(identity)}"
    )
    assert identity["source_code_fingerprint"] == (
        autopsy.source_code_fingerprint(ROOT)
    )
    assert identity["latencies_ms"] == [10, 300]
    assert identity["source_experiment_id"] == (
        "e4x-b1086b70febc39fefa73d35f8451674a126a3c0256ca95a608b57f9c9c6cd1cc"
    )


def test_autopsy_quantifies_latency_flips_across_every_fold() -> None:
    result = report()
    assert result["source_selection_count"] == 317
    assert result["categories"] == {
        "loss_to_loss": 105,
        "loss_to_win": 6,
        "win_to_loss": 35,
        "win_to_win": 171,
    }
    assert 0.169 < result["win_to_loss_fraction_of_10ms_winners"] < 0.170
    assert result["net_winner_loss_at_300ms"] == 29
    assert len(result["folds"]) == 4
    assert all(row["trades"] >= 64 for row in result["folds"])


def test_sparse_buyer_history_and_volatility_are_consistent_signals() -> None:
    rows = {row["feature"]: row for row in report()["top_consistent_decision_features"]}
    history = rows["buyer_history_count_mean"]
    volatility = rows["log_return_volatility"]
    assert history["direction"] == "higher"
    assert history["directionally_consistent_folds"] == 4
    assert history["category_medians"]["win_to_loss"] == 2.0
    assert history["category_medians"]["win_to_win"] == 121.0
    assert volatility["direction"] == "lower"
    assert volatility["directionally_consistent_folds"] == 4
    assert volatility["category_medians"]["win_to_loss"] > 0.32
    assert volatility["category_medians"]["win_to_win"] < 0.06


def test_diagnostic_does_not_fit_candidate_or_touch_live_production() -> None:
    result = report()
    assert result["candidate_fitted"] is False
    assert result["active_untouched_live_data_used"] is False
    assert result["live_confirmation_authorised"] is False
    assert result["production_promotion_authorised"] is False
    assert result["production_deployment_authorised"] is False
    assert result["production_paths_changed"] == 0

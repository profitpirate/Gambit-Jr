from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pytest

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


moe = load_module("e4_v12_choice_moe_tests", SCRIPTS / "e4_v12_choice_moe.py")
PREFIX = ROOT / "artifacts/e4-v12-choice-moe"


def artifact(suffix: str):
    return json.loads(Path(f"{PREFIX}-{suffix}").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def outputs():
    return {
        "data": artifact("data-audit.json"),
        "null": artifact("null-group-audit.json"),
        "features": artifact("feature-manifest.json"),
        "models": artifact("model-comparison.json"),
        "selection": artifact("selection-report.json"),
        "anti": artifact("anti-shortcut-audit.json"),
        "ablation": artifact("ablation-report.json"),
        "calibration": artifact("calibration.json"),
        "execution": artifact("execution-report.json"),
        "economics": artifact("historical-economics.json"),
        "verdict": artifact("historical-verdict.json"),
        "experiment": artifact("experiment-manifest.json"),
        "repro": artifact("reproducibility.json"),
    }


def test_all_required_artifacts_exist() -> None:
    suffixes = (
        "data-audit.json",
        "null-groups.jsonl",
        "null-group-audit.json",
        "feature-manifest.json",
        "model-comparison.json",
        "selection-report.json",
        "selection-report.md",
        "anti-shortcut-audit.json",
        "ablation-report.json",
        "calibration.json",
        "execution-report.json",
        "historical-economics.json",
        "historical-verdict.json",
        "experiment-manifest.json",
        "reproducibility.json",
    )
    assert all(Path(f"{PREFIX}-{suffix}").is_file() for suffix in suffixes)


def test_v1_and_v2_inputs_remain_frozen(outputs) -> None:
    data = outputs["data"]
    assert data["status"] == "PASS"
    assert len(data["frozen_inputs"]) == 12
    for row in data["frozen_inputs"]:
        path = ROOT / row["path"]
        assert path.stat().st_size == row["expected_bytes"]
        assert moe.sha256_path(path) == row["expected_sha256"]


def test_jsonl_parquet_parity_and_order(outputs) -> None:
    parity = outputs["data"]["jsonl_parquet_parity"]
    assert parity["status"] == "PASS"
    assert parity["jsonl_rows"] == parity["parquet_rows"] == 12_927
    assert parity["row_order_match"] is True


def test_all_model_features_are_causal(outputs) -> None:
    assert outputs["null"]["candidate_features_causal"] is True
    assert outputs["features"]["causality"]["candidate_state"].endswith("<= decision_ns")


def test_labels_and_provenance_never_enter_selection_matrix(outputs) -> None:
    names = outputs["features"]["expanded_feature_names"]
    prohibited = outputs["features"]["prohibited_tokens"]
    assert not [name for name in names if any(token in name for token in prohibited)]
    assert outputs["features"]["leakage_scan_violations"] == []


def test_execution_outcomes_never_enter_selection_matrix(outputs) -> None:
    names = set(outputs["features"]["expanded_feature_names"])
    assert not names & {
        "landed_successfully",
        "source_transaction_failed",
        "output_guard_rejected",
        "execution_outcome",
    }


def test_no_trade_groups_have_one_outside_option_and_no_selected_coin(outputs) -> None:
    audit = outputs["null"]
    assert audit["groups"] == audit["outside_option_rows"]
    assert audit["selected_coin_rows"] == 0
    assert audit["known_selection_overlap"] == 0


def test_unresolved_wallet_activity_is_fail_closed(outputs) -> None:
    audit = outputs["null"]
    assert "unresolved_wallet_activity_excluded" in audit
    assert (
        audit["interval_inventory"]["unresolved_wallet_intervals"]
        == audit["unresolved_wallet_activity_excluded"]
    )
    assert audit["sampling_policy"]["exclusion_margin_ms"] == 2_000


def test_no_group_crosses_chronological_splits(outputs) -> None:
    data = outputs["data"]
    assert data["no_group_crosses_splits"] is True
    run_sets = [set(values) for values in data["chronological_split"].values()]
    assert not run_sets[0] & run_sets[1]
    assert not run_sets[0] & run_sets[2]
    assert not run_sets[1] & run_sets[2]


def test_pairwise_group_weights_are_equal() -> None:
    groups = []
    for group_id, alternative_count in (("a", 2), ("b", 7)):
        rows = []
        for index in range(alternative_count + 1):
            rows.append(
                {
                    "decision_group_id": group_id,
                    "candidate_mint": f"{group_id}{index}",
                    "selected_by_e4": index == 0,
                    "split": "train",
                    "candidate_age_ms": index,
                }
            )
        groups.append(rows)
    transformer = moe.TrainOnlyTransformer(("candidate_age_ms",)).fit(groups)
    _, labels, weights = moe.symmetrical_pairs(groups, transformer)
    offset = 0
    for alternative_count in (2, 7):
        size = alternative_count * 2
        assert weights[offset : offset + size].sum() == pytest.approx(1.0)
        assert labels[offset : offset + size].sum() == alternative_count
        offset += size


def test_candidate_row_order_and_pair_orientation_are_invariant(outputs) -> None:
    reproducibility = outputs["repro"]
    assert reproducibility["row_order_invariance"]["status"] == "PASS"
    assert reproducibility["row_order_invariance"]["mismatches"] == 0
    assert reproducibility["pair_orientation"]["symmetrical_orientations"] is True
    assert reproducibility["pair_orientation"]["equal_group_weight"] is True


def test_label_permutation_collapses_to_chance(outputs) -> None:
    result = outputs["repro"]["label_permutation"]
    assert result["collapsed_below_reference"] is True
    assert result["statistically_indistinguishable_from_chance"] is True


def test_all_transforms_and_thresholds_are_train_only(outputs) -> None:
    assert outputs["repro"]["validation_or_holdout_transform_fit"] is False
    assert outputs["calibration"]["fit"]["all_calibration_and_threshold_fitting_split"] == "train"
    policy = outputs["null"]["sampling_policy"]
    assert policy["fit_split"] == "train"
    assert policy["validation_used_to_fit"] is False
    assert policy["holdout_used_to_fit"] is False


def test_failed_fill_uncertainty_is_preserved(outputs) -> None:
    sensitivity = outputs["selection"]["failed_fill_sensitivity"]
    assert sensitivity["failed_fill_groups"] == 283
    assert sensitivity["optimistic_lower_bound"]["exact_hazard_fit_eligible"] is False
    assert sensitivity["central_interval_midpoint"]["exact_hazard_fit_eligible"] is False
    assert sensitivity["conservative_upper_bound"]["exact_hazard_fit_eligible"] is False


def test_identity_model_has_cold_start_audits(outputs) -> None:
    audit = outputs["models"]["identity_cold_start"]
    assert {"unseen_creator", "unseen_buyer", "unseen_cluster", "cold_start"} <= set(audit)
    assert audit["cold_start"]["identity_model_is_final_candidate"] is False


def test_same_slot_and_matched_age_subsets_are_reported(outputs) -> None:
    subsets = outputs["anti"]["subsets"]
    assert subsets["same_slot_alternative"]["groups"] > 0
    assert subsets["matched_age"]["groups"] > 0
    assert subsets["same_slot_alternative"]["small_sample_warning"] is True


def test_every_required_baseline_is_evaluated(outputs) -> None:
    comparison = outputs["selection"]["model_comparison"]
    assert set(moe.BASELINES) <= set(comparison)
    assert all(comparison[name]["validation"] is not None for name in moe.BASELINES)
    assert all(comparison[name]["holdout"] is not None for name in moe.BASELINES)


def test_all_required_ablations_are_present(outputs) -> None:
    results = outputs["ablation"]["results"]
    assert len(results) == 12
    assert {
        "remove_candidate_age",
        "remove_all_recency",
        "remove_creator",
        "remove_buyer_cluster",
        "remove_topology",
        "remove_market_flow",
        "remove_social",
        "remove_raw_identities",
        "creator_only",
        "buyer_only",
        "flow_only",
        "age_only",
    } == set(results)


def test_primary_economics_never_use_future_e4_exits(outputs) -> None:
    economics = outputs["economics"]
    assert economics["exit_policy"]["uses_e4_future_sells"] is False
    assert all(
        row["primary_exit_uses_e4_future_sells"] is False for row in economics["latencies"].values()
    )


def test_false_positives_fees_and_rejections_are_counted(outputs) -> None:
    rows = outputs["economics"]["latencies"].values()
    assert all("false_positive_trades" in row for row in rows)
    assert all("false_entries_per_1000_captured_launches" in row for row in rows)
    assert all(row["fees_paid_sol"] > 0 for row in rows)
    assert all("rejected_submission_costs_sol" in row for row in rows)


def test_each_latency_is_independently_recalculated(outputs) -> None:
    rows = outputs["economics"]["latencies"]
    assert set(rows) == {"0", "1", "2", "5", "10"}
    assert all(row["independently_recalculated"] for row in rows.values())
    assert len({row["closed_trade_ledger_hash"] for row in rows.values()}) == 5


def test_bankroll_accounting_is_chronological(outputs) -> None:
    for row in outputs["economics"]["latencies"].values():
        assert row["ending_bankroll_sol"] == pytest.approx(
            moe.STARTING_BANKROLL_SOL + row["net_pnl_sol"]
        )
        assert row["ending_bankroll_sol"] >= 0


def test_historical_configuration_froze_before_holdout(outputs) -> None:
    freeze = outputs["selection"]["freeze_sequence"]
    assert freeze["model_family_frozen_before_holdout"] is True
    assert freeze["thresholds_frozen_before_holdout"] is True
    assert freeze["execution_policy_frozen_before_holdout"] is True
    assert freeze["exit_policy_frozen_before_holdout"] is True
    assert freeze["holdout_evaluations_after_freeze"] == 1


def test_live_artifacts_are_absent_when_historical_gate_fails(outputs) -> None:
    assert outputs["verdict"]["historical_qualification_passed"] is False
    assert outputs["verdict"]["live_confirmation_authorised"] is False
    assert not list((ROOT / "artifacts").glob("e4-v12-choice-moe-live-*"))
    assert not list((ROOT / "models/e4/research").glob("e4-v12-choice-moe-frozen-*"))


def test_experiment_id_is_deterministic_and_new(outputs) -> None:
    experiment = outputs["experiment"]
    assert moe.registry_experiment_id(experiment["identity"]) == experiment["experiment_id"]
    assert experiment["existing_registry_unique_experiments"] == 13
    assert experiment["scientifically_new_relative_to_registry"] is True


def test_failed_experiment_is_retired_and_cannot_redispatch(outputs) -> None:
    experiment = outputs["experiment"]
    assert experiment["retired"] is True
    assert experiment["failure_classification"] in {
        "INSUFFICIENT_LABELS",
        "FALSE_POSITIVE_OVERLOAD",
        "NEGATIVE_EXPECTANCY",
        "INADEQUATE_PROFIT_FACTOR",
        "LATENCY_FAILURE",
        "HOLDOUT_COLLAPSE",
    }
    assert experiment["identical_scientific_failure_redispatch_allowed"] is False
    assert experiment["infrastructure_retry_allowed"] is True


def test_deterministic_model_and_economic_replay(outputs) -> None:
    reproducibility = outputs["repro"]
    assert reproducibility["deterministic_two_run"]["identical"] is True
    assert reproducibility["economic_replay"]["identical"] is True


def test_no_production_path_changed(outputs) -> None:
    result = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            moe.BASE_COMMIT,
            "--",
            "src/memecoin_bot",
            "models/e4",
            "tools/e4-builder",
        ],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    )
    assert not result.stdout.strip()
    assert outputs["verdict"]["production_paths_changed"] == 0


def test_null_group_file_has_no_duplicate_ids() -> None:
    seen = set()
    split_by_id = defaultdict(set)
    with Path(f"{PREFIX}-null-groups.jsonl").open("r", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            group_id = row["decision_group_id"]
            assert group_id not in seen
            seen.add(group_id)
            split_by_id[group_id].add(row["split"])
            assert row["chosen_alternative"] == "NO_TRADE"
            assert (
                sum(bool(candidate.get("is_outside_option")) for candidate in row["alternatives"])
                == 1
            )
    assert all(len(splits) == 1 for splits in split_by_id.values())


def test_numpy_json_boundary_is_deterministic() -> None:
    value = {"b": np.int64(2), "a": np.asarray([np.float64(1.5)])}
    assert moe.canonical_json(value) == '{"a":[1.5],"b":2}'

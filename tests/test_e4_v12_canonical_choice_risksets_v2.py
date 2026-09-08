from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import duckdb
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


v1 = load_module("choice_v1_for_v2_tests", SCRIPTS / "e4_v12_canonical_choice_sets.py")
v2 = load_module(
    "choice_risksets_v2_tests", SCRIPTS / "e4_v12_canonical_choice_risksets_v2.py"
)

JSONL = ROOT / "artifacts/e4-v12-canonical-choice-risksets-v2.jsonl"
PARQUET = ROOT / "artifacts/e4-v12-canonical-choice-risksets-v2.parquet"
COVERAGE = ROOT / "artifacts/e4-v12-choice-riskset-v2-coverage.json"


@pytest.fixture(scope="module")
def connection():
    value = duckdb.connect()
    parquet_path = str(PARQUET).replace("'", "''")
    value.execute(f"CREATE VIEW risksets AS SELECT * FROM read_parquet('{parquet_path}')")
    yield value
    value.close()


def test_v1_artifacts_are_byte_identical() -> None:
    audit = v2.verify_v1(ROOT)
    assert audit["status"] == "PASS"
    assert audit["changed_files"] == 0
    assert all(row["byte_identical"] for row in audit["files"])


def test_received_ns_is_primary_chronology_authority() -> None:
    later_receipt_lower_index = {
        "received_ns": 20,
        "slot": 1,
        "transaction_index": 0,
        "event_index": 0,
        "signature": "a",
    }
    earlier_receipt_higher_index = {
        "received_ns": 10,
        "slot": 9,
        "transaction_index": 999,
        "event_index": 99,
        "signature": "z",
    }
    assert v1.event_sort_key(earlier_receipt_higher_index) < v1.event_sort_key(
        later_receipt_lower_index
    )


def test_transaction_index_breaks_only_exact_receipt_ties() -> None:
    first = {"received_ns": 10, "slot": 2, "transaction_index": 4, "event_index": 9}
    second = {"received_ns": 10, "slot": 2, "transaction_index": 5, "event_index": 0}
    assert v1.event_sort_key(first) < v1.event_sort_key(second)


def test_every_alternative_existed_by_decision(connection) -> None:
    assert connection.execute(
        "SELECT count(*) FROM risksets WHERE launch_received_ns > decision_ns"
    ).fetchone()[0] == 0


def test_every_feature_timestamp_is_causal(connection) -> None:
    fields = (
        "candidate_state_ns",
        "feature_max_event_ns",
        "reserve_state_ns",
        "social_feature_ns",
        "creator_history_feature_ns",
        "whitelist_feature_ns",
        "entity_history_feature_ns",
    )
    for field in fields:
        assert connection.execute(
            f"SELECT count(*) FROM risksets WHERE {field} > decision_ns"
        ).fetchone()[0] == 0


def test_every_group_has_exactly_one_selected_candidate(connection) -> None:
    assert connection.execute(
        """
        SELECT count(*) FROM (
          SELECT decision_group_id
          FROM risksets
          GROUP BY decision_group_id
          HAVING count(*) FILTER (WHERE selected_by_e4) <> 1
        )
        """
    ).fetchone()[0] == 0


def test_every_selected_event_appears_in_own_group(connection) -> None:
    assert connection.execute(
        """
        SELECT count(*) FROM risksets
        WHERE selected_by_e4 AND chosen_mint <> candidate_mint
        """
    ).fetchone()[0] == 0


def test_v1_decision_group_ids_are_stable_and_retained(connection) -> None:
    v1_ids = set()
    with (ROOT / "artifacts/e4-v12-canonical-choice-sets.jsonl").open(
        encoding="utf-8"
    ) as handle:
        for line in handle:
            row = json.loads(line)
            if row["selected_by_e4"]:
                v1_ids.add(row["decision_group_id"])
    v2_ids = {
        row[0]
        for row in connection.execute(
            "SELECT decision_group_id FROM risksets WHERE selected_by_e4"
        ).fetchall()
    }
    assert len(v1_ids) == 322
    assert v1_ids <= v2_ids


def test_failed_fills_remain_positive_selection_labels(connection) -> None:
    selected, negatives = connection.execute(
        """
        SELECT
          count(*) FILTER (
            WHERE selection_label = 'SELECTED_FILL_REJECTED' AND selected_by_e4
          ),
          count(*) FILTER (
            WHERE selection_label = 'SELECTED_FILL_REJECTED' AND NOT selected_by_e4
          )
        FROM risksets
        """
    ).fetchone()
    assert selected == 283
    assert negatives == 0


def test_selection_and_execution_targets_are_separate(connection) -> None:
    labels = connection.execute(
        """
        SELECT selection_label, execution_outcome, count(*)
        FROM risksets WHERE selected_by_e4
        GROUP BY ALL ORDER BY ALL
        """
    ).fetchall()
    assert ("SELECTED_LANDED", "LANDED_SUCCESSFULLY", 215) in labels
    assert sum(count for label, _, count in labels if label == "SELECTED_FILL_REJECTED") == 283


def test_no_duplicate_mint_within_a_group(connection) -> None:
    assert connection.execute(
        """
        SELECT count(*) FROM (
          SELECT decision_group_id
          FROM risksets
          GROUP BY decision_group_id
          HAVING count(*) <> count(DISTINCT candidate_mint)
        )
        """
    ).fetchone()[0] == 0


def policy_fixture(split: str, alternatives: int) -> list[dict]:
    rows = []
    for index in range(alternatives + 1):
        rows.append(
            {
                "split": split,
                "same_slot_alternative": index == 0,
                "candidate_age_ms": min(index * 100, 60_000),
                "riskset_eligible": True,
                "selected_by_e4": index == 0,
                "reserve_valid": True,
                "economically_executable": True,
            }
        )
    return rows


def test_active_age_policy_is_fit_on_training_only() -> None:
    train = policy_fixture("train", 10)
    base = v2.age_policy([train, policy_fixture("validation", 1), policy_fixture("holdout", 1)])
    changed = v2.age_policy(
        [train, policy_fixture("validation", 500), policy_fixture("holdout", 500)]
    )
    assert base == changed
    assert base["policy_fit_split"] == "train"
    assert not base["validation_used_to_fit"]
    assert not base["holdout_used_to_fit"]


def test_hard_negatives_ignore_future_outcomes() -> None:
    fields = {
        "split": "train",
        "candidate_age_ms": 1.0,
        "fdv_usd": 2.0,
        "creator_seed_sol": 3.0,
        "public_buy_sol": 4.0,
        "unique_buyers": 5,
        "creator_prior_e4_selection_count_v2": 0,
        "buyer_cluster_recurrence": 0,
        "topology_score": 1,
        "first_buyer_identities_json": "[]",
        "social_available_before_decision": False,
        "same_slot_alternative": False,
        "same_transaction_alternative": False,
    }
    group = [
        {
            **fields,
            "decision_group_id": "group",
            "candidate_mint": "selected",
            "selected_by_e4": True,
            "future_return": 1000,
        },
        {
            **fields,
            "decision_group_id": "group",
            "candidate_mint": "alternative",
            "selected_by_e4": False,
            "future_return": -1,
        },
    ]
    scales = {field: 1.0 for field in v2.training_scales([group])}
    v2.add_hard_negatives([group], scales)
    categories = group[1]["hard_negative_categories_json"]
    group[0]["future_return"], group[1]["future_return"] = -999, 999
    for row in group:
        row.pop("hard_negative_categories_json", None)
    v2.add_hard_negatives([group], scales)
    assert group[1]["hard_negative_categories_json"] == categories


def test_entity_histories_are_strictly_prior(connection) -> None:
    assert connection.execute(
        "SELECT count(*) FROM risksets WHERE entity_history_feature_ns > decision_ns"
    ).fetchone()[0] == 0
    assert connection.execute(
        """
        SELECT count(*) FROM risksets
        WHERE creator_prior_launch_count < 0
           OR creator_prior_e4_selection_count_v2 < 0
        """
    ).fetchone()[0] == 0


def test_current_group_does_not_increment_its_own_entity_history(connection) -> None:
    first_decision = connection.execute(
        "SELECT min(decision_ns) FROM risksets"
    ).fetchone()[0]
    assert connection.execute(
        """
        SELECT count(*) FROM risksets
        WHERE decision_ns = ? AND creator_prior_e4_selection_count_v2 <> 0
        """,
        [first_decision],
    ).fetchone()[0] == 0


def test_relative_ranks_use_only_same_group(connection) -> None:
    group_id = connection.execute(
        "SELECT decision_group_id FROM risksets ORDER BY group_eligibility_counts[5] DESC LIMIT 1"
    ).fetchone()[0]
    rows = connection.execute(
        """
        SELECT candidate_age_ms,
               relative_features.launch_age[1],
               relative_features.launch_age[2]
        FROM risksets WHERE decision_group_id = ?
        """,
        [group_id],
    ).fetchall()
    unique = sorted({row[0] for row in rows})
    for age, rank, value_percentile in rows:
        assert rank == unique.index(age) + 1
        assert 0 <= value_percentile <= 1


def test_reserve_units_reproduce_observed_buys_within_tolerance(connection) -> None:
    count, within = connection.execute(
        """
        SELECT count(reserve_reproduction_error_bps),
               count(*) FILTER (WHERE reserve_reproduction_error_bps <= 500)
        FROM risksets
        WHERE selected_by_e4 AND selection_label = 'SELECTED_LANDED'
        """
    ).fetchone()
    assert count > 0
    assert within == count


def test_all_selection_exclusions_have_machine_readable_reason() -> None:
    path = ROOT / "artifacts/e4-v12-selection-exclusions-v2.jsonl"
    with path.open(encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle]
    assert rows
    assert all(row["reason_code"] and row["explanation"] for row in rows)


def test_jsonl_and_parquet_are_row_identical() -> None:
    connection = duckdb.connect()
    try:
        json_path = str(JSONL).replace("'", "''")
        parquet_path = str(PARQUET).replace("'", "''")
        json_relation = f"read_json_auto('{json_path}', format='newline_delimited')"
        parquet_relation = f"read_parquet('{parquet_path}')"
        json_count = connection.execute(f"SELECT count(*) FROM {json_relation}").fetchone()[0]
        parquet_count = connection.execute(
            f"SELECT count(*) FROM {parquet_relation}"
        ).fetchone()[0]
        assert json_count == parquet_count
        assert connection.execute(
            f"SELECT count(*) FROM ((SELECT * FROM {json_relation}) EXCEPT ALL "
            f"(SELECT * FROM {parquet_relation}))"
        ).fetchone()[0] == 0
        assert connection.execute(
            f"SELECT count(*) FROM ((SELECT * FROM {parquet_relation}) EXCEPT ALL "
            f"(SELECT * FROM {json_relation}))"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_committed_output_hashes_and_order_are_deterministic() -> None:
    coverage = json.loads(COVERAGE.read_text(encoding="utf-8"))
    expected = coverage["outputs"][JSONL.name]["sha256"]
    assert hashlib.sha256(JSONL.read_bytes()).hexdigest() == expected
    previous = None
    with JSONL.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            key = (
                row["decision_ns"],
                row["decision_group_id"],
                0 if row["selected_by_e4"] else 1,
                row["candidate_mint"],
            )
            assert previous is None or previous <= key
            previous = key


def test_production_v12_paths_are_untouched() -> None:
    result = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            v2.BASE_COMMIT,
            "--",
            "src/memecoin_bot",
            "models/e4",
            "tools/e4-builder",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == ""

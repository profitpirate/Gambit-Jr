from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts import e4_v12_canonical_choice_sets as choice


def event(
    received_ns: int,
    *,
    slot: int = 10,
    transaction_index: int | None = None,
    event_index: int = 0,
    signature: str = "sig",
) -> dict[str, object]:
    row: dict[str, object] = {
        "received_ns": received_ns,
        "slot": slot,
        "event_index": event_index,
        "signature": signature,
    }
    if transaction_index is not None:
        row["transaction_index"] = transaction_index
    return row


def launch(**overrides: object) -> choice.Launch:
    values: dict[str, object] = {
        "run_id": "1",
        "artifact_name": "artifact",
        "split": "train",
        "mint": "mint",
        "creator": "creator",
        "create_ns": 1_000,
        "create_slot": 10,
        "create_signature": "create",
        "create_event_id": 1,
        "create_event_index": 0,
        "raw": {},
        "create_transaction_index": -1,
    }
    values.update(overrides)
    return choice.Launch(**values)


def decision(**overrides: object) -> choice.Decision:
    values: dict[str, object] = {
        "run_id": "1",
        "artifact_name": "artifact",
        "split": "train",
        "label": "SELECTED_LANDED",
        "chosen_mint": "mint",
        "signature": "decision",
        "decision_ns": 2_000,
        "decision_slot": 10,
        "decision_event_id": 2,
        "decision_event_index": 0,
        "source_transaction_slot": 10,
        "source_transaction_index": 20,
        "decision_time_quality": "observed_landed_event_received_ns",
        "decision_ns_upper_bound": 2_000,
    }
    values.update(overrides)
    return choice.Decision(**values)


def test_received_ns_is_authoritative_before_transaction_index() -> None:
    earlier_receipt = event(100, transaction_index=999)
    later_receipt = event(101, transaction_index=1)
    assert choice.event_sort_key(earlier_receipt) < choice.event_sort_key(later_receipt)


def test_transaction_index_only_breaks_exact_receipt_ties() -> None:
    first = event(100, transaction_index=4)
    second = event(100, transaction_index=5)
    assert choice.event_sort_key(first) < choice.event_sort_key(second)


def test_future_event_is_never_known_at_decision() -> None:
    selected = launch()
    selected_decision = decision()
    assert not choice.event_known_at_decision(
        event(2_001, transaction_index=1), selected_decision, selected
    )
    assert choice.event_known_at_decision(
        event(2_000, transaction_index=19), selected_decision, selected
    )
    assert not choice.event_known_at_decision(
        event(2_000, transaction_index=21), selected_decision, selected
    )


def test_failed_fill_is_positive_selection_evidence() -> None:
    selected = launch()
    failed = decision(
        label="SELECTED_FILL_REJECTED",
        decision_ns=selected.create_ns,
        decision_time_quality="chosen_launch_received_ns_lower_bound",
        source_transaction_failed=True,
        output_guard_rejected=True,
        execution_outcome="OUTPUT_GUARD_REJECTED",
    )
    assert choice.launch_visible(selected, failed)
    assert failed.label == "SELECTED_FILL_REJECTED"
    assert failed.source_transaction_failed


def test_duplicate_mint_and_missing_chosen_are_integrity_errors() -> None:
    selected = decision()
    row = {
        "decision_group_id": selected.group_id,
        "candidate_mint": "other",
        "chosen_mint": "mint",
        "selected_by_e4": False,
        "decision_ns": 2_000,
        "split": "train",
    }
    ranges = {
        "train": {"minimum_received_ns": 1, "maximum_received_ns": 10},
        "validation": {"minimum_received_ns": 11, "maximum_received_ns": 20},
        "holdout": {"minimum_received_ns": 21, "maximum_received_ns": 30},
    }
    violations = choice.integrity_violations([row, row], [selected], [], ranges)
    codes = {item["code"] for item in violations}
    assert "DUPLICATE_MINT_IN_GROUP" in codes
    assert "CHOSEN_MINT_MISSING" in codes


def test_dense_ranks_use_only_supplied_candidates() -> None:
    rows = [
        {"candidate_mint": "a", "creator_seed_sol": 2.0},
        {"candidate_mint": "b", "creator_seed_sol": 1.0},
        {"candidate_mint": "c", "creator_seed_sol": 2.0},
    ]
    ranks = choice.dense_ranks(rows[:2], "creator_seed_sol", descending=True)
    assert ranks == {"a": 1, "b": 2}
    assert "c" not in ranks


def test_reserve_units_reproduce_observed_buy() -> None:
    source_sol = 1.0
    source_tokens = 1_000.0
    post_sol = 31.0
    post_tokens = 30_000.0
    row = {
        "sol_amount": source_sol,
        "token_amount": source_tokens,
        "raw": {
            "virtual_sol_reserves": int(post_sol * choice.LAMPORTS),
            "virtual_token_reserves": int(post_tokens * choice.TOKEN_SCALE),
            "real_token_reserves": int(10_000 * choice.TOKEN_SCALE),
        },
    }
    assert choice.reproduce_observed_buy(row) == 0.0


def test_split_boundaries_reject_temporal_leakage() -> None:
    ranges = {
        "train": {"minimum_received_ns": 1, "maximum_received_ns": 20},
        "validation": {"minimum_received_ns": 20, "maximum_received_ns": 30},
        "holdout": {"minimum_received_ns": 31, "maximum_received_ns": 40},
    }
    violations = choice.integrity_violations([], [], [], ranges)
    assert {item["code"] for item in violations} == {"TRAIN_VALIDATION_TIME_OVERLAP"}


def test_every_exclusion_requires_machine_reason() -> None:
    ranges = {
        "train": {"minimum_received_ns": 1, "maximum_received_ns": 10},
        "validation": {"minimum_received_ns": 11, "maximum_received_ns": 20},
        "holdout": {"minimum_received_ns": 21, "maximum_received_ns": 30},
    }
    violations = choice.integrity_violations(
        [], [], [{"signature": "sig", "reason_code": ""}], ranges
    )
    assert {item["code"] for item in violations} == {"EXCLUSION_WITHOUT_REASON"}


def test_second_resolution_exclusion_at_capture_boundary_is_not_misclassified() -> None:
    wallet = {
        "transactions": [
            {
                "signature": "failed",
                "detail_ok": True,
                "error": {"InstructionError": [3, "Custom"]},
                "blockTime": 10,
                "log_messages": ["Program log: Instruction: Buy"],
            }
        ]
    }
    exclusions, _ = choice.decision_exclusions(
        wallet,
        [],
        {"failed"},
        10_900_000_000,
        20_100_000_000,
    )
    assert exclusions[0]["reason_code"] == "TARGET_LAUNCH_OUTSIDE_AUTHORITATIVE_CAPTURE"


def test_decision_group_id_is_deterministic() -> None:
    assert decision().group_id == decision().group_id
    assert decision(signature="different").group_id != decision().group_id


def test_committed_canonical_artifacts_pass_integrity_gate() -> None:
    root = Path(__file__).resolve().parents[1]
    coverage_path = root / "artifacts" / "e4-v12-choice-set-coverage.json"
    jsonl_path = root / "artifacts" / "e4-v12-canonical-choice-sets.jsonl"
    parquet_path = root / "artifacts" / "e4-v12-canonical-choice-sets.parquet"
    assert coverage_path.is_file()
    assert jsonl_path.is_file()
    assert parquet_path.is_file()
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    assert (
        hashlib.sha256(jsonl_path.read_bytes()).hexdigest()
        == coverage["outputs"]["jsonl"]["sha256"]
    )
    assert (
        hashlib.sha256(parquet_path.read_bytes()).hexdigest()
        == coverage["outputs"]["parquet"]["sha256"]
    )
    assert coverage["integrity"]["status"] == "PASS"
    assert coverage["integrity"]["future_leakage_violations"] == 0
    assert coverage["integrity"]["ambiguous_labels"] == 0
    assert coverage["decision_groups"]["selected_landed"] == 116
    assert coverage["decision_groups"]["selected_fill_rejected"] == 206
    assert coverage["launch_labels"]["true_ignore"] == 35_678
    rows = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == coverage["decision_groups"]["rows"] == 496
    groups: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        groups.setdefault(str(row["decision_group_id"]), []).append(row)
        for field_name in choice.FEATURE_TIMESTAMP_FIELDS:
            timestamp = row.get(field_name)
            assert timestamp is None or int(timestamp) <= int(row["decision_ns"])
    assert len(groups) == 322
    assert all(
        len({row["candidate_mint"] for row in group}) == len(group) for group in groups.values()
    )
    assert all(sum(bool(row["selected_by_e4"]) for row in group) == 1 for group in groups.values())

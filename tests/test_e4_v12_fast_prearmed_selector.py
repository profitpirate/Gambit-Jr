from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import e4_v12_fast_prearmed_selector as subject


def decide(**overrides: object) -> bool:
    values = {
        "creator": "creator",
        "social_handle": "known",
        "social_status_ns": 1_000_000_000,
        "create_ns": 2_000_000_000,
        "prior_e4_attempts": 1,
        "creator_seed_sol": 2.0,
        "mayhem_mode": False,
        "creator_handles": {"creator": {"known"}},
    }
    values.update(overrides)
    return subject.should_select(**values)  # type: ignore[arg-type]


def test_selects_prearmed_repeat_creator_without_io() -> None:
    assert decide() is True


def test_rejects_unknown_handle_or_creator() -> None:
    assert decide(social_handle="other") is False
    assert decide(creator="other") is False


def test_rejects_future_or_stale_social_status() -> None:
    assert decide(social_status_ns=2_000_000_001) is False
    assert decide(social_status_ns=-9_000_000_001) is False
    assert decide(social_status_ns=-8_000_000_000) is True


def test_rejects_first_time_creator_and_mayhem() -> None:
    assert decide(prior_e4_attempts=0) is False
    assert decide(mayhem_mode=True) is False


def test_rejects_creator_seed_below_frozen_two_sol_floor() -> None:
    assert decide(creator_seed_sol=1.999999999) is False
    assert decide(creator_seed_sol=2.0) is True


def test_benchmark_contract_has_no_hot_path_io() -> None:
    result = subject.benchmark(1_000)
    assert result["hot_path_io_operations"] == 0
    assert result["hot_path_model_allocations"] == 0
    assert result["median_microseconds"] >= 0

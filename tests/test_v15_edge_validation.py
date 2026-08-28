from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memecoin_bot.historical.edge_validation import (
    SealedWindow,
    compare_predictions,
    validate_sealed_windows,
    wilson_interval,
)


def test_sealed_windows_reject_overlapping_maturity_periods():
    windows = [
        SealedWindow("one", "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"),
        SealedWindow("two", "2026-01-03T00:00:00+00:00", "2026-01-04T00:00:00+00:00"),
    ]
    validate_sealed_windows(windows, 24)
    with pytest.raises(ValueError, match="maturity leakage"):
        validate_sealed_windows(windows, 25)


def test_hard_gate_does_not_relabel_48_hour_results_as_seven_day_evidence():
    base = datetime(2026, 1, 1, tzinfo=UTC)
    windows = []
    rows = []
    predictions = {"CANDIDATE_V15": {}, "CONTROL_V15": {}}
    for window_index in range(3):
        start = base + timedelta(days=window_index * 4)
        end = start + timedelta(days=1)
        windows.append(SealedWindow(str(window_index), start.isoformat(), end.isoformat()))
        for index in range(100):
            mint = f"{window_index}-{index}"
            rows.append(
                {
                    "mint": mint,
                    "decision_at": (start + timedelta(minutes=index)).isoformat(),
                    "peak_24h": 2 if index < 80 else 1,
                    "peak_maturity": 2 if index < 80 else 1,
                    "terminal_failure": index >= 80,
                    "max_adverse_excursion": -0.1,
                    "market_cap_at_signal": 20_000,
                    "time_to_detection_seconds": 180,
                }
            )
            predictions["CANDIDATE_V15"][mint] = "PREMIUM" if index < 40 else "STRONG"
            predictions["CONTROL_V15"][mint] = "STRONG"

    result = compare_predictions(
        rows,
        predictions,
        windows,
        launch_count=30_000,
        maturity_hours_available=48,
    )
    assert result["models"]["CANDIDATE_V15"]["2x_maturity_precision"] == 0.8
    assert result["models"]["CANDIDATE_V15"]["matured_signals"] == 300
    assert result["acceptance"] == "FAIL TARGET"
    assert "SEVEN_DAY_MATURITY_UNAVAILABLE" in result["gate_failures"]


def test_wilson_interval_is_bounded_and_conservative():
    interval = wilson_interval(200, 250)
    assert interval is not None
    assert 0 < interval[0] < 0.8 < interval[1] < 1

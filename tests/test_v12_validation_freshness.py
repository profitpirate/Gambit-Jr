from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v12_validation_freshness import evaluate_runs


NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def run(
    run_id: int,
    *,
    status: str,
    conclusion: str | None,
    updated_at: str,
    branch: str = "main",
) -> dict:
    return {
        "id": run_id,
        "status": status,
        "conclusion": conclusion,
        "head_branch": branch,
        "event": "schedule",
        "created_at": updated_at,
        "updated_at": updated_at,
        "html_url": f"https://example.invalid/runs/{run_id}",
    }


def test_fresh_success_is_healthy() -> None:
    result = evaluate_runs(
        [
            run(
                1,
                status="completed",
                conclusion="success",
                updated_at="2026-09-25T10:00:00Z",
            )
        ],
        now=NOW,
        max_age=timedelta(hours=6),
        head_branch="main",
    )
    assert result["ok"] is True
    assert result["dispatch_recommended"] is False
    assert result["latest_success"]["id"] == 1


def test_stale_success_requests_recovery() -> None:
    result = evaluate_runs(
        [
            run(
                1,
                status="completed",
                conclusion="success",
                updated_at="2026-09-25T05:00:00Z",
            )
        ],
        now=NOW,
        max_age=timedelta(hours=6),
        head_branch="main",
    )
    assert result["ok"] is False
    assert result["reasons"] == ["latest_success_is_stale"]
    assert result["dispatch_recommended"] is True


def test_newer_hard_failure_is_unhealthy() -> None:
    result = evaluate_runs(
        [
            run(
                2,
                status="completed",
                conclusion="failure",
                updated_at="2026-09-25T11:00:00Z",
            ),
            run(
                1,
                status="completed",
                conclusion="success",
                updated_at="2026-09-25T10:00:00Z",
            ),
        ],
        now=NOW,
        max_age=timedelta(hours=6),
        head_branch="main",
    )
    assert result["ok"] is False
    assert result["newer_hard_failure"]["id"] == 2
    assert result["dispatch_recommended"] is True


def test_active_run_prevents_duplicate_recovery_dispatch() -> None:
    result = evaluate_runs(
        [
            run(
                2,
                status="in_progress",
                conclusion=None,
                updated_at="2026-09-25T11:55:00Z",
            ),
            run(
                1,
                status="completed",
                conclusion="success",
                updated_at="2026-09-25T04:00:00Z",
            ),
        ],
        now=NOW,
        max_age=timedelta(hours=6),
        head_branch="main",
    )
    assert result["ok"] is False
    assert result["active_run"]["id"] == 2
    assert result["dispatch_recommended"] is False


def test_cancelled_run_does_not_override_fresh_success() -> None:
    result = evaluate_runs(
        [
            run(
                2,
                status="completed",
                conclusion="cancelled",
                updated_at="2026-09-25T11:00:00Z",
            ),
            run(
                1,
                status="completed",
                conclusion="success",
                updated_at="2026-09-25T10:30:00Z",
            ),
        ],
        now=NOW,
        max_age=timedelta(hours=6),
        head_branch="main",
    )
    assert result["ok"] is True
    assert result["dispatch_recommended"] is False


def test_other_branch_runs_are_ignored() -> None:
    result = evaluate_runs(
        [
            run(
                9,
                status="completed",
                conclusion="failure",
                updated_at="2026-09-25T11:30:00Z",
                branch="feature/other",
            ),
            run(
                1,
                status="completed",
                conclusion="success",
                updated_at="2026-09-25T10:30:00Z",
                branch="main",
            ),
        ],
        now=NOW,
        max_age=timedelta(hours=6),
        head_branch="main",
    )
    assert result["ok"] is True
    assert result["matching_runs"] == 1

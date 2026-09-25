"""Freshness and self-healing guard for V12 NextGen GitHub validation."""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

HARD_FAILURES = {
    "failure",
    "timed_out",
    "action_required",
    "startup_failure",
}
ACTIVE_STATUSES = {"queued", "in_progress", "requested", "waiting", "pending"}


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = str(value).strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _run_time(run: dict[str, Any]) -> datetime:
    for key in ("updated_at", "run_started_at", "created_at"):
        parsed = parse_timestamp(run.get(key))
        if parsed is not None:
            return parsed
    return datetime.min.replace(tzinfo=timezone.utc)


def evaluate_runs(
    runs: list[dict[str, Any]],
    *,
    now: datetime,
    max_age: timedelta,
    head_branch: str | None = None,
) -> dict[str, Any]:
    now = now.astimezone(timezone.utc)
    relevant = [
        run
        for run in runs
        if not head_branch or str(run.get("head_branch") or "") == head_branch
    ]
    relevant.sort(key=_run_time, reverse=True)

    latest = relevant[0] if relevant else None
    successes = [
        run
        for run in relevant
        if str(run.get("status") or "") == "completed"
        and str(run.get("conclusion") or "") == "success"
    ]
    latest_success = successes[0] if successes else None
    latest_success_at = _run_time(latest_success) if latest_success else None
    age_seconds = (
        max(0.0, (now - latest_success_at).total_seconds())
        if latest_success_at is not None
        else None
    )
    stale = latest_success_at is None or age_seconds > max_age.total_seconds()

    newer_hard_failure = None
    if latest_success_at is not None:
        for run in relevant:
            if _run_time(run) <= latest_success_at:
                break
            if (
                str(run.get("status") or "") == "completed"
                and str(run.get("conclusion") or "") in HARD_FAILURES
            ):
                newer_hard_failure = run
                break
    else:
        newer_hard_failure = next(
            (
                run
                for run in relevant
                if str(run.get("status") or "") == "completed"
                and str(run.get("conclusion") or "") in HARD_FAILURES
            ),
            None,
        )

    active = next(
        (
            run
            for run in relevant
            if str(run.get("status") or "") in ACTIVE_STATUSES
        ),
        None,
    )

    reasons: list[str] = []
    if latest_success is None:
        reasons.append("no_successful_validation_found")
    elif stale:
        reasons.append("latest_success_is_stale")
    if newer_hard_failure is not None:
        reasons.append(
            "newer_hard_failure:"
            + str(newer_hard_failure.get("conclusion") or "unknown")
        )

    healthy = not reasons
    return {
        "ok": healthy,
        "reasons": reasons,
        "head_branch": head_branch,
        "max_age_seconds": int(max_age.total_seconds()),
        "matching_runs": len(relevant),
        "latest_run": {
            "id": latest.get("id") if latest else None,
            "status": latest.get("status") if latest else None,
            "conclusion": latest.get("conclusion") if latest else None,
            "event": latest.get("event") if latest else None,
            "created_at": latest.get("created_at") if latest else None,
            "updated_at": latest.get("updated_at") if latest else None,
            "html_url": latest.get("html_url") if latest else None,
        },
        "latest_success": {
            "id": latest_success.get("id") if latest_success else None,
            "updated_at": latest_success.get("updated_at") if latest_success else None,
            "html_url": latest_success.get("html_url") if latest_success else None,
            "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
        },
        "newer_hard_failure": {
            "id": newer_hard_failure.get("id") if newer_hard_failure else None,
            "conclusion": (
                newer_hard_failure.get("conclusion")
                if newer_hard_failure
                else None
            ),
            "html_url": (
                newer_hard_failure.get("html_url")
                if newer_hard_failure
                else None
            ),
        },
        "active_run": {
            "id": active.get("id") if active else None,
            "status": active.get("status") if active else None,
            "html_url": active.get("html_url") if active else None,
        },
        "dispatch_recommended": (not healthy and active is None),
    }


def _api_json(url: str, token: str) -> dict[str, Any]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "gambit-v12-validation-watchdog",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"GitHub API HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"GitHub API unavailable: {exc.reason}") from exc


def fetch_runs(
    *,
    repository: str,
    workflow: str,
    token: str,
    per_page: int = 50,
) -> list[dict[str, Any]]:
    query = urlencode({"per_page": max(1, min(100, int(per_page)))})
    url = (
        f"https://api.github.com/repos/{repository}/actions/workflows/"
        f"{quote(workflow, safe='')}/runs?{query}"
    )
    payload = _api_json(url, token)
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise RuntimeError("GitHub workflow-runs response is missing workflow_runs")
    return [run for run in runs if isinstance(run, dict)]


def dispatch_workflow(
    *,
    repository: str,
    workflow: str,
    ref: str,
    token: str,
) -> None:
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required for recovery dispatch")
    url = (
        f"https://api.github.com/repos/{repository}/actions/workflows/"
        f"{quote(workflow, safe='')}/dispatches"
    )
    body = json.dumps({"ref": ref}, separators=(",", ":")).encode("utf-8")
    request = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "gambit-v12-validation-watchdog",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            if response.status != 204:
                raise RuntimeError(
                    f"unexpected workflow-dispatch status {response.status}"
                )
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(
            f"workflow recovery dispatch failed HTTP {exc.code}: {detail}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(
            f"workflow recovery dispatch unavailable: {exc.reason}"
        ) from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repository",
        default=os.getenv("GITHUB_REPOSITORY", ""),
        help="GitHub repository in owner/name form",
    )
    parser.add_argument(
        "--workflow",
        default="v12-nextgen-concurrent-validation.yml",
    )
    parser.add_argument("--head-branch", default="main")
    parser.add_argument("--max-age-hours", type=float, default=6.0)
    parser.add_argument("--per-page", type=int, default=50)
    parser.add_argument("--output")
    parser.add_argument("--dispatch-if-unhealthy", action="store_true")
    parser.add_argument("--dispatch-ref", default="main")
    args = parser.parse_args()

    if "/" not in args.repository:
        parser.error("--repository must be owner/name")
    if args.max_age_hours <= 0:
        parser.error("--max-age-hours must be positive")

    token = os.getenv("GITHUB_TOKEN", "")
    runs = fetch_runs(
        repository=args.repository,
        workflow=args.workflow,
        token=token,
        per_page=args.per_page,
    )
    result = evaluate_runs(
        runs,
        now=datetime.now(timezone.utc),
        max_age=timedelta(hours=args.max_age_hours),
        head_branch=args.head_branch or None,
    )
    result["repository"] = args.repository
    result["workflow"] = args.workflow
    result["recovery_dispatch"] = "not_requested"

    if (
        args.dispatch_if_unhealthy
        and result["dispatch_recommended"]
    ):
        dispatch_workflow(
            repository=args.repository,
            workflow=args.workflow,
            ref=args.dispatch_ref,
            token=token,
        )
        result["recovery_dispatch"] = "requested"

    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

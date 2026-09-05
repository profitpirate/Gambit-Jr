#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import subprocess
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

AUDIT_MARKER = "[lifetime-audit]"
USER_EMAILS = {"joshua.ayorinde@yahoo.com"}
USER_NAMES = {"profitpirate"}
BOT_NAME_PREFIXES = (
    "gambit-",
    "github-actions",
    "dependabot",
    "web-flow",
)


@dataclass(frozen=True, slots=True)
class Commit:
    sha: str
    authored_at: datetime
    name: str
    email: str
    subject: str


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def run_text(*args: str) -> str:
    return subprocess.check_output(args, text=True, encoding="utf-8").strip()


def load_commits() -> list[Commit]:
    separator = "\x1f"
    record_separator = "\x1e"
    output = run_text(
        "git",
        "log",
        "--all",
        f"--format=%H{separator}%aI{separator}%an{separator}%ae{separator}%s{record_separator}",
    )
    commits: dict[str, Commit] = {}
    for record in output.split(record_separator):
        record = record.strip()
        if not record:
            continue
        fields = record.split(separator)
        if len(fields) < 5:
            continue
        sha, timestamp, name, email = fields[:4]
        subject = separator.join(fields[4:]).strip()
        if AUDIT_MARKER in subject:
            continue
        authored_at = parse_time(timestamp)
        if authored_at is None:
            continue
        commits[sha] = Commit(
            sha=sha,
            authored_at=authored_at,
            name=name.strip(),
            email=email.strip(),
            subject=subject,
        )
    return sorted(commits.values(), key=lambda item: item.authored_at)


def is_user_commit(commit: Commit) -> bool:
    return (
        commit.email.lower() in USER_EMAILS
        or commit.name.strip().lower() in USER_NAMES
    )


def is_bot_commit(commit: Commit) -> bool:
    name = commit.name.strip().lower()
    email = commit.email.strip().lower()
    return (
        name.startswith(BOT_NAME_PREFIXES)
        or "actions@users.noreply.github.com" in email
        or "noreply@github.com" in email
        or "bot" in email
    ) and not is_user_commit(commit)


def session_estimate(
    timestamps: Iterable[datetime],
    *,
    max_gap_hours: float,
    terminal_hours: float,
) -> dict[str, Any]:
    times = sorted(timestamps)
    if not times:
        return {
            "hours": 0.0,
            "sessions": 0,
            "max_gap_hours": max_gap_hours,
            "terminal_hours_per_session": terminal_hours,
        }
    cutoff = max_gap_hours * 3600.0
    total_seconds = 0.0
    sessions = 1
    session_lengths: list[float] = []
    current_seconds = 0.0
    for previous, current in zip(times, times[1:]):
        gap = max(0.0, (current - previous).total_seconds())
        if gap <= cutoff:
            total_seconds += gap
            current_seconds += gap
        else:
            total_seconds += terminal_hours * 3600.0
            current_seconds += terminal_hours * 3600.0
            session_lengths.append(current_seconds / 3600.0)
            current_seconds = 0.0
            sessions += 1
    total_seconds += terminal_hours * 3600.0
    current_seconds += terminal_hours * 3600.0
    session_lengths.append(current_seconds / 3600.0)
    return {
        "hours": round(total_seconds / 3600.0, 3),
        "sessions": sessions,
        "max_gap_hours": max_gap_hours,
        "terminal_hours_per_session": terminal_hours,
        "median_session_hours": round(statistics.median(session_lengths), 3),
        "longest_session_hours": round(max(session_lengths), 3),
    }


def github_json(url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Gambit-Jr-lifetime-audit/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def load_workflow_runs(repo: str, token: str) -> tuple[int, list[dict[str, Any]]]:
    runs: list[dict[str, Any]] = []
    total_count = 0
    for page in range(1, 100):
        query = urllib.parse.urlencode({"per_page": 100, "page": page})
        payload = github_json(
            f"https://api.github.com/repos/{repo}/actions/runs?{query}", token
        )
        total_count = int(payload.get("total_count") or total_count)
        batch = payload.get("workflow_runs") or []
        if not isinstance(batch, list):
            break
        runs.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 100:
            break
    return total_count, runs


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def workflow_metrics(total_count: int, runs: list[dict[str, Any]]) -> dict[str, Any]:
    intervals: list[tuple[datetime, datetime]] = []
    durations: list[float] = []
    conclusions: Counter[str] = Counter()
    workflows: Counter[str] = Counter()
    for run in runs:
        conclusions[str(run.get("conclusion") or run.get("status") or "unknown")] += 1
        workflows[str(run.get("name") or "unknown")] += 1
        start = parse_time(run.get("run_started_at") or run.get("created_at"))
        end = parse_time(run.get("updated_at"))
        if start is None or end is None or end < start:
            continue
        seconds = (end - start).total_seconds()
        # Guard against stale metadata while retaining legitimate long captures.
        if seconds > 48 * 3600:
            continue
        durations.append(seconds)
        intervals.append((start, end))

    merged: list[list[datetime]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        elif end > merged[-1][1]:
            merged[-1][1] = end
    union_seconds = sum((end - start).total_seconds() for start, end in merged)
    duration_hours = [seconds / 3600.0 for seconds in durations]
    return {
        "total_runs_reported": total_count,
        "runs_retrieved": len(runs),
        "runs_with_duration": len(durations),
        "cumulative_run_wall_hours": round(sum(duration_hours), 3),
        "non_overlapping_automation_coverage_hours": round(union_seconds / 3600.0, 3),
        "median_run_minutes": round(statistics.median(durations) / 60.0, 3)
        if durations
        else 0.0,
        "p90_run_minutes": round(percentile(durations, 0.90) / 60.0, 3),
        "longest_run_hours": round(max(duration_hours), 3) if duration_hours else 0.0,
        "conclusions": dict(conclusions),
        "distinct_workflow_names": len(workflows),
        "top_workflows": workflows.most_common(15),
        "metric_note": (
            "Cumulative workflow wall time is a lower-bound automation metric; "
            "multi-job workflows can consume more runner time than their wall duration."
        ),
    }


def count_remote_branches() -> int:
    output = run_text(
        "git", "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin"
    )
    names = {
        line.strip()
        for line in output.splitlines()
        if line.strip() and line.strip() != "origin/HEAD"
    }
    return len(names)


def phase_counts(commits: list[Commit]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for commit in commits:
        text = commit.subject.lower()
        if "e4-v12" in text or "v12" in text:
            counts["V12 / golden-thesis / role-model"] += 1
        elif "e4-v11" in text or "v11" in text:
            counts["V11"] += 1
        elif "e4-v10" in text or "v10" in text:
            counts["V10 / three pipelines"] += 1
        elif "e4" in text:
            counts["E4 engine and execution"] += 1
        elif any(version in text for version in ("v1.5", "v1.4", "v1.3", "v1.2", "v1.1")):
            counts["Gambit Jr V1.x product"] += 1
        else:
            counts["General platform / Discord / infrastructure"] += 1
    return dict(counts)


def build_report(repo: str, token: str | None) -> dict[str, Any]:
    commits = load_commits()
    user_commits = [item for item in commits if is_user_commit(item)]
    bot_commits = [item for item in commits if is_bot_commit(item)]
    other_commits = [
        item for item in commits if item not in user_commits and item not in bot_commits
    ]
    user_times = [item.authored_at for item in user_commits]

    estimates = {
        "conservative": session_estimate(
            user_times, max_gap_hours=1.0, terminal_hours=0.5
        ),
        "central": session_estimate(
            user_times, max_gap_hours=2.0, terminal_hours=1.0
        ),
        "upper": session_estimate(
            user_times, max_gap_hours=4.0, terminal_hours=1.5
        ),
    }
    # Git history cannot represent architecture discussions, manual transaction
    # forensics, review, monitoring, failed local attempts, or work completed
    # before the first repository commit. Keep that adjustment explicit.
    non_commit_adjustments = {
        "conservative": {
            "pre_repository_hours": 2.0,
            "planning_research_review_multiplier": 1.15,
        },
        "central": {
            "pre_repository_hours": 5.0,
            "planning_research_review_multiplier": 1.25,
        },
        "upper": {
            "pre_repository_hours": 8.0,
            "planning_research_review_multiplier": 1.35,
        },
    }
    active_effort: dict[str, float] = {}
    for label, estimate in estimates.items():
        adjustment = non_commit_adjustments[label]
        active_effort[label] = round(
            estimate["hours"]
            * float(adjustment["planning_research_review_multiplier"])
            + float(adjustment["pre_repository_hours"]),
            3,
        )

    earliest = commits[0].authored_at if commits else None
    latest = commits[-1].authored_at if commits else None
    calendar_hours = (
        (latest - earliest).total_seconds() / 3600.0
        if earliest is not None and latest is not None
        else 0.0
    )
    per_day = Counter(item.authored_at.date().isoformat() for item in user_commits)

    actions = (
        workflow_metrics(*load_workflow_runs(repo, token))
        if token
        else {"unavailable": "GITHUB_TOKEN was not provided"}
    )

    central = active_effort["central"]
    combined_lower_bound = central + float(actions.get("cumulative_run_wall_hours") or 0.0)
    return {
        "version": "gambit-jr-lifetime-effort-audit-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "repository": repo,
        "scope": "all fetched repository branches and workflow runs",
        "repository_history": {
            "earliest_commit": earliest.isoformat() if earliest else None,
            "latest_commit": latest.isoformat() if latest else None,
            "calendar_lifetime_hours_between_commits": round(calendar_hours, 3),
            "calendar_lifetime_days_between_commits": round(calendar_hours / 24.0, 3),
            "remote_branches": count_remote_branches(),
            "unique_commits_all_branches": len(commits),
            "user_authored_commits": len(user_commits),
            "automation_authored_commits": len(bot_commits),
            "other_authored_commits": len(other_commits),
            "active_user_commit_dates": len(per_day),
            "user_commits_by_date": dict(sorted(per_day.items())),
            "commit_phase_counts": phase_counts(commits),
        },
        "git_session_models": estimates,
        "non_commit_adjustments": non_commit_adjustments,
        "estimated_active_research_and_development_hours": active_effort,
        "recommended_lifetime_active_hours": round(central),
        "github_actions": actions,
        "combined_active_plus_cumulative_workflow_wall_hours_lower_bound": round(
            combined_lower_bound, 3
        ),
        "interpretation": {
            "recommended_number": (
                "Use recommended_lifetime_active_hours for human/AI-guided research "
                "and bot-development effort."
            ),
            "automation": (
                "Do not describe cumulative workflow wall hours as human labour; it is "
                "unattended/automated validation and live-capture runtime."
            ),
            "uncertainty": (
                "Commit-derived hours are an estimate. The conservative-to-upper range "
                "is more defensible than false minute-level precision."
            ),
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    history = report["repository_history"]
    active = report["estimated_active_research_and_development_hours"]
    actions = report["github_actions"]
    return f"""# Gambit Jr lifetime effort audit

Generated: `{report['generated_at']}`

## Recommended total

**{report['recommended_lifetime_active_hours']} active hours** of research and bot development.

Defensible range: **{round(active['conservative'])}–{round(active['upper'])} hours**.

## Repository evidence

- First commit: `{history['earliest_commit']}`
- Latest audited commit: `{history['latest_commit']}`
- Calendar lifetime: **{history['calendar_lifetime_days_between_commits']} days**
- Remote branches: **{history['remote_branches']}**
- Unique commits across all branches: **{history['unique_commits_all_branches']}**
- User-authored commits: **{history['user_authored_commits']}**
- Automation-authored commits: **{history['automation_authored_commits']}**
- Active commit dates: **{history['active_user_commit_dates']}**

## Git-session estimates

- Conservative: **{active['conservative']} h**
- Central: **{active['central']} h**
- Upper: **{active['upper']} h**

These totals include an explicit allowance for architecture discussion, manual E4 transaction analysis, review, monitoring, failed attempts and work completed before the first commit.

## Automated validation

- Workflow runs: **{actions.get('total_runs_reported', 'unavailable')}**
- Cumulative workflow wall time: **{actions.get('cumulative_run_wall_hours', 'unavailable')} h**
- Non-overlapping automation coverage: **{actions.get('non_overlapping_automation_coverage_hours', 'unavailable')} h**
- Distinct workflow names: **{actions.get('distinct_workflow_names', 'unavailable')}**

Automation time is reported separately and is not human labour. Multi-job workflows can consume more runner time than the wall-time figure.
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", "profitpirate/Gambit-Jr"))
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    args = parser.parse_args()

    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    report = build_report(args.repo, token)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    args.markdown_output.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

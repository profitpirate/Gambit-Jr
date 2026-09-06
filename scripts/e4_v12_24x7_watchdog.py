#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

ACTIVE_STATUSES = {"queued", "in_progress", "waiting", "requested", "pending"}
FAILURE_CONCLUSIONS = {
    "failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale",
}
FOUND_VALUES = {
    "FOUND", "LIVE_CONFIRMED", "FOUND_LIVE_SUB10MS_GOLDEN_THESIS",
    "HISTORICAL_AND_LIVE_CONFIRMED",
}


@dataclass(frozen=True, slots=True)
class Target:
    workflow: str
    ref: str
    role: str
    priority: int
    success_cooldown_minutes: int
    failure_backoff_minutes: int
    stale_active_minutes: int
    stale_queue_minutes: int = 60


@dataclass(slots=True)
class Action:
    workflow: str
    ref: str
    role: str
    decision: str
    reason: str
    latest_run_id: int | None = None
    latest_status: str | None = None
    latest_conclusion: str | None = None
    latest_head_sha: str | None = None
    branch_head_sha: str | None = None
    age_minutes: float | None = None
    cancelled_run_ids: list[int] | None = None
    dispatched: bool = False
    error: str | None = None


TARGETS: tuple[Target, ...] = (
    Target("e4-v12-selection-certification.yml", "codex/e4-v12-selection-reconstruction", "fresh-evidence", 0, 240, 20, 420),
    Target("e4-v12-golden-master.yml", "codex/e4-v12-sub10ms-entry-research-20260905", "parallel-master", 1, 45, 20, 600),
    Target("e4-v12-failed-aware-v3-golden.yml", "codex/e4-v12-sub10ms-entry-research-20260905", "failed-aware", 2, 60, 20, 600),
    Target("e4-v12-golden-multithesis-v5.yml", "codex/e4-v12-sub10ms-entry-research-20260905", "multi-thesis", 3, 120, 30, 600),
    Target("e4-v12-bayesian-source-golden.yml", "codex/e4-v12-sub10ms-entry-research-20260905", "bayesian-source", 4, 180, 30, 600),
    Target("e4-v12-buyer-cluster-golden.yml", "codex/e4-v12-sub10ms-entry-research-20260905", "buyer-cluster", 5, 180, 30, 600),
    Target("e4-v12-recurrence-golden-v3.yml", "codex/e4-v12-sub10ms-entry-research-20260905", "recurrence-shape", 6, 180, 30, 600),
    Target("e4-v12-prior-whitelist-golden.yml", "codex/e4-v12-sub10ms-entry-research-20260905", "prior-whitelist", 7, 240, 45, 600),
    Target("e4-v12-prelaunch-social-golden.yml", "codex/e4-v12-sub10ms-entry-research-20260905", "prelaunch-social", 8, 240, 45, 600),
    Target("e4-v12-reactive-confidence-golden.yml", "codex/e4-v12-sub10ms-entry-research-20260905", "reactive-confidence", 9, 240, 45, 600),
    Target("e4-v12-reactive-independent-golden.yml", "codex/e4-v12-sub10ms-entry-research-20260905", "reactive-independent", 10, 240, 45, 600),
    Target("e4-v12-reactive-lattice-golden.yml", "codex/e4-v12-sub10ms-entry-research-20260905", "reactive-lattice", 11, 240, 45, 600),
    Target("e4-v12-reactive-profit-golden.yml", "codex/e4-v12-sub10ms-entry-research-20260905", "reactive-profit", 12, 240, 45, 600),
    Target("e4-v12-market-golden.yml", "codex/e4-v12-sub10ms-entry-research-20260905", "market-only", 13, 360, 60, 600),
    Target("e4-v12-social-golden.yml", "codex/e4-v12-sub10ms-entry-research-20260905", "social", 14, 360, 60, 600),
    Target("e4-v12-social-whitelist-golden.yml", "codex/e4-v12-sub10ms-entry-research-20260905", "social-whitelist", 15, 360, 60, 600),
)


class GitHubAPI:
    def __init__(self, repo: str, token: str) -> None:
        self.repo = repo
        self.token = token
        self.base = f"https://api.github.com/repos/{repo}"

    def request(self, method: str, endpoint: str, payload: Mapping[str, Any] | None = None, *, allow_not_found: bool = False) -> Any:
        url = endpoint if endpoint.startswith("https://") else f"{self.base}{endpoint}"
        data = None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Gambit-Jr-E4-watchdog/1.0",
        }
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
                return json.loads(body.decode("utf-8")) if body else None
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if allow_not_found and exc.code == 404:
                return None
            raise RuntimeError(f"GitHub API {method} {endpoint} failed: HTTP {exc.code}: {body[:500]}") from exc

    def get(self, endpoint: str, *, allow_not_found: bool = False) -> Any:
        return self.request("GET", endpoint, allow_not_found=allow_not_found)

    def post(self, endpoint: str, payload: Mapping[str, Any] | None = None) -> Any:
        return self.request("POST", endpoint, payload)

    def branch_head(self, ref: str) -> str:
        payload = self.get(f"/branches/{urllib.parse.quote(ref, safe='')}")
        return str(((payload or {}).get("commit") or {}).get("sha") or "")

    def file_text(self, path: str, ref: str) -> str | None:
        encoded_path = urllib.parse.quote(path, safe="/")
        query = urllib.parse.urlencode({"ref": ref})
        payload = self.get(f"/contents/{encoded_path}?{query}", allow_not_found=True)
        if not payload:
            return None
        encoded = str(payload.get("content") or "").replace("\n", "")
        return base64.b64decode(encoded).decode("utf-8", errors="replace") if encoded else ""

    def workflow_runs(self, workflow: str, ref: str) -> list[dict[str, Any]]:
        encoded = urllib.parse.quote(workflow, safe="")
        query = urllib.parse.urlencode({"branch": ref, "per_page": 20, "exclude_pull_requests": "true"})
        payload = self.get(f"/actions/workflows/{encoded}/runs?{query}")
        return [dict(row) for row in ((payload or {}).get("workflow_runs") or []) if isinstance(row, Mapping)]

    def dispatch(self, workflow: str, ref: str) -> None:
        encoded = urllib.parse.quote(workflow, safe="")
        self.post(f"/actions/workflows/{encoded}/dispatches", {"ref": ref})

    def cancel(self, run_id: int) -> None:
        self.post(f"/actions/runs/{run_id}/cancel")


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def age_minutes(value: Any, now: datetime) -> float | None:
    parsed = parse_time(value)
    return max(0.0, (now - parsed).total_seconds() / 60.0) if parsed else None


def is_found(value: str | None) -> bool:
    normalized = str(value or "").strip().upper()
    return normalized in FOUND_VALUES or normalized.startswith("FOUND_")


def latest_successful_evidence_time(api: GitHubAPI, evidence_target: Target) -> datetime | None:
    try:
        runs = api.workflow_runs(evidence_target.workflow, evidence_target.ref)
    except RuntimeError:
        return None
    for row in runs:
        if str(row.get("status") or "") == "completed" and str(row.get("conclusion") or "") == "success":
            return parse_time(row.get("updated_at") or row.get("created_at"))
    return None


def decide_target(api: GitHubAPI, target: Target, *, now: datetime, evidence_time: datetime | None, dry_run: bool) -> Action:
    action = Action(target.workflow, target.ref, target.role, "observe", "uninitialised", cancelled_run_ids=[])
    try:
        branch_head = api.branch_head(target.ref)
        action.branch_head_sha = branch_head
        runs = api.workflow_runs(target.workflow, target.ref)
    except Exception as exc:
        action.decision = "error"
        action.reason = "unable to inspect workflow"
        action.error = str(exc)
        return action

    if not runs:
        action.decision = "dispatch"
        action.reason = "workflow has no run on target branch"
        return action

    active = [row for row in runs if str(row.get("status") or "") in ACTIVE_STATUSES]
    if active:
        active.sort(key=lambda row: parse_time(row.get("created_at")) or datetime.min.replace(tzinfo=UTC), reverse=True)
        newest = active[0]
        status = str(newest.get("status") or "")
        created_age = age_minutes(newest.get("created_at"), now)
        action.latest_run_id = int(newest.get("id") or 0) or None
        action.latest_status = status
        action.latest_conclusion = str(newest.get("conclusion") or "") or None
        action.latest_head_sha = str(newest.get("head_sha") or "") or None
        action.age_minutes = created_age
        stale_limit = target.stale_queue_minutes if status in {"queued", "waiting", "requested", "pending"} else target.stale_active_minutes
        if created_age is not None and created_age > stale_limit:
            action.decision = "restart"
            action.reason = f"{status} run exceeded {stale_limit} minute stale limit"
            for row in active:
                run_id = int(row.get("id") or 0)
                if run_id <= 0:
                    continue
                if not dry_run:
                    try:
                        api.cancel(run_id)
                    except Exception as exc:
                        action.error = (f"{action.error}; " if action.error else "") + f"cancel {run_id}: {exc}"
                        continue
                action.cancelled_run_ids.append(run_id)
            return action
        action.decision = "healthy"
        action.reason = f"{status} run is within its health window"
        return action

    completed = [row for row in runs if str(row.get("status") or "") == "completed"]
    latest = completed[0] if completed else runs[0]
    latest_updated = parse_time(latest.get("updated_at") or latest.get("created_at"))
    latest_age = age_minutes(latest.get("updated_at") or latest.get("created_at"), now)
    conclusion = str(latest.get("conclusion") or "")
    run_head = str(latest.get("head_sha") or "")
    action.latest_run_id = int(latest.get("id") or 0) or None
    action.latest_status = str(latest.get("status") or "") or None
    action.latest_conclusion = conclusion or None
    action.latest_head_sha = run_head or None
    action.age_minutes = latest_age

    if branch_head and run_head and branch_head != run_head:
        action.decision = "dispatch"
        action.reason = "target branch has newer research code"
        return action
    if target.role != "fresh-evidence" and evidence_time and latest_updated and evidence_time > latest_updated:
        action.decision = "dispatch"
        action.reason = "new immutable live evidence arrived after the last run"
        return action
    if conclusion in FAILURE_CONCLUSIONS or conclusion not in {"success", "neutral", "skipped"}:
        if latest_age is None or latest_age >= target.failure_backoff_minutes:
            action.decision = "dispatch"
            action.reason = f"latest run concluded {conclusion or 'unknown'} and backoff elapsed"
        else:
            action.decision = "cooldown"
            action.reason = f"latest {conclusion or 'unknown'} run is inside {target.failure_backoff_minutes} minute backoff"
        return action
    if latest_age is None or latest_age >= target.success_cooldown_minutes:
        action.decision = "dispatch"
        action.reason = f"latest completed run is older than {target.success_cooldown_minutes} minute research cadence"
    else:
        action.decision = "healthy"
        action.reason = f"recent completed run is inside {target.success_cooldown_minutes} minute cadence"
    return action


def main() -> int:
    parser = argparse.ArgumentParser(description="Self-healing 10-minute E4 V12 research watchdog")
    parser.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-dispatches", type=int, default=int(os.getenv("E4_WATCHDOG_MAX_DISPATCHES", "6")))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if not args.repo:
        raise SystemExit("--repo or GITHUB_REPOSITORY is required")
    if not token:
        raise SystemExit("GH_TOKEN or GITHUB_TOKEN is required")

    now = datetime.now(UTC)
    api = GitHubAPI(args.repo, token)
    research_ref = "codex/e4-v12-sub10ms-entry-research-20260905"
    sentinel_paths = ("research/e4-v12-golden-sentinel.txt", "research/e4-v12-golden-authoritative-sentinel.txt")
    sentinel_values: dict[str, str | None] = {}
    for path in sentinel_paths:
        try:
            sentinel_values[path] = api.file_text(path, research_ref)
        except Exception as exc:
            sentinel_values[path] = f"ERROR: {exc}"
    found = any(is_found(value) for value in sentinel_values.values())
    report: dict[str, Any] = {
        "version": "e4-v12-24x7-watchdog-v1",
        "checked_at": now.isoformat(),
        "repository": args.repo,
        "research_ref": research_ref,
        "interval_minutes": 10,
        "found": found,
        "sentinels": sentinel_values,
        "max_dispatches_per_tick": max(0, args.max_dispatches),
        "dry_run": bool(args.dry_run),
        "actions": [],
    }
    if found:
        report["state"] = "GOLDEN_THESIS_FOUND"
        report["message"] = "Sentinel is confirmed; no further research was launched."
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    evidence_time = latest_successful_evidence_time(api, TARGETS[0])
    report["latest_successful_evidence_at"] = evidence_time.isoformat() if evidence_time else None
    actions = [decide_target(api, target, now=now, evidence_time=evidence_time, dry_run=args.dry_run) for target in sorted(TARGETS, key=lambda item: item.priority)]
    dispatch_budget = max(0, args.max_dispatches)
    for action in actions:
        if action.decision not in {"dispatch", "restart"}:
            continue
        if dispatch_budget <= 0:
            action.decision = "deferred"
            action.reason += "; per-tick dispatch budget exhausted"
            continue
        try:
            if not args.dry_run:
                api.dispatch(action.workflow, action.ref)
            action.dispatched = True
            dispatch_budget -= 1
        except Exception as exc:
            action.error = str(exc)
            action.dispatched = False
            action.decision = "error"

    report["actions"] = [asdict(action) for action in actions]
    report["dispatches"] = sum(action.dispatched for action in actions)
    report["active_or_healthy"] = sum(action.decision in {"healthy", "cooldown"} for action in actions)
    report["errors"] = [{"workflow": action.workflow, "error": action.error} for action in actions if action.error]
    report["state"] = "RESEARCH_ACTIVE" if report["dispatches"] or report["active_or_healthy"] else "RESEARCH_REQUIRES_ATTENTION"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

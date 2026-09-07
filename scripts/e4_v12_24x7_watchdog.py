#!/usr/bin/env python3
"""Novelty-aware ten-minute watchdog for E4 V12 research."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

if __package__:
    from scripts import e4_v12_experiment_registry as registry
else:
    import e4_v12_experiment_registry as registry

ACTIVE_STATUSES = {"queued", "in_progress", "waiting", "requested", "pending"}
INFRASTRUCTURE_CONCLUSIONS = {
    "cancelled",
    "timed_out",
    "action_required",
    "startup_failure",
    "stale",
}
SUCCESS_CONCLUSIONS = {"success", "neutral", "skipped"}
RESEARCH_REF = "codex/e4-v12-sub10ms-entry-research-20260905"
INTEGRITY_REF = "codex/e4-v12-canonical-choice-set"
INTEGRITY_PATH = "artifacts/e4-v12-choice-set-coverage.json"
EVIDENCE_EPOCH_PATH = "models/e4/e4-v12-evidence-epoch.txt"
EVIDENCE_DATA_PATH = "models/e4/e4-v12-forward-evidence.json"
LIVE_VERDICT_PATHS = (
    "docs/research/e4-v12-golden-master-live-verdict.json",
    "docs/research/e4-v12-golden-live-verdict.json",
)
SENTINEL_PATHS = (
    "research/e4-v12-golden-sentinel.txt",
    "research/e4-v12-golden-authoritative-sentinel.txt",
)
GOLDEN_VALUES = {
    "LIVE_CONFIRMED",
    "FOUND_LIVE_SUB10MS_GOLDEN_THESIS",
    "HISTORICAL_AND_LIVE_CONFIRMED",
    "UNTOUCHED_LIVE_PASSED",
}
WATCHDOG_ERRORS = (
    RuntimeError,
    OSError,
    ValueError,
    TypeError,
    json.JSONDecodeError,
    TimeoutError,
)


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
    thesis_family: str | None = None
    model_family: str | None = None
    feature_contract: str | None = None
    causal_horizon: str = "workflow-defined-causal-horizon"
    candidate_risk_set_policy: str = "workflow-defined-causal-risk-set"
    status_path: str | None = None

    @property
    def scientific(self) -> bool:
        return self.role != "fresh-evidence"


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
    experiment_id: str | None = None
    thesis_family: str | None = None
    failure_classification: str | None = None
    novelty_priority: int | None = None


@dataclass(frozen=True, slots=True)
class ExperimentSpec:
    identity: dict[str, Any]
    dataset_version: str
    dataset_source_manifest: dict[str, Any]


def research_target(
    workflow: str,
    role: str,
    priority: int,
    *,
    cooldown: int,
    backoff: int,
    thesis_family: str,
    model_family: str,
    feature_contract: str,
    status_path: str | None = None,
) -> Target:
    return Target(
        workflow,
        RESEARCH_REF,
        role,
        priority,
        cooldown,
        backoff,
        600,
        thesis_family=thesis_family,
        model_family=model_family,
        feature_contract=feature_contract,
        causal_horizon="0/1/2/5/10ms causal execution replay",
        candidate_risk_set_policy="frozen chronological launch risk sets",
        status_path=status_path,
    )


TARGETS: tuple[Target, ...] = (
    Target(
        "e4-v12-selection-certification.yml",
        "codex/e4-v12-selection-reconstruction",
        "fresh-evidence",
        0,
        240,
        20,
        420,
    ),
    research_target(
        "e4-v12-golden-master.yml",
        "parallel-master",
        1,
        cooldown=45,
        backoff=20,
        thesis_family="parallel-master",
        model_family="multi-family tournament",
        feature_contract="failed-preimpact, clustered-preimpact, reactive-lattice, reactive-independent",
    ),
    research_target(
        "e4-v12-failed-aware-v3-golden.yml",
        "failed-aware-v3",
        2,
        cooldown=60,
        backoff=20,
        thesis_family="failed-aware-preimpact-v3",
        model_family="failed-aware causal selector",
        feature_contract="mapped failed attempts and pre-impact state",
    ),
    research_target(
        "e4-v12-golden-multithesis-v5.yml",
        "multi-thesis-v5",
        3,
        cooldown=120,
        backoff=30,
        thesis_family="multi-thesis-v5",
        model_family="chronological multi-thesis tournament",
        feature_contract="preimpact and reactive candidate families",
        status_path="docs/research/e4-v12-golden-current-status.json",
    ),
    research_target(
        "e4-v12-bayesian-source-golden.yml",
        "bayesian-source",
        4,
        cooldown=180,
        backoff=30,
        thesis_family="bayesian-source-confidence",
        model_family="causal Bayesian source confidence",
        feature_contract="source confidence and causal pre-entry evidence",
    ),
    research_target(
        "e4-v12-buyer-cluster-golden.yml",
        "buyer-cluster",
        5,
        cooldown=180,
        backoff=30,
        thesis_family="buyer-cluster",
        model_family="buyer-cluster conditional selector",
        feature_contract="causal early buyer cluster recurrence",
        status_path="docs/research/e4-v12-buyer-cluster-current-status.json",
    ),
    research_target(
        "e4-v12-recurrence-golden-v3.yml",
        "recurrence-shape",
        6,
        cooldown=180,
        backoff=30,
        thesis_family="recurrence-shape-v3",
        model_family="recurrence-shape selector",
        feature_contract="creator and buyer recurrence shape",
        status_path="docs/research/e4-v12-recurrence-current-status.json",
    ),
    research_target(
        "e4-v12-prior-whitelist-golden.yml",
        "prior-whitelist",
        7,
        cooldown=240,
        backoff=45,
        thesis_family="prior-whitelist",
        model_family="prior-identity whitelist selector",
        feature_contract="strictly prior creator whitelist membership",
        status_path="docs/research/e4-v12-prior-whitelist-current-status.json",
    ),
    research_target(
        "e4-v12-prelaunch-social-golden.yml",
        "prelaunch-social",
        8,
        cooldown=240,
        backoff=45,
        thesis_family="prelaunch-social",
        model_family="causal social selector",
        feature_contract="social observations timestamped before launch",
        status_path="docs/research/e4-v12-prelaunch-social-current-status.json",
    ),
    research_target(
        "e4-v12-reactive-confidence-golden.yml",
        "reactive-confidence",
        9,
        cooldown=240,
        backoff=45,
        thesis_family="reactive-confidence",
        model_family="reactive confidence model",
        feature_contract="post-launch causal flow confidence before decision",
        status_path="docs/research/e4-v12-reactive-confidence-current-status.json",
    ),
    research_target(
        "e4-v12-reactive-independent-golden.yml",
        "reactive-independent",
        10,
        cooldown=240,
        backoff=45,
        thesis_family="reactive-independent",
        model_family="independent reactive policy search",
        feature_contract="independent public-flow and market features",
    ),
    research_target(
        "e4-v12-reactive-lattice-golden.yml",
        "reactive-lattice",
        11,
        cooldown=240,
        backoff=45,
        thesis_family="reactive-lattice",
        model_family="reactive policy lattice",
        feature_contract="causal multihorizon reactive lattice",
    ),
    research_target(
        "e4-v12-reactive-profit-golden.yml",
        "reactive-profit",
        12,
        cooldown=240,
        backoff=45,
        thesis_family="reactive-profit",
        model_family="reactive profit model",
        feature_contract="causal profit and execution features",
    ),
    research_target(
        "e4-v12-market-golden.yml",
        "market-only",
        13,
        cooldown=360,
        backoff=60,
        thesis_family="market-only",
        model_family="market-only selector",
        feature_contract="market structure without identity leakage",
    ),
    research_target(
        "e4-v12-social-golden.yml",
        "social",
        14,
        cooldown=360,
        backoff=60,
        thesis_family="social",
        model_family="social evidence selector",
        feature_contract="causal immutable social observations",
    ),
    research_target(
        "e4-v12-social-whitelist-golden.yml",
        "social-whitelist",
        15,
        cooldown=360,
        backoff=60,
        thesis_family="social-whitelist",
        model_family="social and identity whitelist selector",
        feature_contract="causal social plus prior whitelist evidence",
        status_path="docs/research/e4-v12-social-whitelist-current-status.json",
    ),
)


class GitHubAPI:
    def __init__(self, repo: str, token: str) -> None:
        self.repo = repo
        self.token = token
        self.base = f"https://api.github.com/repos/{repo}"
        self._branch_heads: dict[str, str] = {}
        self._trees: dict[str, dict[str, str]] = {}

    def request(
        self,
        method: str,
        endpoint: str,
        payload: Mapping[str, Any] | None = None,
        *,
        allow_not_found: bool = False,
    ) -> Any:
        url = endpoint if endpoint.startswith("https://") else f"{self.base}{endpoint}"
        data = None
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Gambit-Jr-E4-watchdog/2.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
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
            raise RuntimeError(
                f"GitHub API {method} {endpoint} failed: HTTP {exc.code}: {body[:500]}"
            ) from exc

    def get(self, endpoint: str, *, allow_not_found: bool = False) -> Any:
        return self.request("GET", endpoint, allow_not_found=allow_not_found)

    def post(self, endpoint: str, payload: Mapping[str, Any] | None = None) -> Any:
        return self.request("POST", endpoint, payload)

    def branch_head(self, ref: str) -> str:
        if ref not in self._branch_heads:
            payload = self.get(f"/branches/{urllib.parse.quote(ref, safe='')}")
            self._branch_heads[ref] = str(
                ((payload or {}).get("commit") or {}).get("sha") or ""
            )
        return self._branch_heads[ref]

    def branch_tree(self, ref: str) -> dict[str, str]:
        if ref not in self._trees:
            treeish = (
                ref if re.fullmatch(r"[0-9a-f]{40}", ref) else self.branch_head(ref)
            )
            payload = self.get(f"/git/trees/{treeish}?recursive=1")
            self._trees[ref] = {
                str(row.get("path") or ""): str(row.get("sha") or "")
                for row in (payload or {}).get("tree") or []
                if row.get("type") == "blob"
            }
        return self._trees[ref]

    def file_text(self, path: str, ref: str) -> str | None:
        encoded_path = urllib.parse.quote(path, safe="/")
        query = urllib.parse.urlencode({"ref": ref})
        payload = self.get(f"/contents/{encoded_path}?{query}", allow_not_found=True)
        if not payload:
            return None
        encoded = str(payload.get("content") or "").replace("\n", "")
        return (
            base64.b64decode(encoded).decode("utf-8", errors="replace")
            if encoded
            else ""
        )

    def json_file(self, path: str, ref: str) -> dict[str, Any] | None:
        text = self.file_text(path, ref)
        if not text:
            return None
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return None
        return dict(value) if isinstance(value, Mapping) else None

    def workflow_runs(self, workflow: str, ref: str) -> list[dict[str, Any]]:
        encoded = urllib.parse.quote(workflow, safe="")
        query = urllib.parse.urlencode(
            {"branch": ref, "per_page": 20, "exclude_pull_requests": "true"}
        )
        payload = self.get(f"/actions/workflows/{encoded}/runs?{query}")
        return [
            dict(row)
            for row in ((payload or {}).get("workflow_runs") or [])
            if isinstance(row, Mapping)
        ]

    def latest_path_commit(self, path: str, ref: str) -> dict[str, Any] | None:
        query = urllib.parse.urlencode({"path": path, "sha": ref, "per_page": 1})
        payload = self.get(f"/commits?{query}")
        if not isinstance(payload, list) or not payload:
            return None
        row = payload[0]
        return dict(row) if isinstance(row, Mapping) else None

    def dispatch(self, workflow: str, ref: str) -> None:
        encoded = urllib.parse.quote(workflow, safe="")
        self.post(f"/actions/workflows/{encoded}/dispatches", {"ref": ref})

    def cancel(self, run_id: int) -> None:
        self.post(f"/actions/runs/{run_id}/cancel")


def parse_time(value: Any) -> datetime | None:
    return registry.parse_timestamp(value)


def age_minutes(value: Any, now: datetime) -> float | None:
    parsed = parse_time(value)
    return max(0.0, (now - parsed).total_seconds() / 60.0) if parsed else None


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def workflow_parameter_contract(text: str) -> list[str]:
    """Capture every workflow line that can encode an experiment parameter."""
    output: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if (
            "--" in line
            or re.match(r"^[A-Z][A-Z0-9_]+:\s*", line)
            or line.startswith(("matrix:", "include:", "- id:", "type:", "horizon:"))
        ):
            output.append(line)
    return output


def source_code_fingerprint(
    api: GitHubAPI, target: Target, *, ref: str | None = None
) -> str:
    workflow_path = f".github/workflows/{target.workflow}"
    relevant: dict[str, str] = {}
    for path, blob_sha in api.branch_tree(ref or target.ref).items():
        if (
            path == workflow_path
            or path.startswith(("scripts/", "src/", "tools/"))
            or path in {"pyproject.toml", "requirements-e4.txt"}
        ):
            relevant[path] = blob_sha
    if workflow_path not in relevant:
        raise ValueError(f"target workflow is absent from source tree: {workflow_path}")
    return registry.sha256_value(relevant)


def source_matches_latest_run(
    api: GitHubAPI, target: Target, latest_head_sha: str | None
) -> bool:
    if not latest_head_sha:
        return False
    return source_code_fingerprint(api, target) == source_code_fingerprint(
        api, target, ref=latest_head_sha
    )


def integrity_gate(api: GitHubAPI) -> dict[str, Any]:
    document = api.json_file(INTEGRITY_PATH, INTEGRITY_REF)
    if document is None:
        return {
            "passed": False,
            "reason": "canonical integrity report is missing or invalid",
            "fingerprint": None,
        }
    integrity = document.get("integrity") or {}
    passed = (
        integrity.get("status") == "PASS"
        and int(integrity.get("future_leakage_violations") or 0) == 0
        and int(integrity.get("ambiguous_labels") or 0) == 0
    )
    return {
        "passed": passed,
        "reason": (
            "canonical choice-set integrity passed"
            if passed
            else "canonical integrity gate failed"
        ),
        "fingerprint": registry.sha256_value(document),
        "source": f"{INTEGRITY_REF}:{INTEGRITY_PATH}",
    }


def evidence_snapshot(api: GitHubAPI, evidence_target: Target) -> dict[str, Any]:
    runs = [
        row
        for row in api.workflow_runs(evidence_target.workflow, evidence_target.ref)
        if row.get("status") == "completed" and row.get("conclusion") == "success"
    ]
    runs.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    source_runs = [
        {
            "run_id": int(row.get("id") or 0),
            "head_sha": str(row.get("head_sha") or ""),
            "created_at": str(row.get("created_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
        }
        for row in runs[:20]
    ]
    tree = api.branch_tree(evidence_target.ref)
    committed_manifest = {
        "source_ref": evidence_target.ref,
        "files": {
            path: tree.get(path) for path in (EVIDENCE_DATA_PATH, EVIDENCE_EPOCH_PATH)
        },
    }
    epoch_text = api.file_text(EVIDENCE_EPOCH_PATH, evidence_target.ref)
    epoch = (epoch_text or "unversioned-evidence-epoch").strip()
    data_blob = tree.get(EVIDENCE_DATA_PATH) or "missing"
    latest_commit = api.latest_path_commit(EVIDENCE_DATA_PATH, evidence_target.ref)
    commit_body = (latest_commit or {}).get("commit") or {}
    committer = commit_body.get("committer") or {}
    author = commit_body.get("author") or {}
    latest = parse_time(committer.get("date") or author.get("date"))
    return {
        "dataset_version": f"forward-evidence:{data_blob}",
        "dataset_source_manifest_fingerprint": registry.sha256_value(
            committed_manifest
        ),
        "evidence_epoch": epoch,
        "latest_successful_evidence_at": latest,
        "source_manifest": committed_manifest,
        "source_runs": source_runs,
    }


def experiment_spec(
    api: GitHubAPI, target: Target, evidence: Mapping[str, Any]
) -> ExperimentSpec:
    if not target.scientific:
        raise ValueError("fresh evidence collection is not a thesis experiment")
    workflow_path = f".github/workflows/{target.workflow}"
    workflow_text = api.file_text(workflow_path, target.ref)
    if workflow_text is None:
        raise ValueError(f"workflow definition unavailable: {workflow_path}")
    workflow_sha = _sha256_text(workflow_text)
    fixed_run_ids = sorted(
        {int(value) for value in re.findall(r"\b[1-9]\d{10}\b", workflow_text)}
    )
    if fixed_run_ids:
        dataset_manifest: dict[str, Any] = {
            "policy": "frozen-explicit-run-set",
            "source_ref": evidence.get("source_manifest", {}).get("source_ref"),
            "run_ids": fixed_run_ids,
        }
        dataset_version = "frozen-runs:" + ",".join(map(str, fixed_run_ids))
    elif "gh run list" in workflow_text or "selection-certification" in workflow_text:
        source_runs = list(evidence.get("source_runs") or [])
        dataset_manifest = {
            "policy": "rolling-successful-evidence-risk-set",
            "source_ref": evidence.get("source_manifest", {}).get("source_ref"),
            "runs": source_runs,
        }
        dataset_version = "rolling-runs:" + ",".join(
            str(row.get("run_id") or "") for row in source_runs
        )
    else:
        dataset_manifest = dict(evidence.get("source_manifest") or {})
        dataset_manifest["policy"] = "committed-evidence-manifest"
        dataset_version = str(evidence["dataset_version"])
    identity = {
        "thesis_family_identifier": target.thesis_family,
        "source_code_fingerprint": source_code_fingerprint(api, target),
        "dataset_source_manifest_fingerprint": registry.sha256_value(dataset_manifest),
        "evidence_epoch": evidence["evidence_epoch"],
        "feature_set_fingerprint": registry.sha256_value(
            {
                "feature_contract": target.feature_contract,
                "workflow_definition_sha256": workflow_sha,
            }
        ),
        "model_family": target.model_family,
        "full_parameters": {
            "workflow_definition_sha256": workflow_sha,
            "workflow_parameter_contract": workflow_parameter_contract(workflow_text),
        },
        "causal_horizon": target.causal_horizon,
        "candidate_risk_set_policy": target.candidate_risk_set_policy,
        "chronological_split": {
            "policy": "strict chronological train/validation/untouched-holdout",
            "definition_committed_by": workflow_sha,
        },
        "bankroll": {
            "starting_balance_sol": 3.0,
            "definition_committed_by": workflow_sha,
        },
        "position_sizing": {
            "policy": "workflow-defined bounded position sizing",
            "definition_committed_by": workflow_sha,
        },
        "fee_model": {
            "policy": "workflow-defined Solana fees and priority fees",
            "definition_committed_by": workflow_sha,
        },
        "output_guard": {
            "policy": "V12 bounded output deterioration guard",
            "definition_committed_by": workflow_sha,
        },
        "latency_assumptions": {
            "replay_ms": [0, 1, 2, 5, 10],
            "definition_committed_by": workflow_sha,
        },
        "execution_policy": {
            "policy": "paper replay only; immutable V12 execution semantics",
            "definition_committed_by": workflow_sha,
        },
        "exit_policy": {
            "policy": "workflow-defined causal exit policy",
            "definition_committed_by": workflow_sha,
        },
    }
    return ExperimentSpec(identity, dataset_version, dataset_manifest)


def _walk_mappings(value: Any) -> Sequence[Mapping[str, Any]]:
    output: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        output.append(value)
        for nested in value.values():
            output.extend(_walk_mappings(nested))
    elif isinstance(value, list):
        for nested in value:
            output.extend(_walk_mappings(nested))
    return output


def _first_mapping(
    document: Mapping[str, Any], names: Sequence[str]
) -> dict[str, Any] | None:
    wanted = {name.lower() for name in names}
    for mapping in _walk_mappings(document):
        for key, value in mapping.items():
            if str(key).lower() in wanted and isinstance(value, Mapping):
                return dict(value)
    return None


def _first_value(document: Mapping[str, Any], names: Sequence[str]) -> Any:
    wanted = {name.lower() for name in names}
    for mapping in _walk_mappings(document):
        for key, value in mapping.items():
            if str(key).lower() in wanted and not isinstance(value, (Mapping, list)):
                return value
    return None


def metrics_from_status(status: Mapping[str, Any] | None) -> dict[str, Any]:
    document = status or {}
    return {
        "train_metrics": _first_mapping(document, ("train", "train_metrics")),
        "validation_metrics": _first_mapping(
            document, ("validation", "validation_metrics")
        ),
        "chronological_holdout_metrics": _first_mapping(
            document, ("holdout", "holdout_metrics", "test_metrics")
        ),
        "live_metrics": _first_mapping(
            document, ("live", "live_metrics", "untouched_live")
        ),
        "trade_count": _first_value(document, ("trade_count", "trades", "n_trades")),
        "wr": _first_value(document, ("wr", "win_rate", "holdout_win_rate")),
        "wilson_lower_bound": _first_value(
            document, ("wilson_lower_bound", "wilson_lb", "wilson_lower")
        ),
        "pnl": _first_value(document, ("pnl", "pnl_sol", "net_pnl")),
        "profit_factor": _first_value(document, ("profit_factor", "pf")),
        "drawdown": _first_value(document, ("drawdown", "max_drawdown")),
        "selection_precision": _first_value(
            document, ("selection_precision", "precision")
        ),
        "recall": _first_value(document, ("recall", "selection_recall")),
        "latency_results": _first_mapping(
            document, ("quote_to_route_ack_ms", "latency", "latency_results")
        ),
    }


def status_failure(
    target: Target, run: Mapping[str, Any], status: Mapping[str, Any] | None
) -> tuple[str | None, str | None]:
    conclusion = str(run.get("conclusion") or "")
    if conclusion in INFRASTRUCTURE_CONCLUSIONS:
        return "INFRASTRUCTURE_FAILURE", f"workflow concluded {conclusion}"
    document = status or {}
    text = registry.canonical_json(document).upper()
    for failure in sorted(registry.FAILURE_CLASSES):
        if failure in text and failure != "DUPLICATE_EXPERIMENT":
            return failure, f"status artifact classified the run as {failure}"
    if any(
        word in text for word in ("PROVIDER_UNAVAILABLE", "HTTP_429", "RPC_FAILURE")
    ):
        return (
            "INFRASTRUCTURE_FAILURE",
            "status artifact reports a temporary provider failure",
        )
    latency = metrics_from_status(status).get("latency_results") or {}
    if (
        status
        and status.get("sub10ms_pass") is False
        and float(latency.get("p95") or 0) >= 10
    ):
        return "LATENCY_FAILURE", "declared sub-10ms gate failed"
    thesis_status = str(
        document.get("thesis_status")
        or document.get("preimpact_status")
        or document.get("status")
        or ""
    ).upper()
    if thesis_status in {"NOT_CONCLUSIVE", "NOT_PRODUCED", "NO_TRAIN_SURVIVOR"}:
        return "NO_TRAIN_SURVIVOR", f"thesis status was {thesis_status}"
    if conclusion == "failure":
        return (
            "NO_TRAIN_SURVIVOR",
            "golden search failed without an infrastructure marker",
        )
    if conclusion in SUCCESS_CONCLUSIONS:
        return (
            "HOLDOUT_COLLAPSE",
            "workflow completed without an untouched-live golden pass",
        )
    return (
        "INFRASTRUCTURE_FAILURE",
        f"unclassified workflow conclusion {conclusion or 'unknown'}",
    )


def material_change_for(failure: str | None) -> str | None:
    mapping = {
        "DATA_INTEGRITY_FAILURE": "repair integrity and change the dataset/source-manifest fingerprint",
        "INSUFFICIENT_LABELS": "add causally valid labels or a new evidence epoch",
        "NO_TRAIN_SURVIVOR": "change features, model family, parameters, or evidence",
        "VALIDATION_COLLAPSE": "change the thesis or feature/parameter fingerprint",
        "HOLDOUT_COLLAPSE": "change the thesis or add a genuinely new evidence epoch",
        "LIVE_COLLAPSE": "change execution assumptions or collect a new untouched-live epoch",
        "FALSE_POSITIVE_OVERLOAD": "change risk-set policy or selection threshold parameters",
        "NEGATIVE_EXPECTANCY": "change entry/exit economics or thesis family",
        "INADEQUATE_PROFIT_FACTOR": "change causal features, sizing, fees, or exit policy",
        "LATENCY_FAILURE": "change source or transport latency assumptions/code",
        "OUTPUT_DETERIORATION": "change output guard or execution policy",
        "EXECUTION_FAILURE": "change execution policy or infrastructure",
        "EXIT_FAILURE": "change exit policy",
        "INFRASTRUCTURE_FAILURE": "retry after backoff without changing scientific identity",
    }
    return mapping.get(str(failure or ""))


def untouched_live_gate(api: GitHubAPI) -> tuple[bool, dict[str, Any]]:
    sentinels = {path: api.file_text(path, RESEARCH_REF) for path in SENTINEL_PATHS}
    verdicts = {path: api.json_file(path, RESEARCH_REF) for path in LIVE_VERDICT_PATHS}
    sentinel_pass = any(
        str(value or "").strip().upper() in GOLDEN_VALUES
        for value in sentinels.values()
    )
    for path, verdict in verdicts.items():
        if not verdict:
            continue
        status = str(verdict.get("status") or "").strip().upper()
        explicit_pass = (
            verdict.get("golden_gate_passed") is True or status in GOLDEN_VALUES
        )
        if verdict.get("untouched_live") is True and explicit_pass and sentinel_pass:
            return True, {
                "sentinels": sentinels,
                "verdict_path": path,
                "verdict": verdict,
            }
    return False, {"sentinels": sentinels, "verdicts": verdicts}


def stop_gate(integrity: Mapping[str, Any], live_golden_passed: bool) -> bool:
    return bool(integrity.get("passed") is True and live_golden_passed)


def latest_run_fields(action: Action, run: Mapping[str, Any], now: datetime) -> None:
    action.latest_run_id = int(run.get("id") or 0) or None
    action.latest_status = str(run.get("status") or "") or None
    action.latest_conclusion = str(run.get("conclusion") or "") or None
    action.latest_head_sha = str(run.get("head_sha") or "") or None
    action.age_minutes = age_minutes(
        run.get("updated_at") or run.get("created_at"), now
    )


def fresh_evidence_action(api: GitHubAPI, target: Target, *, now: datetime) -> Action:
    action = Action(
        target.workflow,
        target.ref,
        target.role,
        "observe",
        "uninitialised",
        cancelled_run_ids=[],
    )
    try:
        action.branch_head_sha = api.branch_head(target.ref)
        runs = api.workflow_runs(target.workflow, target.ref)
    except WATCHDOG_ERRORS as exc:
        action.decision = "error"
        action.reason = "unable to inspect fresh-evidence workflow"
        action.error = str(exc)
        return action
    active = [row for row in runs if str(row.get("status") or "") in ACTIVE_STATUSES]
    if active:
        newest = active[0]
        latest_run_fields(action, newest, now)
        limit = (
            target.stale_queue_minutes
            if action.latest_status in {"queued", "waiting", "requested", "pending"}
            else target.stale_active_minutes
        )
        if action.age_minutes is not None and action.age_minutes > limit:
            action.decision = "restart"
            action.reason = f"fresh-evidence job exceeded {limit} minute stale limit"
            action.cancelled_run_ids = [
                int(row["id"]) for row in active if row.get("id")
            ]
        else:
            action.decision = "healthy"
            action.reason = "fresh evidence collection is active"
        return action
    latest = runs[0] if runs else None
    if latest:
        latest_run_fields(action, latest, now)
    if (
        latest is None
        or action.age_minutes is None
        or action.age_minutes >= target.success_cooldown_minutes
    ):
        action.decision = "dispatch"
        action.reason = "fresh evidence cadence is due"
    else:
        action.decision = "healthy"
        action.reason = "fresh evidence is inside its independent collection cadence"
    return action


def _artifact_paths(target: Target, run: Mapping[str, Any]) -> list[str]:
    paths = [str(run.get("html_url") or f"github-actions:{target.workflow}")]
    if target.status_path:
        paths.append(f"{target.ref}:{target.status_path}")
    paths.append(f"{target.ref}:.github/workflows/{target.workflow}")
    return paths


def scientific_action(
    api: GitHubAPI,
    store: registry.RegistryStore,
    target: Target,
    evidence: Mapping[str, Any],
    *,
    now: datetime,
    integrity_passed: bool,
    dry_run: bool,
) -> Action:
    action = Action(
        target.workflow,
        target.ref,
        target.role,
        "observe",
        "uninitialised",
        cancelled_run_ids=[],
        thesis_family=target.thesis_family,
    )
    try:
        action.branch_head_sha = api.branch_head(target.ref)
        runs = api.workflow_runs(target.workflow, target.ref)
        spec = experiment_spec(api, target, evidence)
        identifier = registry.experiment_id(spec.identity)
        action.experiment_id = identifier
        document = store.load()
    except WATCHDOG_ERRORS as exc:
        action.decision = "error"
        action.reason = "unable to construct or inspect experiment identity"
        action.error = str(exc)
        return action

    families = {str(row.get("thesis_family") or "") for row in document["records"]}
    record = registry.record_by_id(document, identifier)
    action.novelty_priority = (
        0 if target.thesis_family not in families else 1 if record is None else 2
    )
    active = [row for row in runs if str(row.get("status") or "") in ACTIVE_STATUSES]
    status = (
        api.json_file(target.status_path, target.ref) if target.status_path else None
    )
    if active:
        newest = active[0]
        latest_run_fields(action, newest, now)
        state = str(action.latest_status or "").upper()
        try:
            same_source = source_matches_latest_run(api, target, action.latest_head_sha)
        except WATCHDOG_ERRORS as exc:
            action.decision = "error"
            action.reason = "unable to compare active-run source fingerprint"
            action.error = str(exc)
            return action
        active_started = parse_time(newest.get("created_at"))
        latest_evidence = evidence.get("latest_successful_evidence_at")
        same_dataset = not latest_evidence or (
            active_started is not None and active_started >= latest_evidence
        )
        if record is None and same_source and same_dataset and not dry_run:
            record, _ = store.register(
                spec.identity,
                {
                    "state": state,
                    "source_commit": action.latest_head_sha or action.branch_head_sha,
                    "dataset_version": spec.dataset_version,
                    "dataset_source_manifest": spec.dataset_source_manifest,
                    "workflow": target.workflow,
                    "ref": target.ref,
                    "latest_run_id": action.latest_run_id,
                    "latest_run_url": newest.get("html_url"),
                    "dispatch_attempts": 1,
                    "exact_artifact_paths": _artifact_paths(target, newest),
                },
            )
        stale_limit = (
            target.stale_queue_minutes
            if action.latest_status in {"queued", "waiting", "requested", "pending"}
            else target.stale_active_minutes
        )
        age = age_minutes(newest.get("created_at"), now)
        action.age_minutes = age
        if age is not None and age > stale_limit:
            action.decision = "restart"
            action.reason = f"active job exceeded {stale_limit} minute stale limit"
            action.failure_classification = "INFRASTRUCTURE_FAILURE"
            if record and not dry_run:
                store.update(
                    identifier,
                    {
                        "state": "INFRASTRUCTURE_FAILED",
                        "failure_classification": "INFRASTRUCTURE_FAILURE",
                        "retry_not_before": registry.iso_timestamp(now),
                        "material_change_required_before_rerun": material_change_for(
                            "INFRASTRUCTURE_FAILURE"
                        ),
                    },
                )
            action.cancelled_run_ids = [
                int(row["id"]) for row in active if row.get("id")
            ]
            return action
        action.decision = "healthy"
        action.reason = "identical experiment is already active"
        return action

    latest = next(
        (row for row in runs if row.get("status") == "completed"),
        runs[0] if runs else None,
    )
    if latest:
        latest_run_fields(action, latest, now)
    try:
        same_source = bool(
            latest and source_matches_latest_run(api, target, action.latest_head_sha)
        )
    except WATCHDOG_ERRORS as exc:
        action.decision = "error"
        action.reason = "unable to compare completed-run source fingerprint"
        action.error = str(exc)
        return action
    latest_started = parse_time(latest.get("created_at")) if latest else None
    new_evidence = bool(
        evidence.get("latest_successful_evidence_at")
        and latest_started
        and evidence["latest_successful_evidence_at"] > latest_started
    )
    if (
        record is not None
        and latest
        and str(record.get("state") or "") in registry.ACTIVE_STATES
    ):
        failure, retirement_reason = status_failure(target, latest, status)
        action.failure_classification = failure
        changes = {
            "state": "COMPLETED_FAILED" if failure else "COMPLETED",
            "latest_run_id": action.latest_run_id,
            "latest_run_url": latest.get("html_url"),
            "failure_classification": failure,
            "retired": failure in registry.SCIENTIFIC_FAILURE_CLASSES,
            "reason_for_retirement": retirement_reason,
            "material_change_required_before_rerun": material_change_for(failure),
            "exact_artifact_paths": _artifact_paths(target, latest),
            "live_status": status.get("status") if status else None,
            **metrics_from_status(status),
        }
        record = (
            store.update(identifier, changes)
            if not dry_run
            else {**record, **changes, "updated_timestamp": registry.iso_timestamp(now)}
        )
    if record is None and latest and same_source and not new_evidence:
        failure, retirement_reason = status_failure(target, latest, status)
        action.failure_classification = failure
        metadata = {
            "state": "COMPLETED_FAILED" if failure else "COMPLETED",
            "source_commit": action.latest_head_sha or action.branch_head_sha,
            "dataset_version": spec.dataset_version,
            "dataset_source_manifest": spec.dataset_source_manifest,
            "workflow": target.workflow,
            "ref": target.ref,
            "latest_run_id": action.latest_run_id,
            "latest_run_url": latest.get("html_url"),
            "failure_classification": failure,
            "retired": failure in registry.SCIENTIFIC_FAILURE_CLASSES,
            "retirement_reason": retirement_reason,
            "material_change_required_before_rerun": material_change_for(failure),
            "exact_artifact_paths": _artifact_paths(target, latest),
            "live_status": status.get("status") if status else None,
            **metrics_from_status(status),
        }
        if not dry_run:
            record, _ = store.register(spec.identity, metadata)
        else:
            record = registry.new_record(spec.identity, metadata, now=now)

    retry_state, retry_reason = registry.retry_decision(
        record,
        now=now,
        infrastructure_backoff_minutes=target.failure_backoff_minutes,
        stale_active_minutes=target.stale_active_minutes,
    )
    if not integrity_passed:
        action.decision = "blocked_integrity"
        action.reason = "canonical data integrity gate is not passing"
        action.failure_classification = "DATA_INTEGRITY_FAILURE"
        return action
    if retry_state == "ELIGIBLE_NOVEL":
        action.decision = "dispatch"
        action.reason = "new experiment identity"
        return action
    if retry_state in {"ELIGIBLE_INFRASTRUCTURE_RETRY", "ELIGIBLE_STALE_RESTART"}:
        action.decision = "restart" if retry_state.endswith("RESTART") else "dispatch"
        action.reason = retry_reason
        action.failure_classification = "INFRASTRUCTURE_FAILURE"
        return action
    if retry_state == "ACTIVE":
        action.decision = "healthy"
        action.reason = retry_reason
        return action
    action.decision = "retire_duplicate"
    action.reason = retry_reason
    action.failure_classification = "DUPLICATE_EXPERIMENT"
    if record and not dry_run:
        store.suppress_duplicate(identifier, run_id=action.latest_run_id)
    return action


def prepare_dispatch_record(
    store: registry.RegistryStore,
    api: GitHubAPI,
    target: Target,
    evidence: Mapping[str, Any],
    action: Action,
) -> None:
    spec = experiment_spec(api, target, evidence)
    existing = registry.record_by_id(
        store.load(), registry.experiment_id(spec.identity)
    )
    if existing is None:
        store.register(
            spec.identity,
            {
                "state": "DISPATCHED",
                "source_commit": action.branch_head_sha,
                "dataset_version": spec.dataset_version,
                "dataset_source_manifest": spec.dataset_source_manifest,
                "workflow": target.workflow,
                "ref": target.ref,
                "dispatch_attempts": 1,
                "exact_artifact_paths": [
                    f"{target.ref}:.github/workflows/{target.workflow}"
                ],
            },
        )
    else:
        store.update(
            existing["experiment_id"],
            {
                "state": "DISPATCHED",
                "failure_classification": None,
                "retired": False,
                "reason_for_retirement": None,
                "latest_run_id": None,
                "dispatch_attempts": int(existing.get("dispatch_attempts") or 0) + 1,
                "retry_not_before": None,
            },
        )


def dispatch_action(
    api: GitHubAPI, target: Target, action: Action, *, dry_run: bool
) -> None:
    if action.decision == "restart":
        for run_id in action.cancelled_run_ids or []:
            if not dry_run:
                api.cancel(run_id)
    if not dry_run:
        api.dispatch(target.workflow, target.ref)
    action.dispatched = True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Novelty-aware 10-minute E4 V12 research watchdog"
    )
    parser.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path("artifacts/e4-v12-experiment-registry.json"),
    )
    parser.add_argument(
        "--leaderboard-json",
        type=Path,
        default=Path("artifacts/e4-v12-experiment-leaderboard.json"),
    )
    parser.add_argument(
        "--leaderboard-markdown",
        type=Path,
        default=Path("artifacts/e4-v12-experiment-leaderboard.md"),
    )
    parser.add_argument(
        "--max-dispatches",
        type=int,
        default=int(os.getenv("E4_WATCHDOG_MAX_DISPATCHES", "6")),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or ""
    if not args.repo:
        raise SystemExit("--repo or GITHUB_REPOSITORY is required")
    if not token and not args.dry_run:
        raise SystemExit("GH_TOKEN or GITHUB_TOKEN is required for dispatch")

    now = datetime.now(UTC)
    api = GitHubAPI(args.repo, token)
    store = registry.RegistryStore(args.registry)
    try:
        before = store.load()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"experiment registry integrity failure: {exc}") from exc

    gate = integrity_gate(api)
    live_golden_passed, live_evidence = untouched_live_gate(api)
    golden_passed = stop_gate(gate, live_golden_passed)
    report: dict[str, Any] = {
        "version": "e4-v12-24x7-watchdog-v2",
        "checked_at": now.isoformat(),
        "repository": args.repo,
        "research_ref": RESEARCH_REF,
        "interval_minutes": 10,
        "found": golden_passed,
        "live_golden_gate_passed": live_golden_passed,
        "untouched_live_gate": live_evidence,
        "integrity_gate": gate,
        "max_dispatches_per_tick": max(0, args.max_dispatches),
        "dry_run": bool(args.dry_run),
        "registry_records_before": len(before["records"]),
        "actions": [],
    }
    if golden_passed:
        report["state"] = "UNTOUCHED_LIVE_GOLDEN_GATE_PASSED"
        report["message"] = (
            "Untouched-live result passed the declared golden gate; research dispatch stopped."
        )
        registry.write_leaderboard(
            store.load(), args.leaderboard_json, args.leaderboard_markdown
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        registry.atomic_write_json(args.output, report)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    evidence_target = TARGETS[0]
    evidence_action = fresh_evidence_action(api, evidence_target, now=now)
    evidence = evidence_snapshot(api, evidence_target)
    report["latest_successful_evidence_at"] = (
        evidence["latest_successful_evidence_at"].isoformat()
        if evidence["latest_successful_evidence_at"]
        else None
    )
    scientific_actions = [
        scientific_action(
            api,
            store,
            target,
            evidence,
            now=now,
            integrity_passed=bool(gate["passed"]),
            dry_run=bool(args.dry_run),
        )
        for target in TARGETS[1:]
    ]

    target_by_workflow = {target.workflow: target for target in TARGETS}
    if evidence_action.decision in {"dispatch", "restart"}:
        try:
            dispatch_action(
                api,
                evidence_target,
                evidence_action,
                dry_run=bool(args.dry_run),
            )
        except WATCHDOG_ERRORS as exc:
            evidence_action.decision = "error"
            evidence_action.dispatched = False
            evidence_action.error = str(exc)

    eligible = [
        action
        for action in scientific_actions
        if action.decision in {"dispatch", "restart"}
    ]
    eligible.sort(
        key=lambda action: (
            action.novelty_priority if action.novelty_priority is not None else 99,
            target_by_workflow[action.workflow].priority,
        )
    )
    dispatch_budget = max(0, args.max_dispatches)
    for action in eligible:
        target = target_by_workflow[action.workflow]
        if dispatch_budget <= 0:
            action.decision = "deferred"
            action.reason += "; per-tick novel-experiment dispatch budget exhausted"
            continue
        try:
            if not args.dry_run:
                prepare_dispatch_record(store, api, target, evidence, action)
            dispatch_action(api, target, action, dry_run=bool(args.dry_run))
            dispatch_budget -= 1
        except WATCHDOG_ERRORS as exc:
            action.error = str(exc)
            action.dispatched = False
            action.decision = "error"
            if action.experiment_id and not args.dry_run:
                retry_at = now + timedelta(minutes=target.failure_backoff_minutes)
                try:
                    store.update(
                        action.experiment_id,
                        {
                            "state": "INFRASTRUCTURE_FAILED",
                            "failure_classification": "INFRASTRUCTURE_FAILURE",
                            "retry_not_before": registry.iso_timestamp(retry_at),
                            "material_change_required_before_rerun": material_change_for(
                                "INFRASTRUCTURE_FAILURE"
                            ),
                        },
                    )
                except KeyError:
                    pass

    actions = [evidence_action, *scientific_actions]
    report["actions"] = [asdict(action) for action in actions]
    report["dispatches"] = sum(action.dispatched for action in actions)
    report["fresh_evidence_dispatches"] = int(evidence_action.dispatched)
    report["novel_scientific_dispatches"] = sum(
        action.dispatched for action in scientific_actions
    )
    report["duplicates_retired"] = sum(
        action.decision == "retire_duplicate" for action in scientific_actions
    )
    report["active_or_healthy"] = sum(
        action.decision in {"healthy", "cooldown"} for action in actions
    )
    report["errors"] = [
        {"workflow": action.workflow, "error": action.error}
        for action in actions
        if action.error
    ]
    report["registry_records_after"] = len(store.load()["records"])
    if not gate["passed"]:
        report["state"] = "DATA_INTEGRITY_BLOCKED"
    elif report["errors"]:
        report["state"] = "RESEARCH_REQUIRES_ATTENTION"
    else:
        report["state"] = "RESEARCH_ACTIVE"
    registry.write_leaderboard(
        store.load(), args.leaderboard_json, args.leaderboard_markdown
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    registry.atomic_write_json(args.output, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

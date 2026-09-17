#!/usr/bin/env python3
"""Backfill causal social timestamps for recurring-creator holdout launches."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import aiohttp
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e4_v12_wallet_selection_model as wallet
from e4_v12_fast_prearmed_selector import benchmark as benchmark_selector

SCHEMA_VERSION = "e4-v12-holdout-social-backfill-v1"
TWITTER_EPOCH_MS = 1_288_834_974_657
STATUS_RE = re.compile(r"(?:twitter\.com|x\.com)/[^/]+/status/(\d+)", re.IGNORECASE)
HANDLE_RE = re.compile(r"(?:twitter\.com|x\.com)/([^/?#]+)", re.IGNORECASE)


def normalise_metadata_uri(uri: str) -> str:
    value = uri.strip()
    if value.startswith("ipfs://"):
        return f"https://gateway.pinata.cloud/ipfs/{value[7:]}"
    match = re.search(r"/ipfs/([^/?#]+)", value)
    if match:
        return f"https://gateway.pinata.cloud/ipfs/{match.group(1)}"
    return value


def social_status_id(value: str) -> str:
    match = STATUS_RE.search(value)
    return match.group(1) if match else ""


def social_handle(value: str) -> str:
    match = HANDLE_RE.search(value)
    return match.group(1).lower() if match else ""


def snowflake_timestamp_ns(status_id: str) -> int:
    if not status_id:
        return 0
    return ((int(status_id) >> 22) + TWITTER_EPOCH_MS) * 1_000_000


async def fetch_one(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    uri: str,
) -> tuple[str, dict[str, Any]]:
    url = normalise_metadata_uri(uri)
    if not url.startswith(("http://", "https://")):
        return uri, {"error": "unsupported_uri", "twitter": "", "status_id": ""}
    async with semaphore:
        for attempt in range(3):
            try:
                async with session.get(url) as response:
                    if response.status == 200:
                        payload = await response.json(content_type=None)
                        twitter = str(payload.get("twitter") or "")
                        return uri, {
                            "error": "",
                            "twitter": twitter,
                            "handle": social_handle(twitter),
                            "status_id": social_status_id(twitter),
                        }
                    if response.status not in {429, 500, 502, 503, 504}:
                        return uri, {
                            "error": f"http_{response.status}",
                            "twitter": "",
                            "status_id": "",
                        }
            except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
                if attempt == 2:
                    return uri, {
                        "error": type(exc).__name__,
                        "twitter": "",
                        "status_id": "",
                    }
            await asyncio.sleep(0.25 * (2**attempt))
    return uri, {"error": "retry_exhausted", "twitter": "", "status_id": ""}


async def fetch_metadata(uris: Sequence[str]) -> dict[str, dict[str, Any]]:
    timeout = aiohttp.ClientTimeout(total=30)
    headers = {"User-Agent": "Gambit-Jr-E4-research/12"}
    semaphore = asyncio.Semaphore(4)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        results = await asyncio.gather(
            *(fetch_one(session, semaphore, uri) for uri in sorted(set(uris)))
        )
    return dict(results)


def rule_metrics(
    rows: Sequence[wallet.LaunchRow],
    selected: set[tuple[str, str]],
) -> dict[str, Any]:
    predicted = [row for row in rows if (row.run_id, row.mint) in selected]
    true_positive = sum(row.selected for row in predicted)
    attempts = sum(row.selected for row in rows)
    return {
        "launches": len(rows),
        "attempts": attempts,
        "predicted": len(predicted),
        "true_positive": true_positive,
        "precision": true_positive / max(len(predicted), 1),
        "recall": true_positive / max(attempts, 1),
    }


def load_or_fetch(root: Path, uris: Sequence[str]) -> dict[str, dict[str, Any]]:
    cache_path = root / ".tmp-e4-v12-holdout-social-metadata.json"
    cache: dict[str, dict[str, Any]] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    retryable_errors = {"retry_exhausted", "TimeoutError", "ClientConnectorError"}
    missing = [
        uri
        for uri in uris
        if uri
        and (
            uri not in cache
            or str(cache[uri].get("error") or "") in retryable_errors
        )
    ]
    if missing:
        print(f"fetch metadata {len(missing)} unique URIs", flush=True)
        cache.update(asyncio.run(fetch_metadata(missing)))
        wallet.write_json(cache_path, cache)
    return cache


def run(root: Path) -> dict[str, Any]:
    rows, traces, audit = wallet.extract_dataset(
        root, include_holdout=True, only_holdout=True
    )
    unique = [row for row in rows if row.horizon_ms == 0]
    recurring = [
        row
        for row in unique
        if row.features[wallet.FEATURE_NAMES.index("creator_prior_e4_attempts")] >= 1
    ]
    uri_by_key: dict[tuple[str, str], str] = {}
    for spec in wallet.base.capture_specs(root, include_holdout=True):
        if spec.split != "holdout":
            continue
        creates = wallet.load_create_rows(spec.events_path)
        for mint, create in creates.items():
            raw = create.get("raw") if isinstance(create.get("raw"), Mapping) else {}
            uri_by_key[(spec.run_id, mint)] = str(
                raw.get("uri") or create.get("uri") or ""
            )
    cache = load_or_fetch(
        root, [uri_by_key.get((row.run_id, row.mint), "") for row in recurring]
    )
    social: dict[tuple[str, str], dict[str, Any]] = {}
    for row in recurring:
        key = (row.run_id, row.mint)
        uri = uri_by_key.get(key, "")
        record = cache.get(uri, {})
        status_id = str(record.get("status_id") or "")
        timestamp_ns = snowflake_timestamp_ns(status_id)
        age_seconds = (
            (row.create_ns - timestamp_ns) / 1_000_000_000 if timestamp_ns else None
        )
        social[key] = {
            "metadata_available": bool(record) and not record.get("error"),
            "twitter_available": bool(record.get("twitter")),
            "status_available": bool(status_id),
            "tweet_age_seconds": age_seconds,
        }

    policy_payload = json.loads(
        (root / "artifacts/e4-v12-wallet-selection-development.json").read_text(
            encoding="utf-8"
        )
    )["winner"]["policy"]
    policy = wallet.ExitPolicy(
        policy_payload["stop"],
        policy_payload["first_take"],
        policy_payload["first_fraction"],
        policy_payload["hold_ms"],
        policy_payload["trail_retrace"],
    )
    source_payload = json.loads(
        (
            root
            / ".tmp-choice-set-source/research/33889403746/e4-v12-global-attempt-intent.json"
        ).read_text(encoding="utf-8")
    )
    source_runs = {str(row["run"]) for row in source_payload["rows"]}
    source_max_run = max(source_runs, key=int)
    creator_handles: dict[str, set[str]] = {}
    for source_row in source_payload["rows"]:
        creator = str(source_row.get("creator") or "")
        handle = str(source_row.get("twitter_handle") or "").lower()
        if creator and handle:
            creator_handles.setdefault(creator, set()).add(handle)
    for row in sorted(recurring, key=lambda item: (item.create_ns, item.run_id, item.mint)):
        key = (row.run_id, row.mint)
        trace = traces[key]
        uri = uri_by_key.get(key, "")
        cached = cache.get(uri, {})
        handle = str(cached.get("handle") or social_handle(str(cached.get("twitter") or "")))
        social[key]["handle"] = handle
        social[key]["prearmed_handle_match"] = bool(
            handle and handle in creator_handles.get(trace.creator, set())
        )
        if handle:
            creator_handles.setdefault(trace.creator, set()).add(handle)
    segment_universe = {
        "all_holdout": unique,
        "not_in_social_source": [
            row for row in unique if row.run_id not in source_runs
        ],
        "strictly_after_social_source": [
            row for row in unique if int(row.run_id) > int(source_max_run)
        ],
    }

    def evaluate_segments(
        candidate_rows: Sequence[wallet.LaunchRow],
    ) -> dict[str, Any]:
        candidate_segments = {
            "all_holdout": list(candidate_rows),
            "not_in_social_source": [
                row for row in candidate_rows if row.run_id not in source_runs
            ],
            "strictly_after_social_source": [
                row for row in candidate_rows if int(row.run_id) > int(source_max_run)
            ],
        }
        output = {}
        for segment, rows_in_segment in candidate_segments.items():
            economics = {}
            for latency in (0, 1, 2, 5, 10, 20, 50):
                replay = wallet.simulate(
                    rows_in_segment,
                    traces,
                    np.ones(len(rows_in_segment)),
                    0.5,
                    policy,
                    latency_ms=latency,
                    decision_horizon_ms=0,
                )
                economics[str(latency)] = {
                    key: value
                    for key, value in replay.items()
                    if key not in {"ledger", "ledger_hash"}
                }
            output[segment] = {
                "intent": rule_metrics(
                    segment_universe[segment],
                    {(row.run_id, row.mint) for row in rows_in_segment},
                ),
                "economics": economics,
            }
        return output

    rules = {}
    for maximum_age in (1, 2, 5, 10, 30):
        selected = {
            key
            for key, value in social.items()
            if value["tweet_age_seconds"] is not None
            and 0 <= value["tweet_age_seconds"] <= maximum_age
        }
        selected_rows = [
            row for row in recurring if (row.run_id, row.mint) in selected
        ]
        segment_rows = {
            "all_holdout": selected_rows,
            "not_in_social_source": [
                row for row in selected_rows if row.run_id not in source_runs
            ],
            "strictly_after_social_source": [
                row for row in selected_rows if int(row.run_id) > int(source_max_run)
            ],
        }
        segments = {}
        for segment, candidate_rows in segment_rows.items():
            economics = {}
            for latency in (0, 1, 2, 5, 10, 20, 50):
                result = wallet.simulate(
                    candidate_rows,
                    traces,
                    np.ones(len(candidate_rows)),
                    0.5,
                    policy,
                    latency_ms=latency,
                    decision_horizon_ms=0,
                )
                economics[str(latency)] = {
                    key: value
                    for key, value in result.items()
                    if key not in {"ledger", "ledger_hash"}
                }
            segments[segment] = {
                "intent": rule_metrics(
                    segment_universe[segment],
                    {(row.run_id, row.mint) for row in candidate_rows},
                ),
                "economics": economics,
            }
        rules[f"repeat_creator_tweet_within_{maximum_age}s"] = {
            "intent": rule_metrics(unique, selected),
            "economics": segments["all_holdout"]["economics"],
            "segments": segments,
        }
        prearmed_selected = {
            key
            for key in selected
            if social[key]["prearmed_handle_match"]
        }
        prearmed_rows = [
            row for row in recurring if (row.run_id, row.mint) in prearmed_selected
        ]
        prearmed_segments = {}
        for segment, candidate_rows in {
            "all_holdout": prearmed_rows,
            "not_in_social_source": [
                row for row in prearmed_rows if row.run_id not in source_runs
            ],
            "strictly_after_social_source": [
                row for row in prearmed_rows if int(row.run_id) > int(source_max_run)
            ],
        }.items():
            economics = {}
            for latency in (0, 1, 2, 5, 10, 20, 50):
                result = wallet.simulate(
                    candidate_rows,
                    traces,
                    np.ones(len(candidate_rows)),
                    0.5,
                    policy,
                    latency_ms=latency,
                    decision_horizon_ms=0,
                )
                economics[str(latency)] = {
                    key: value
                    for key, value in result.items()
                    if key not in {"ledger", "ledger_hash"}
                }
            prearmed_segments[segment] = {
                "intent": rule_metrics(
                    segment_universe[segment],
                    {(row.run_id, row.mint) for row in candidate_rows},
                ),
                "economics": economics,
            }
        rules[f"prearmed_repeat_creator_tweet_within_{maximum_age}s"] = {
            "execution_contract": (
                "creator-to-handle relationship was observed earlier; social status arrived "
                "before CREATE; CREATE path performs hash lookups only"
            ),
            "segments": prearmed_segments,
        }
    prior_index = wallet.FEATURE_NAMES.index("creator_prior_e4_attempts")
    seed_index = wallet.FEATURE_NAMES.index("creator_seed_sol")
    for name, minimum_prior, minimum_seed in (
        ("prearmed_10s_seed_2", 1, 2),
        ("prearmed_10s_prior_2", 2, 0),
        ("prearmed_10s_prior_2_seed_5", 2, 5),
    ):
        candidate_rows = [
            row
            for row in recurring
            if social[(row.run_id, row.mint)]["prearmed_handle_match"]
            and social[(row.run_id, row.mint)]["tweet_age_seconds"] is not None
            and 0
            <= social[(row.run_id, row.mint)]["tweet_age_seconds"]
            <= 10
            and row.features[prior_index] >= minimum_prior
            and row.features[seed_index] >= minimum_seed
        ]
        rules[name] = {
            "parameters_frozen_from_prior_social_epoch": {
                "maximum_tweet_age_seconds": 10,
                "minimum_prior_e4_attempts": minimum_prior,
                "minimum_creator_seed_sol": minimum_seed,
            },
            "segments": evaluate_segments(candidate_rows),
        }
    result = {
        "schema_version": SCHEMA_VERSION,
        "scientific_status": "EXPLORATORY_BACKFILL_NOT_UNTOUCHED",
        "causal_contract": (
            "social URL was present in immutable CREATE metadata; status timestamp is "
            "decoded from the status snowflake and must precede CREATE"
        ),
        "holdout_audit": audit,
        "coverage": {
            "holdout_launches": len(unique),
            "recurring_creator_launches": len(recurring),
            "metadata_available": sum(v["metadata_available"] for v in social.values()),
            "twitter_available": sum(v["twitter_available"] for v in social.values()),
            "status_available": sum(v["status_available"] for v in social.values()),
        },
        "social_source_runs": sorted(source_runs, key=int),
        "strictly_after_social_source_run": source_max_run,
        "rules": rules,
        "exit_policy_reused_without_reoptimisation": policy_payload,
        "hot_path_benchmark": benchmark_selector(),
        "production_paths_changed": 0,
        "golden_thesis_approved": False,
    }
    wallet.write_json(root / "artifacts/e4-v12-holdout-social-backfill.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    result = run(args.repo_root.resolve())
    print(json.dumps({"coverage": result["coverage"], "rules": result["rules"]}))


if __name__ == "__main__":
    main()

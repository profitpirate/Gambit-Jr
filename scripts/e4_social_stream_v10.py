#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import aiohttp

X_STREAM_URL = "https://api.x.com/2/tweets/search/stream"
X_RULES_URL = "https://api.x.com/2/tweets/search/stream/rules"


def append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", buffering=1) as handle:
        handle.write(json.dumps(dict(payload), separators=(",", ":"), ensure_ascii=True) + "\n")
        handle.flush()


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"accounts": {}, "rules": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, Mapping):
        raise ValueError("social source config must be a JSON object")
    return dict(data)


def accounts(config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw = config.get("accounts") or config.get("handles") or {}
    if isinstance(raw, Mapping):
        return {
            str(key).lower().lstrip("@"): dict(value) if isinstance(value, Mapping) else {}
            for key, value in raw.items()
        }
    output: dict[str, dict[str, Any]] = {}
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, Mapping):
            continue
        handle = str(row.get("handle") or row.get("username") or "").lower().lstrip("@")
        if handle:
            output[handle] = dict(row)
    return output


def desired_rules(config: Mapping[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for handle, row in accounts(config).items():
        if str(row.get("enabled", True)).lower() in {"0", "false", "no", "off"}:
            continue
        rows.append({"value": f"from:{handle} -is:retweet", "tag": f"gambit-account:{handle}"})
    for index, value in enumerate(config.get("rules") or []):
        if isinstance(value, str):
            rows.append({"value": value, "tag": f"gambit-rule:{index}"})
        elif isinstance(value, Mapping) and value.get("value"):
            rows.append({"value": str(value["value"]), "tag": str(value.get("tag") or f"gambit-rule:{index}")})
    # X currently limits rule payload sizes. Keep the bridge deterministic and
    # force operators to split very large curated lists across processes.
    return rows[:1_000]


async def x_request(session: aiohttp.ClientSession, method: str, url: str, bearer: str, **kwargs: Any) -> Any:
    headers = dict(kwargs.pop("headers", {}))
    headers["Authorization"] = f"Bearer {bearer}"
    async with session.request(method, url, headers=headers, **kwargs) as response:
        text = await response.text()
        if response.status >= 400:
            raise RuntimeError(f"X API HTTP {response.status}: {text[:500]}")
        return json.loads(text) if text.strip() else {}


async def sync_rules(session: aiohttp.ClientSession, bearer: str, config: Mapping[str, Any]) -> None:
    if str(os.getenv("E4_X_MANAGE_RULES", "true")).lower() in {"0", "false", "no", "off"}:
        return
    current = await x_request(session, "GET", X_RULES_URL, bearer)
    existing = list(current.get("data") or []) if isinstance(current, Mapping) else []
    gambit = [row for row in existing if str(row.get("tag") or "").startswith("gambit-")]
    if gambit:
        await x_request(
            session,
            "POST",
            X_RULES_URL,
            bearer,
            json={"delete": {"ids": [str(row["id"]) for row in gambit]}},
        )
    rules = desired_rules(config)
    for start in range(0, len(rules), 25):
        await x_request(session, "POST", X_RULES_URL, bearer, json={"add": rules[start : start + 25]})


def authority_for(handle: str, config: Mapping[str, Any]) -> float:
    row = accounts(config).get(handle.lower().lstrip("@"), {})
    value = row.get("authority") or row.get("score") or row.get("authority_score") or 0.0
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if score > 1:
        score /= 100.0
    return min(1.0, max(0.0, score))


def engagement_velocity(tweet: Mapping[str, Any], observed_ns: int) -> float:
    metrics = tweet.get("public_metrics") or {}
    interactions = sum(
        float(metrics.get(key) or 0.0)
        for key in ("like_count", "retweet_count", "reply_count", "quote_count", "bookmark_count")
    )
    created = tweet.get("created_at")
    age = 1.0
    if isinstance(created, str):
        try:
            from datetime import datetime

            parsed = datetime.fromisoformat(created.replace("Z", "+00:00"))
            age = max(1.0, observed_ns / 1e9 - parsed.timestamp())
        except Exception:
            pass
    return min(1.0, interactions / max(100.0, age * 25.0))


async def run_x(args: argparse.Namespace, stop: asyncio.Event) -> None:
    bearer = args.bearer or os.getenv("X_BEARER_TOKEN")
    if not bearer:
        raise RuntimeError("X_BEARER_TOKEN is required unless --stdin-jsonl is used")
    config = load_config(args.config)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=95)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        await sync_rules(session, bearer, config)
        backoff = 1.0
        while not stop.is_set():
            try:
                params = {
                    "tweet.fields": "created_at,author_id,public_metrics,entities,lang",
                    "expansions": "author_id",
                    "user.fields": "username,public_metrics,verified,verified_type",
                }
                headers = {"Authorization": f"Bearer {bearer}"}
                async with session.get(X_STREAM_URL, headers=headers, params=params) as response:
                    if response.status >= 400:
                        text = await response.text()
                        raise RuntimeError(f"X stream HTTP {response.status}: {text[:500]}")
                    backoff = 1.0
                    async for raw in response.content:
                        if stop.is_set():
                            break
                        raw = raw.strip()
                        if not raw:
                            continue
                        payload = json.loads(raw)
                        tweet = payload.get("data") or {}
                        users = {
                            str(row.get("id")): row
                            for row in ((payload.get("includes") or {}).get("users") or [])
                            if isinstance(row, Mapping)
                        }
                        author = users.get(str(tweet.get("author_id")), {})
                        handle = str(author.get("username") or tweet.get("author_id") or "unknown").lower()
                        observed_ns = time.time_ns()
                        created_ns = observed_ns
                        if tweet.get("created_at"):
                            try:
                                from datetime import datetime

                                created_ns = int(datetime.fromisoformat(str(tweet["created_at"]).replace("Z", "+00:00")).timestamp() * 1e9)
                            except Exception:
                                pass
                        row = {
                            "source": "x-filtered-stream",
                            "source_account": handle,
                            "post_id": str(tweet.get("id") or ""),
                            "text": str(tweet.get("text") or ""),
                            "created_ns": created_ns,
                            "observed_ns": observed_ns,
                            "authority": max(
                                authority_for(handle, config),
                                min(1.0, float((author.get("public_metrics") or {}).get("followers_count") or 0) / 250_000.0),
                            ),
                            "engagement_velocity": engagement_velocity(tweet, observed_ns),
                            "provenance": "x-api-filtered-stream",
                            "raw_matching_rules": payload.get("matching_rules") or [],
                        }
                        append_jsonl(args.output, row)
                        print(json.dumps({"social_signal": row["post_id"], "account": handle, "observed_ns": observed_ns}), flush=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(json.dumps({"x_stream_error": f"{type(exc).__name__}: {exc}", "backoff": backoff}), flush=True)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(60.0, backoff * 2.0)


async def run_stdin(args: argparse.Namespace, stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        payload = json.loads(line)
        if not isinstance(payload, Mapping):
            continue
        row = dict(payload)
        row.setdefault("observed_ns", time.time_ns())
        row.setdefault("created_ns", row["observed_ns"])
        row.setdefault("source", "stdin-social")
        row.setdefault("source_account", "unknown")
        row.setdefault("authority", 0.0)
        row.setdefault("engagement_velocity", 0.0)
        append_jsonl(args.output, row)


async def main_async(args: argparse.Namespace) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    if args.stdin_jsonl:
        await run_stdin(args, stop)
    else:
        await run_x(args, stop)
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Gambit E4 V10 real-time social narrative bridge")
    value.add_argument("--config", type=Path, default=Path("models/e4/e4-social-sources.json"))
    value.add_argument("--output", type=Path, default=Path(os.getenv("E4_SOCIAL_SIGNAL_JOURNAL", "var/e4/social-stream.jsonl")))
    value.add_argument("--bearer")
    value.add_argument("--stdin-jsonl", action="store_true")
    return value


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async(parser().parse_args())))

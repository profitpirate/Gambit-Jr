#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from scripts import e4_v12_baseline_registry as baseline

BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
IDENTITY_KEYS = {
    "creator",
    "creator_address",
    "creator_wallet",
    "deployer",
    "deployer_address",
    "dev",
    "dev_wallet",
    "wallet",
    "address",
    "funder",
    "funding_wallet",
    "funded_by",
    "source_wallet",
    "parent_wallet",
    "fee_payer",
    "feePayer",
    "payer",
    "owner",
    "authority",
}
GROUP_KEYS = {
    "related_wallets",
    "relatedWallets",
    "cluster_members",
    "members",
    "wallets",
    "aliases",
    "funding_chain",
    "first_buyers",
}


def is_address(value: Any) -> bool:
    return bool(BASE58_RE.match(str(value or "")))


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}
        self.rank: dict[str, int] = {}

    def add(self, value: str) -> None:
        if value not in self.parent:
            self.parent[value] = value
            self.rank[value] = 0

    def find(self, value: str) -> str:
        self.add(value)
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        a = self.find(left)
        b = self.find(right)
        if a == b:
            return
        if self.rank[a] < self.rank[b]:
            a, b = b, a
        self.parent[b] = a
        if self.rank[a] == self.rank[b]:
            self.rank[a] += 1


def git_output(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args],
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()


def candidate_paths(repo: Path, ref: str) -> list[str]:
    paths = git_output(repo, "ls-tree", "-r", "--name-only", ref).splitlines()
    output = []
    for path in paths:
        lower = path.lower()
        if not lower.endswith(".json"):
            continue
        if not (lower.startswith("models/e4/") or lower.startswith("docs/research/")):
            continue
        if any(term in lower for term in ("identity", "funder", "funding", "creator", "deployer", "wallet", "registry", "forensic")):
            output.append(path)
    return output


def commit_before(repo: Path, ref: str, path: str, cutoff_epoch: float) -> str:
    cutoff = dt.datetime.fromtimestamp(cutoff_epoch, tz=dt.timezone.utc).isoformat()
    try:
        return git_output(repo, "rev-list", "-1", f"--before={cutoff}", ref, "--", path)
    except Exception:
        return ""


def payload_at(repo: Path, commit: str, path: str) -> Any:
    try:
        return json.loads(git_output(repo, "show", f"{commit}:{path}"))
    except Exception:
        return None


def addresses_in(value: Any) -> list[str]:
    output: list[str] = []
    if is_address(value):
        output.append(str(value))
    elif isinstance(value, Mapping):
        for item in value.values():
            output.extend(addresses_in(item))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            output.extend(addresses_in(item))
    return list(dict.fromkeys(output))


def walk_relationships(value: Any) -> Iterable[tuple[list[str], str]]:
    if isinstance(value, Mapping):
        direct: list[str] = []
        for key, item in value.items():
            if key in IDENTITY_KEYS:
                direct.extend(addresses_in(item))
            elif key in GROUP_KEYS:
                direct.extend(addresses_in(item))
        direct = list(dict.fromkeys(direct))
        if len(direct) >= 2:
            yield direct, "record"
        for key, item in value.items():
            if is_address(key) and isinstance(item, Mapping):
                nested = [str(key)]
                for nested_key, nested_value in item.items():
                    if nested_key in IDENTITY_KEYS or nested_key in GROUP_KEYS:
                        nested.extend(addresses_in(nested_value))
                nested = list(dict.fromkeys(nested))
                if len(nested) >= 2:
                    yield nested, "address-key"
            if isinstance(item, (Mapping, list, tuple)):
                yield from walk_relationships(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from walk_relationships(item)


def build_registry(repo: Path, ref: str, cutoff_epoch: float) -> dict[str, Any]:
    uf = UnionFind()
    sources: list[dict[str, Any]] = []
    relationship_count = 0
    for path in candidate_paths(repo, ref):
        commit = commit_before(repo, ref, path, cutoff_epoch)
        if not commit:
            continue
        payload = payload_at(repo, commit, path)
        if payload is None:
            continue
        file_relationships = 0
        for addresses, _ in walk_relationships(payload):
            anchor = addresses[0]
            uf.add(anchor)
            for address in addresses[1:]:
                uf.union(anchor, address)
            file_relationships += 1
            relationship_count += 1
        if file_relationships:
            sources.append({"path": path, "commit": commit, "relationships": file_relationships})

    members_by_root: dict[str, list[str]] = defaultdict(list)
    for address in sorted(uf.parent):
        members_by_root[uf.find(address)].append(address)
    clusters = [members for members in members_by_root.values() if len(members) >= 2]
    clusters.sort(key=lambda members: (-len(members), members[0]))
    wallet_to_cluster: dict[str, str] = {}
    cluster_payload: dict[str, Any] = {}
    baseline_registry = baseline.build_registry(repo, ref, cutoff_epoch)
    creator_stats = baseline_registry.get("creators") or {}

    for index, members in enumerate(clusters, start=1):
        cluster_id = f"e4id-{index:06d}"
        wins = losses = trades = 0
        stat_members = 0
        for member in members:
            wallet_to_cluster[member] = cluster_id
            stats = creator_stats.get(member)
            if isinstance(stats, Mapping):
                stat_members += 1
                wins += int(stats.get("wins") or 0)
                losses += int(stats.get("losses") or 0)
                trades += int(stats.get("trades") or 0)
        trades = max(trades, wins + losses)
        cluster_payload[cluster_id] = {
            "members": members,
            "member_count": len(members),
            "stat_member_count": stat_members,
            "wins": wins,
            "losses": losses,
            "trades": trades,
            "win_rate": wins / trades if trades else 0.0,
        }

    return {
        "version": "e4-v12-causal-identity-cluster-registry-v1",
        "cutoff_epoch": cutoff_epoch,
        "ref": ref,
        "source_files": sources,
        "relationship_count": relationship_count,
        "cluster_count": len(cluster_payload),
        "wallet_count": len(wallet_to_cluster),
        "wallet_to_cluster": dict(sorted(wallet_to_cluster.items())),
        "clusters": cluster_payload,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build creator/funder clusters from repository state before a causal cutoff")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--cutoff-epoch", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    registry = build_registry(args.repo, args.ref, args.cutoff_epoch)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"cluster_count": registry["cluster_count"], "wallet_count": registry["wallet_count"], "relationship_count": registry["relationship_count"]}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

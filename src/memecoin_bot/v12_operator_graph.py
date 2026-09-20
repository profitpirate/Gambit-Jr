"""Conservative creator/operator identity clustering for V12 research."""
from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS v12_creator_identity(
    creator TEXT PRIMARY KEY,
    funding_wallet TEXT,
    metadata_host TEXT,
    social_handle TEXT,
    first_seen_ns INTEGER NOT NULL,
    last_seen_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS v12_operator_clusters(
    creator TEXT PRIMARY KEY,
    cluster_id TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence TEXT NOT NULL,
    updated_ns INTEGER NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class ClusterMember:
    creator: str
    cluster_id: str
    confidence: float
    evidence: str


class OperatorGraph:
    """Cluster creators only when identity evidence is strong enough.

    Cluster membership never grants live trading authority; it is exposed to the
    apprenticeship/analyst layer so wallet rotation does not erase context.
    """

    def __init__(self, database: Path | sqlite3.Connection):
        if isinstance(database, sqlite3.Connection):
            self.conn = database
            self.owns_connection = False
        else:
            database.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(database, timeout=5, isolation_level=None, check_same_thread=False)
            self.owns_connection = True
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        if self.owns_connection:
            self.conn.close()

    @staticmethod
    def _norm(value: str | None) -> str:
        return str(value or "").strip().lower().lstrip("@")

    def observe(
        self,
        creator: str,
        *,
        funding_wallet: str | None = None,
        metadata_host: str | None = None,
        social_handle: str | None = None,
        observed_ns: int | None = None,
    ) -> None:
        creator = str(creator or "").strip()
        if not creator:
            return
        now = int(observed_ns or time.time_ns())
        current = self.conn.execute(
            "SELECT * FROM v12_creator_identity WHERE creator=?",
            (creator,),
        ).fetchone()
        funding = str(funding_wallet or (current["funding_wallet"] if current else "") or "")
        host = self._norm(metadata_host or (current["metadata_host"] if current else ""))
        handle = self._norm(social_handle or (current["social_handle"] if current else ""))
        first = int(current["first_seen_ns"]) if current else now
        self.conn.execute(
            """
            INSERT INTO v12_creator_identity(
                creator,funding_wallet,metadata_host,social_handle,first_seen_ns,last_seen_ns
            ) VALUES(?,?,?,?,?,?)
            ON CONFLICT(creator) DO UPDATE SET
                funding_wallet=excluded.funding_wallet,
                metadata_host=excluded.metadata_host,
                social_handle=excluded.social_handle,
                last_seen_ns=excluded.last_seen_ns
            """,
            (creator, funding, host, handle, first, now),
        )

    def _edge(self, left: sqlite3.Row, right: sqlite3.Row) -> tuple[float, list[str]]:
        evidence: list[str] = []
        score = 0.0
        lf, rf = str(left["funding_wallet"] or ""), str(right["funding_wallet"] or "")
        lh, rh = str(left["metadata_host"] or ""), str(right["metadata_host"] or "")
        ls, rs = str(left["social_handle"] or ""), str(right["social_handle"] or "")
        if lf and lf == rf:
            score += 1.0
            evidence.append("same_funding_wallet")
        if ls and ls == rs:
            score += 0.90
            evidence.append("same_social_handle")
        if lh and lh == rh:
            score += 0.40
            evidence.append("same_metadata_host")
        return score, evidence

    def rebuild(self) -> list[ClusterMember]:
        rows = self.conn.execute(
            "SELECT * FROM v12_creator_identity ORDER BY creator"
        ).fetchall()
        parent = {str(row["creator"]): str(row["creator"]) for row in rows}
        edge_evidence: dict[tuple[str, str], tuple[float, str]] = {}

        def find(value: str) -> str:
            while parent[value] != value:
                parent[value] = parent[parent[value]]
                value = parent[value]
            return value

        def union(left: str, right: str) -> None:
            a, b = find(left), find(right)
            if a != b:
                parent[max(a, b)] = min(a, b)

        for index, left in enumerate(rows):
            for right in rows[index + 1 :]:
                score, evidence = self._edge(left, right)
                # A funding-wallet match stands alone. Otherwise require at least
                # two independent softer identity signals (social + metadata).
                if score >= 1.0 and (
                    "same_funding_wallet" in evidence or len(evidence) >= 2
                ):
                    lc, rc = str(left["creator"]), str(right["creator"])
                    union(lc, rc)
                    edge_evidence[(lc, rc)] = (min(1.0, score), "+".join(evidence))

        groups: dict[str, list[str]] = {}
        for creator in parent:
            groups.setdefault(find(creator), []).append(creator)

        now = time.time_ns()
        self.conn.execute("DELETE FROM v12_operator_clusters")
        result: list[ClusterMember] = []
        for creators in groups.values():
            if len(creators) < 2:
                continue
            cluster_id = hashlib.sha256("|".join(sorted(creators)).encode()).hexdigest()[:20]
            confidence = 0.0
            evidence_strings: set[str] = set()
            for (left, right), (score, evidence) in edge_evidence.items():
                if left in creators and right in creators:
                    confidence = max(confidence, score)
                    evidence_strings.add(evidence)
            evidence = ",".join(sorted(evidence_strings))
            for creator in creators:
                self.conn.execute(
                    """
                    INSERT INTO v12_operator_clusters(
                        creator,cluster_id,confidence,evidence,updated_ns
                    ) VALUES(?,?,?,?,?)
                    """,
                    (creator, cluster_id, confidence, evidence, now),
                )
                result.append(
                    ClusterMember(creator, cluster_id, confidence, evidence)
                )
        return result

    def related(self, creator: str) -> list[ClusterMember]:
        row = self.conn.execute(
            "SELECT cluster_id FROM v12_operator_clusters WHERE creator=?",
            (str(creator),),
        ).fetchone()
        if row is None:
            return []
        return [
            ClusterMember(
                str(item["creator"]),
                str(item["cluster_id"]),
                float(item["confidence"]),
                str(item["evidence"]),
            )
            for item in self.conn.execute(
                "SELECT * FROM v12_operator_clusters WHERE cluster_id=? ORDER BY creator",
                (str(row["cluster_id"]),),
            )
        ]

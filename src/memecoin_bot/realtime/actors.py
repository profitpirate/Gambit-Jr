from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from itertools import combinations
from typing import Any

from memecoin_bot.realtime.learning import wilson_interval


def _json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"), sort_keys=True)


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include timezone: {value}")
    return parsed.astimezone(UTC)


def _stage(age_seconds: float, migration_at: str | None, observed_at: datetime) -> str:
    if migration_at and observed_at >= _timestamp(migration_at):
        return "MIGRATION_BUYER" if age_seconds <= 900 else "POST_MIGRATION_PULLBACK"
    if age_seconds <= 30:
        return "BONDING_SNIPER"
    if age_seconds <= 120:
        return "EARLY_CURVE"
    if age_seconds <= 600:
        return "MID_CURVE"
    return "UNKNOWN"


class ActorIntelligence:
    """Point-in-time creator, wallet strategy, copyability, consensus, and funder graph."""

    def __init__(self, store: Any):
        self.store = store

    def creator_profile_at(self, chain: str, creator: str, decision_at: str) -> dict[str, Any]:
        decision = _timestamp(decision_at)
        rows = [
            dict(row)
            for row in self.store.conn.execute(
                "SELECT cl.*,o.max_multiple_from_discovery,o.final_lifecycle_state,o.updated_at,"
                "t.first_discovered_at FROM creator_launches_v14 cl JOIN tokens t ON t.id=cl.token_id "
                "LEFT JOIN token_outcomes o ON o.token_id=t.id WHERE t.chain=? AND cl.creator_address=? "
                "AND cl.launched_at<? ORDER BY cl.launched_at",
                (chain, creator, decision_at),
            )
            if row["updated_at"] is None or _timestamp(str(row["updated_at"])) <= decision
        ]
        peaks = [float(row.get("max_multiple_from_discovery") or 0) for row in rows]
        failures = [
            str(row.get("final_lifecycle_state") or "").startswith("FAILED")
            or float(row.get("max_multiple_from_discovery") or 0) < 0.3
            for row in rows
        ]
        migrations = self.store.conn.execute(
            "SELECT COUNT(*) FROM creator_launches_v14 cl JOIN migration_continuity_v15 m "
            "ON m.token_id=cl.token_id WHERE cl.creator_address=? AND m.migration_timestamp<?",
            (creator, decision_at),
        ).fetchone()[0]
        launch_metrics = []
        for row in rows:
            initial = self.store.conn.execute(
                "SELECT market_cap_usd FROM token_snapshots WHERE token_id=? AND captured_at<=? "
                "AND market_cap_usd IS NOT NULL ORDER BY captured_at LIMIT 1",
                (row["token_id"], decision_at),
            ).fetchone()
            migration = self.store.conn.execute(
                "SELECT migration_timestamp FROM migration_continuity_v15 WHERE token_id=? "
                "AND migration_timestamp<=?",
                (row["token_id"], decision_at),
            ).fetchone()
            creator_trades = list(
                self.store.conn.execute(
                    "SELECT side,event_timestamp FROM token_event_timeline_v15 WHERE token_id=? "
                    "AND actor=? AND available_timestamp<=? ORDER BY event_timestamp",
                    (row["token_id"], creator, decision_at),
                )
            )
            launch_metrics.append(
                {
                    "initial_market_cap": float(initial[0]) if initial and initial[0] else None,
                    "migration_seconds": (
                        max(
                            0.0,
                            (
                                _timestamp(str(migration[0]))
                                - _timestamp(str(row["launched_at"]))
                            ).total_seconds(),
                        )
                        if migration and migration[0]
                        else None
                    ),
                    "self_buy": any(trade["side"] == "buy" for trade in creator_trades),
                    "self_sell": any(trade["side"] == "sell" for trade in creator_trades),
                }
            )
        funders = [
            str(row[0])
            for row in self.store.conn.execute(
                "SELECT funder_wallet FROM wallet_funding_edges_v15 WHERE chain=? "
                "AND funded_wallet=? AND first_funded_at<=?",
                (chain, creator, decision_at),
            )
        ]
        self_buy_launches = sum(row["self_buy"] for row in launch_metrics)
        return {
            "creator": creator,
            "available_at": decision_at,
            "launch_count": len(rows),
            "2x_rate": sum(peak >= 2 for peak in peaks) / len(peaks) if peaks else None,
            "5x_rate": sum(peak >= 5 for peak in peaks) / len(peaks) if peaks else None,
            "10x_rate": sum(peak >= 10 for peak in peaks) / len(peaks) if peaks else None,
            "terminal_failure_rate": sum(failures) / len(failures) if failures else None,
            "migration_rate": migrations / len(rows) if rows else None,
            "median_launch_market_cap": statistics.median(
                row["initial_market_cap"]
                for row in launch_metrics
                if row["initial_market_cap"] is not None
            )
            if any(row["initial_market_cap"] is not None for row in launch_metrics)
            else None,
            "median_time_to_migration_seconds": statistics.median(
                row["migration_seconds"]
                for row in launch_metrics
                if row["migration_seconds"] is not None
            )
            if any(row["migration_seconds"] is not None for row in launch_metrics)
            else None,
            "creator_self_buy_rate": (
                self_buy_launches / len(launch_metrics) if launch_metrics else None
            ),
            "creator_sell_rate": (
                sum(row["self_sell"] for row in launch_metrics) / len(launch_metrics)
                if launch_metrics
                else None
            ),
            "inventory_retention_rate": (
                sum(row["self_buy"] and not row["self_sell"] for row in launch_metrics)
                / self_buy_launches
                if self_buy_launches
                else None
            ),
            "funder_reuse": len(funders) - len(set(funders)),
            "distinct_prior_funders": len(set(funders)),
            "wallet_rotation": "UNKNOWN_NO_IDENTITY_RESOLUTION",
            "narrative_reuse": "UNKNOWN_UNLESS_TIMESTAMPED_NARRATIVE_MEMBERSHIP_EXISTS",
            "sample_confidence": "PROVEN" if len(rows) >= 20 else "PROVISIONAL" if rows else "UNKNOWN",
            "point_in_time": True,
        }

    def build_wallet_copyability(
        self,
        *,
        matured_before: str,
        horizon_hours: float = 48,
        delays: tuple[int, ...] = (5, 15, 30, 60, 120),
        regime: str = "ALL",
    ) -> dict[str, Any]:
        cutoff = _timestamp(matured_before)
        buys = [
            dict(row)
            for row in self.store.conn.execute(
                "SELECT e.*,r.launched_at,r.migration_completed_at,t.chain FROM token_event_timeline_v15 e "
                "JOIN token_realtime_state r ON r.token_id=e.token_id JOIN tokens t ON t.id=e.token_id "
                "WHERE e.event_type='TOKEN_TRADE' AND e.side='buy' AND e.event_timestamp<? "
                "AND e.actor IS NOT NULL AND e.actor!='UNKNOWN' ORDER BY e.event_timestamp",
                (matured_before,),
            )
        ]
        observations: list[dict[str, Any]] = []
        for buy in buys:
            bought_at = _timestamp(str(buy["event_timestamp"]))
            maturity = bought_at + timedelta(hours=horizon_hours)
            if maturity > cutoff:
                continue
            launch = _timestamp(str(buy["launched_at"]))
            age = max(0.0, (bought_at - launch).total_seconds())
            strategy = _stage(age, buy.get("migration_completed_at"), bought_at)
            sell = self.store.conn.execute(
                "SELECT MIN(event_timestamp) FROM token_event_timeline_v15 WHERE token_id=? "
                "AND actor=? AND side='sell' AND event_timestamp>=?",
                (buy["token_id"], buy["actor"], buy["event_timestamp"]),
            ).fetchone()[0]
            for delay in delays:
                entry_at = bought_at + timedelta(seconds=delay)
                entry = self.store.conn.execute(
                    "SELECT captured_at,market_cap_usd,price_usd,liquidity_usd FROM token_snapshots "
                    "WHERE token_id=? AND captured_at>=? AND captured_at<=? ORDER BY captured_at LIMIT 1",
                    (buy["token_id"], entry_at.isoformat(), maturity.isoformat()),
                ).fetchone()
                if not entry or not entry["market_cap_usd"] or float(entry["market_cap_usd"]) <= 0:
                    continue
                future = list(
                    self.store.conn.execute(
                        "SELECT market_cap_usd FROM token_snapshots WHERE token_id=? AND captured_at>=? "
                        "AND captured_at<=? AND market_cap_usd IS NOT NULL ORDER BY captured_at",
                        (buy["token_id"], entry["captured_at"], maturity.isoformat()),
                    )
                )
                values = [float(row[0]) / float(entry["market_cap_usd"]) for row in future]
                if not values:
                    continue
                observations.append(
                    {
                        "wallet": buy["actor"],
                        "chain": buy["chain"],
                        "token_id": buy["token_id"],
                        "stage": strategy,
                        "delay": delay,
                        "peak": max(values),
                        "drawdown": min(values) - 1,
                        "liquidity": entry["liquidity_usd"],
                        "first_wallet_sell": sell,
                        "available_at": maturity.isoformat(),
                    }
                )
        groups: dict[tuple[str, str, str, int], list[dict[str, Any]]] = defaultdict(list)
        for row in observations:
            groups[(row["chain"], row["wallet"], row["stage"], row["delay"])].append(row)
        persisted = 0
        with self.store._lock, self.store.conn:
            for (chain, wallet, stage, delay), rows in groups.items():
                for target in (2, 5, 10, 20):
                    wins = sum(float(row["peak"]) >= target for row in rows)
                    interval = wilson_interval(wins, len(rows))
                    strategy = stage if len(rows) >= 3 else "UNKNOWN"
                    available = max(str(row["available_at"]) for row in rows)
                    self.store.conn.execute(
                        "INSERT INTO wallet_strategy_profiles_v15(chain,wallet_address,stage,objective,"
                        "regime,copy_delay_seconds,strategy,sample,wins,failures,precision,wilson_low,"
                        "wilson_high,median_remaining_upside,median_drawdown,available_at,evidence_json) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(chain,wallet_address,stage,"
                        "objective,regime,copy_delay_seconds,available_at) DO UPDATE SET evidence_json="
                        "excluded.evidence_json",
                        (
                            chain,
                            wallet,
                            stage,
                            f"{target}X",
                            regime,
                            delay,
                            strategy,
                            len(rows),
                            wins,
                            len(rows) - wins,
                            wins / len(rows),
                            interval[0] if interval else None,
                            interval[1] if interval else None,
                            statistics.median(float(row["peak"]) for row in rows),
                            statistics.median(float(row["drawdown"]) for row in rows),
                            available,
                            _json(
                                {
                                    "one_hit_wonder_rejected": len(rows) < 3,
                                    "horizon_hours": horizon_hours,
                                    "point_in_time": True,
                                }
                            ),
                        ),
                    )
                    persisted += 1
        return {
            "buys_considered": len(buys),
            "copyability_observations": len(observations),
            "profiles_persisted": persisted,
            "delays": list(delays),
            "matured_before": matured_before,
        }

    def independent_consensus(
        self,
        *,
        token_id: int,
        decision_at: str,
        stage: str,
        objective: str = "2X",
        copy_delay_seconds: int = 30,
    ) -> dict[str, Any]:
        buyers = {
            str(row[0])
            for row in self.store.conn.execute(
                "SELECT DISTINCT actor FROM token_event_timeline_v15 WHERE token_id=? AND side='buy' "
                "AND available_timestamp<=? AND actor IS NOT NULL AND actor!='UNKNOWN'",
                (token_id, decision_at),
            )
        }
        profiles: dict[str, dict[str, Any]] = {}
        for wallet in buyers:
            row = self.store.conn.execute(
                "SELECT * FROM wallet_strategy_profiles_v15 WHERE wallet_address=? AND stage=? "
                "AND objective=? AND copy_delay_seconds=? AND available_at<=? ORDER BY available_at DESC LIMIT 1",
                (wallet, stage, objective, copy_delay_seconds, decision_at),
            ).fetchone()
            if row and int(row["sample"]) >= 5 and float(row["wilson_low"] or 0) > 0:
                profiles[wallet] = dict(row)
        parent = {wallet: wallet for wallet in profiles}

        def find(value: str) -> str:
            while parent[value] != value:
                parent[value] = parent[parent[value]]
                value = parent[value]
            return value

        def union(left: str, right: str) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        funders: dict[str, set[str]] = defaultdict(set)
        for row in self.store.conn.execute(
            "SELECT funded_wallet,funder_wallet FROM wallet_funding_edges_v15 WHERE funded_wallet IN "
            f"({','.join('?' for _ in profiles)}) AND first_funded_at<=?" if profiles else "SELECT NULL,NULL WHERE 0",
            (*profiles, decision_at) if profiles else (),
        ):
            funders[str(row[1])].add(str(row[0]))
        for members in funders.values():
            for left, right in combinations(sorted(members), 2):
                union(left, right)
        slot_groups: dict[str, set[str]] = defaultdict(set)
        for row in self.store.conn.execute(
            "SELECT actor,slot_or_block FROM token_event_timeline_v15 WHERE token_id=? "
            "AND side='buy' AND likely_bundled=1 AND available_timestamp<=?",
            (token_id, decision_at),
        ):
            actor = str(row[0] or "")
            if actor in profiles and row[1] is not None:
                slot_groups[str(row[1])].add(actor)
        for members in slot_groups.values():
            for left, right in combinations(sorted(members), 2):
                union(left, right)
        if len(profiles) >= 2:
            placeholders = ",".join("?" for _ in profiles)
            co_purchases: dict[tuple[str, str], int] = defaultdict(int)
            token_groups: dict[int, set[str]] = defaultdict(set)
            for row in self.store.conn.execute(
                f"SELECT token_id,actor FROM token_event_timeline_v15 WHERE side='buy' "
                f"AND actor IN ({placeholders}) AND available_timestamp<=?",
                (*profiles, decision_at),
            ):
                token_groups[int(row[0])].add(str(row[1]))
            for members in token_groups.values():
                for pair in combinations(sorted(members), 2):
                    co_purchases[pair] += 1
            for (left, right), shared in co_purchases.items():
                if shared >= 3:
                    union(left, right)
        clusters = {find(wallet) for wallet in profiles}
        strategies = {str(row["strategy"]) for row in profiles.values()}
        return {
            "raw_smart_wallet_count": len(profiles),
            "independent_smart_wallet_count": len(clusters),
            "copyable_consensus": len(clusters) >= 2,
            "strategy_matched_consensus": len(clusters) >= 2 and len(strategies) == 1,
            "linked_wallet_share": (
                1 - len(clusters) / len(profiles) if profiles else None
            ),
            "profile_sample_floor": min((int(row["sample"]) for row in profiles.values()), default=0),
            "point_in_time": True,
        }

    def funder_graph(self, token_id: int, decision_at: str) -> dict[str, Any]:
        buyers = {
            str(row[0])
            for row in self.store.conn.execute(
                "SELECT DISTINCT actor FROM token_event_timeline_v15 WHERE token_id=? AND side='buy' "
                "AND available_timestamp<=? AND actor IS NOT NULL",
                (token_id, decision_at),
            )
        }
        if not buyers:
            return {
                "state": "UNKNOWN_NO_BUYER_IDENTITIES",
                "funder_independence": None,
                "creator_link_score": None,
            }
        placeholders = ",".join("?" for _ in buyers)
        rows = [
            dict(row)
            for row in self.store.conn.execute(
                f"SELECT * FROM wallet_funding_edges_v15 WHERE funded_wallet IN ({placeholders}) "
                "AND first_funded_at<=?",
                (*sorted(buyers), decision_at),
            )
        ]
        if not rows:
            return {
                "state": "UNKNOWN_NO_FUNDING_EVIDENCE",
                "funder_independence": None,
                "creator_link_score": None,
            }
        by_funder: dict[str, set[str]] = defaultdict(set)
        for row in rows:
            by_funder[str(row["funder_wallet"])].add(str(row["funded_wallet"]))
        largest = max((len(values) for values in by_funder.values()), default=0)
        recencies = [
            max(0.0, (_timestamp(decision_at) - _timestamp(str(row["last_funded_at"]))).total_seconds())
            for row in rows
        ]
        creator_row = self.store.conn.execute(
            "SELECT creator_address FROM token_realtime_state WHERE token_id=?", (token_id,)
        ).fetchone()
        creator = str(creator_row[0]) if creator_row and creator_row[0] else None
        creator_funders = (
            {
                str(row[0])
                for row in self.store.conn.execute(
                    "SELECT funder_wallet FROM wallet_funding_edges_v15 WHERE chain=("
                    "SELECT chain FROM tokens WHERE id=?) AND funded_wallet=? AND first_funded_at<=?",
                    (token_id, creator, decision_at),
                )
            }
            if creator
            else set()
        )
        creator_linked = sum(
            str(row["funder_wallet"]) == creator
            or str(row["funder_wallet"]) in creator_funders
            for row in rows
        )
        return {
            "state": "OBSERVED",
            "funder_independence": len(by_funder) / len(buyers),
            "creator_link_score": creator_linked / len(rows),
            "cluster_size": largest,
            "funding_recency_seconds": statistics.median(recencies) if recencies else None,
            "funding_concentration": largest / len(buyers),
            "funders": len(by_funder),
            "buyer_coverage": len({row["funded_wallet"] for row in rows}) / len(buyers),
        }

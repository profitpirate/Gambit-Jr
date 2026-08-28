from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

SYSTEM_PROGRAM = "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s"


def _duckdb() -> Any:
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - research extra guard
        raise RuntimeError("install the research extra: pip install -e .[research]") from exc
    return duckdb


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def _source_manifest(corpus: Path) -> dict[str, Any]:
    files = sorted(path for path in corpus.rglob("*.parquet") if path.is_file())
    entries = [
        {
            "path": path.relative_to(corpus).as_posix(),
            "bytes": path.stat().st_size,
        }
        for path in files
    ]
    encoded = json.dumps(entries, separators=(",", ":"), sort_keys=True).encode()
    return {
        "parquet_files": len(files),
        "parquet_bytes": sum(row["bytes"] for row in entries),
        "metadata_manifest_sha256": hashlib.sha256(encoded).hexdigest(),
        "hash_scope": "relative_path_and_byte_size_not_content",
    }


def build_realtime_event_replay(
    database: str | Path,
    corpus: str | Path,
) -> dict[str, Any]:
    """Create a no-lookahead event replay over the longest legitimate local corpus.

    The source contains transaction events but no native account-state history. The
    replay therefore exposes exact trade/buyer/sell trajectories and explicitly
    leaves real reserves, funder linkage, bundles, and provider latency unavailable.
    """
    database_path, corpus_path = Path(database), Path(corpus)
    trade_glob = _sql_path(corpus_path / "trades" / "*.parquet")
    tokens_path = _sql_path(corpus_path / "tokens.parquet")
    if not (corpus_path / "tokens.parquet").exists():
        raise FileNotFoundError(corpus_path / "tokens.parquet")
    connection = _duckdb().connect(str(database_path))
    try:
        tables = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
        if "runner_autopsy_replay" not in tables:
            raise RuntimeError("runner_autopsy_replay must be built before realtime event replay")
        connection.execute(
            f"""
            CREATE OR REPLACE VIEW realtime_valid_events_v15 AS
            WITH source AS (
              SELECT t.*,m.creator,m.top10_pct_suspect,
                row_number() OVER (
                  PARTITION BY t.mint ORDER BY t.event_time,t.user_wallet,t.is_buy,t.sol_amount
                ) event_sequence,
                row_number() OVER (
                  PARTITION BY t.mint,t.user_wallet,t.is_buy
                  ORDER BY t.event_time,t.sol_amount
                ) actor_side_sequence
              FROM read_parquet('{trade_glob}') t
              LEFT JOIN read_parquet('{tokens_path}') m USING(mint)
              WHERE t.seconds_since_launch BETWEEN 0 AND 1800
                AND t.user_wallet <> '{SYSTEM_PROGRAM}'
                AND t.sol_amount IS NOT NULL
                AND t.token_amount IS NOT NULL
                AND t.price_sol IS NOT NULL
                AND t.token_amount*t.price_sol > 0
                AND t.sol_amount/(t.token_amount*t.price_sol) BETWEEN .01 AND 100
            )
            SELECT hash(mint,event_time,user_wallet,is_buy,sol_amount,event_sequence) event_id,
              mint canonical_token,'solana' AS "chain",'pumpfun' AS platform,
              event_time source_timestamp,event_time decision_timestamp,
              event_time available_timestamp_proxy,
              'HISTORICAL_PROXY_NO_PROVIDER_LATENCY' evidence_mode,
              event_sequence,seconds_since_launch,user_wallet actor,
              creator,top10_pct_suspect,
              CASE WHEN is_buy THEN 'WALLET_BUY' ELSE 'WALLET_SELL' END event_type,
              CASE WHEN is_buy THEN 'buy' ELSE 'sell' END side,
              sol_amount,token_amount,price_sol,market_cap_sol,
              is_buy AND actor_side_sequence=1 first_observed_buy,
              user_wallet=creator creator_linked
            FROM source
            """
        )
        connection.execute(
            """
            CREATE OR REPLACE VIEW realtime_event_bands_v15 AS
            WITH banded AS (
              SELECT *,CASE
                WHEN seconds_since_launch<=15 THEN '0-15'
                WHEN seconds_since_launch<=30 THEN '15-30'
                WHEN seconds_since_launch<=60 THEN '30-60'
                WHEN seconds_since_launch<=90 THEN '60-90'
                WHEN seconds_since_launch<=120 THEN '90-120'
                WHEN seconds_since_launch<=180 THEN '120-180'
                WHEN seconds_since_launch<=300 THEN '180-300'
                WHEN seconds_since_launch<=600 THEN '300-600'
                ELSE '600-1800' END band,
                CASE
                  WHEN seconds_since_launch<=15 THEN 1
                  WHEN seconds_since_launch<=30 THEN 2
                  WHEN seconds_since_launch<=60 THEN 3
                  WHEN seconds_since_launch<=90 THEN 4
                  WHEN seconds_since_launch<=120 THEN 5
                  WHEN seconds_since_launch<=180 THEN 6
                  WHEN seconds_since_launch<=300 THEN 7
                  WHEN seconds_since_launch<=600 THEN 8
                  ELSE 9 END band_order
              FROM realtime_valid_events_v15
            )
            SELECT canonical_token,band,band_order,min(source_timestamp) first_event_at,
              max(source_timestamp) last_event_at,count(*) trade_count,
              count_if(side='buy') buy_count,count_if(side='sell') sell_count,
              count(DISTINCT actor) raw_actor_count,
              count(DISTINCT actor) FILTER(side='buy') raw_buyer_count,
              count_if(first_observed_buy) new_buyer_count,
              sum(CASE WHEN side='buy' THEN sol_amount ELSE 0 END) buy_sol,
              sum(CASE WHEN side='sell' THEN sol_amount ELSE 0 END) sell_sol,
              sum(CASE WHEN side='buy' THEN sol_amount ELSE -sol_amount END) net_sol,
              avg(creator_linked::INTEGER) creator_linked_share,
              NULL::DOUBLE adjusted_independent_buyers,
              NULL::DOUBLE real_sol_reserve,
              'UNKNOWN_NO_LINKAGE_GRAPH' linkage_state,
              'UNAVAILABLE_NO_NATIVE_ACCOUNT_HISTORY' reserve_state
            FROM banded GROUP BY canonical_token,band,band_order
            """
        )
        connection.execute(
            """
            CREATE OR REPLACE VIEW realtime_token_trajectory_v15 AS
            WITH first_sell AS (
              SELECT canonical_token,min(seconds_since_launch) first_sell_seconds
              FROM realtime_valid_events_v15 WHERE side='sell' GROUP BY canonical_token
            ), outcomes AS (
              SELECT mint,max(peak_multiple) peak_multiple,
                bool_or(coalesce(terminal_failure,false)) terminal_failure,
                min(decision_at) first_research_decision,
                max(decision_at) last_research_decision
              FROM runner_autopsy_replay GROUP BY mint
            )
            SELECT e.canonical_token,any_value(e.creator) creator,
              bool_or(coalesce(e.top10_pct_suspect,false)) top10_pct_suspect,
              min(e.source_timestamp) first_event_at,
              max(e.source_timestamp) last_event_at,count(*) event_count,
              count_if(e.side='buy') buy_count,count_if(e.side='sell') sell_count,
              count(DISTINCT e.actor) raw_actor_count,
              count(DISTINCT e.actor) FILTER(e.side='buy') raw_buyer_count,
              f.first_sell_seconds,
              count(DISTINCT e.actor) FILTER(
                e.side='buy' AND e.seconds_since_launch>f.first_sell_seconds
              ) buyers_after_first_sell,
              o.peak_multiple,o.terminal_failure,o.first_research_decision,
              o.last_research_decision
            FROM realtime_valid_events_v15 e
            LEFT JOIN first_sell f USING(canonical_token)
            LEFT JOIN outcomes o ON o.mint=e.canonical_token
            GROUP BY e.canonical_token,f.first_sell_seconds,o.peak_multiple,
              o.terminal_failure,o.first_research_decision,o.last_research_decision
            """
        )
        counts = connection.execute(
            """
            SELECT count(*) events,count(DISTINCT canonical_token) tokens,
              min(source_timestamp) first_event,max(source_timestamp) last_event,
              count(DISTINCT canonical_token) FILTER(side='sell') tokens_with_sell,
              count(DISTINCT canonical_token) FILTER(first_observed_buy) tokens_with_buyer_arrival
            FROM realtime_valid_events_v15
            """
        ).fetchone()
        bands = [
            {
                "band": row[0],
                "tokens": int(row[1]),
                "events": int(row[2]),
                "raw_buyers": int(row[3] or 0),
                "buy_sol": float(row[4] or 0),
                "sell_sol": float(row[5] or 0),
            }
            for row in connection.execute(
                """
                SELECT band,count(*) tokens,sum(trade_count) events,
                  sum(raw_buyer_count) raw_buyers,sum(buy_sol) buy_sol,sum(sell_sol) sell_sol
                FROM realtime_event_bands_v15 GROUP BY band,band_order ORDER BY band_order
                """
            ).fetchall()
        ]
        manifest = {
            "version": "HISTORICAL_REALTIME_EVENT_REPLAY_V1",
            "truth_state": "MEASURED_TRANSACTION_REPLAY_PARTIAL_FEATURE_COVERAGE",
            "events": int(counts[0]),
            "tokens": int(counts[1]),
            "first_event": str(counts[2]),
            "last_event": str(counts[3]),
            "tokens_with_sell": int(counts[4]),
            "tokens_with_buyer_arrival": int(counts[5]),
            "bands": bands,
            "source": _source_manifest(corpus_path),
            "point_in_time": {
                "ordering": "event_time_then_deterministic_tie_break",
                "lookahead_features": False,
                "provider_availability_latency": "UNAVAILABLE",
                "available_timestamp_proxy": "event_time_replay_only",
            },
            "unavailable": {
                "real_sol_reserve": "no native account-state history in source corpus",
                "funder_graph": "no point-in-time funder edges in source corpus",
                "independent_wallet_linkage": "wallet identities present; linkage graph absent",
                "bundle_identity": "no exact bundle identifier in source corpus",
                "social_narrative": "not present in transaction corpus",
            },
        }
        connection.execute(
            "CREATE TABLE IF NOT EXISTS realtime_replay_manifests_v15("
            "version VARCHAR PRIMARY KEY,created_at TIMESTAMPTZ DEFAULT current_timestamp,"
            "manifest_json JSON NOT NULL)"
        )
        connection.execute(
            "INSERT OR REPLACE INTO realtime_replay_manifests_v15(version,manifest_json) "
            "VALUES(?,?)",
            (manifest["version"], json.dumps(manifest, default=str, sort_keys=True)),
        )
        return manifest
    finally:
        connection.close()


def write_replay_manifest(result: dict[str, Any], output: str | Path) -> None:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, default=str, sort_keys=True), encoding="utf-8")

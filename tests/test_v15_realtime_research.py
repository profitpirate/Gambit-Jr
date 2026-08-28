from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np

from memecoin_bot.historical.intelligence_v3_execution import ResearchData
from memecoin_bot.historical.realtime_replay import build_realtime_event_replay
from memecoin_bot.historical.realtime_research import _cohort_autopsy, _effects


def _copy_sql(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def test_real_transaction_replay_preserves_bands_sells_and_truthful_unknowns(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    trades = corpus / "trades"
    trades.mkdir(parents=True)
    source = duckdb.connect()
    source.execute(
        """
        CREATE TABLE trades AS SELECT * FROM (VALUES
          ('A','2026-06-05T00:00:01+00:00'::TIMESTAMPTZ,1.0,true,'W1',1.0,10.0,.1,10.0,'s1'),
          ('A','2026-06-05T00:00:20+00:00'::TIMESTAMPTZ,20.0,false,'W1',.5,5.0,.1,9.0,'s2'),
          ('B','2026-06-05T00:01:10+00:00'::TIMESTAMPTZ,10.0,true,'W2',2.0,20.0,.1,20.0,'s3')
        ) t(mint,event_time,seconds_since_launch,is_buy,user_wallet,sol_amount,
            token_amount,price_sol,market_cap_sol,tx_signature)
        """
    )
    source.execute(
        "CREATE TABLE tokens AS SELECT * FROM (VALUES "
        "('A','C1',false),('B','C2',false)) t(mint,creator,top10_pct_suspect)"
    )
    source.execute(f"COPY trades TO '{_copy_sql(trades / 'part.parquet')}' (FORMAT PARQUET)")
    source.execute(f"COPY tokens TO '{_copy_sql(corpus / 'tokens.parquet')}' (FORMAT PARQUET)")
    source.close()
    database = tmp_path / "research.duckdb"
    target = duckdb.connect(str(database))
    target.execute(
        "CREATE TABLE runner_autopsy_replay AS SELECT * FROM (VALUES "
        "('A',2.5,false,'2026-06-05T00:03:00+00:00'::TIMESTAMPTZ),"
        "('B',.5,true,'2026-06-05T00:04:00+00:00'::TIMESTAMPTZ)) "
        "t(mint,peak_multiple,terminal_failure,decision_at)"
    )
    target.close()
    result = build_realtime_event_replay(database, corpus)
    assert result["events"] == 3
    assert result["tokens"] == 2
    assert result["tokens_with_sell"] == 1
    assert result["unavailable"]["real_sol_reserve"]
    check = duckdb.connect(str(database), read_only=True)
    assert check.execute(
        "SELECT band,trade_count,raw_buyer_count,real_sol_reserve "
        "FROM realtime_event_bands_v15 WHERE canonical_token='A' ORDER BY band_order"
    ).fetchall() == [("0-15", 1, 1, None), ("15-30", 1, 0, None)]
    check.close()


def test_low_performance_autopsy_and_hypotheses_are_quantitative_not_approved() -> None:
    count = 30
    peaks = np.asarray([3.0 if index % 3 == 0 else 0.5 for index in range(count)])
    features = {
        "net_flow": np.asarray([2.0 if peak >= 2 else -1.0 for peak in peaks]),
        "buyers": np.asarray([5.0 if peak >= 2 else 1.0 for peak in peaks]),
        "log_market_cap": np.full(count, np.log1p(100.0)),
    }
    data = ResearchData(
        mint=np.asarray([f"T{index}" for index in range(count)]),
        creator=np.asarray([f"C{index}" for index in range(count)]),
        decision_day=np.asarray(["2026-06-28"] * count),
        timestamp_seconds=np.full(count, 180),
        peak_multiple=peaks,
        terminal_failure=peaks < 1,
        max_adverse_excursion=np.full(count, -0.2),
        graduated=np.zeros(count, dtype=bool),
        features=features,
        control_score=np.arange(count, dtype=float),
    )
    selected_scores = np.asarray([10.0 if index % 4 == 0 else 0.0 for index in range(count)])
    autopsy = _cohort_autopsy(data, np.ones(count, dtype=bool), selected_scores, np)
    assert autopsy["selected_1pct"] == 1
    assert autopsy["ranked_root_cause_differences"]
    effects = _effects(
        data,
        np.asarray([index < 20 for index in range(count)]),
        np.asarray([index >= 20 for index in range(count)]),
        np,
    )
    assert effects[0]["development_effect"] is not None
    assert all(row["status"] != "APPROVED" for row in effects)

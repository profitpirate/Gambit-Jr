from __future__ import annotations

import gzip
import hashlib
import json
import math
import statistics
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .store import HistoricalWarehouse, RawEvidence

TRENCHES_FILES = {
    "observations.parquet": "046fbaaad2151aabccc50683733767c0f728911e23b7b204b0d4fb7bc5389d37",
    "post_ids.csv": "cd9e8990fd2faa8057771cb5cb1346517cfc34bec34d7999f581bcc40c988944",
    "labels.parquet": "65f421fb589cbaf1bf626006f37486a26d2ac219f022f96bcb9690e30c68ecb9",
    "features.parquet": "6abcbaaf85c6da76bcce847bb72893a5aef92f71d043d60267db0618174d8142",
    "split.json": "9991b4162c9669e74c48444c34d77b2b5a0daa5545733791743413121c22d68a",
    "truth_set.parquet": "9475ed11c4cec81c28b9387cdb6f4bb9565ee39dabf16185f44f1b95b4b64132",
    "curve_paths.jsonl.gz": "6dfba77ddc3056d7f6d7e88fced4652b32108056ae0d1e0572ba0e4118ae5058",
    "launch_windows.jsonl.gz": "908ecaf196acad44e3df7e876da6eac0e9a1aa14e314a6b12c5e3b16ad08db74",
    "account_state.jsonl.gz": "598fdeb1b0b9acc9cc38c2be2bfc8955c8134d96be125cdccffc752491979844",
}

MELT_FILES = {
    "memecoin/memecoin.parquet": "7558a0a716d4ad1a7c4fb079ab8efae6ef7cc06580fabadc8dbbc206ef48bdd8",
    "label/labels.parquet": "9fcf8aff60d2102b70a612521303a16a54e9a017ab57d2ce9134ea0ee704d13f",
    "feat/feature.parquet": "7acdc3ed98cc70e874d1b76b1abeb8142bcc1ab435601dc800311148daed3a6b",
    "bundle/bundle-00000.parquet": (
        "58d40c494e54497efd29de3a54cba8112444235124fb44bbdf7a9abd53399697"
    ),
}

LAUNCH_CORPUS_FILES = {
    "tokens.parquet": "c005d86d424013e5c78701161b025f3d8c3d472afb61466e0ad6fd5afe9e8ea6",
    "migrations.parquet": "ef5d5141fd94acbcd121bed50e39e525cf7777d25338fc9852a67fd8a085105d",
    "postgard_outcomes.parquet": (
        "00a9980cb97630e9ea89574742f0e134d448e5566c5d7bda461f59bb02de022b"
    ),
    "postgard_snapshots.parquet": (
        "34a63b8333a41b3cc84d05461febe725cf37842fa0e21d6ea00472dcdcfa1e72"
    ),
    "wallet_stats.parquet": "4750101ddd335caa606e287be793df97b92daedee647b282d972fd3daf309a80",
    "snapshots.parquet": "41b6a221dca6ea01d68fac2129c3c5cd3966a27839f267e0f6aa4e980b6fc7d8",
    "trades/trades-00000.parquet": "646f56bba13514a7aa2cdbaf5ac37499175d4e0a6d5bee07f7f7f96c7e481154",
    "trades/trades-00001.parquet": "93da769636d25d5541301b98f1f7039282556de00dbcd6e3ea7ef736c2b8bde0",
    "trades/trades-00002.parquet": "2e2d91f9c73fb247e53cbc368fb9c52e93bd113febe10aef3a30a8add0dca3cb",
    "trades/trades-00003.parquet": "14450a575d5896ddf8b6f9ce8e9f601b87d0ab9042ff2cb46445c1bdf6db9070",
    "trades/trades-00004.parquet": "39e39b93691e124c801335e1ed454ed0c7bc1ace6e24456570af8d7a4c7fae47",
    "trades/trades-00005.parquet": "e171c5121d1ca56df35289abac57e1bdf8c53761b00abdac6be084517046c387",
    "trades/trades-00006.parquet": "77627cdcd584e0bf97e6e62e3e9bccd2825cccfef0b7316371eedd2fa67705a0",
    "trades/trades-00007.parquet": "c265053ef06620601ad6b2160177e0affba8423da70aeba13fecea2cc842d4c5",
    "trades/trades-00008.parquet": "414500a26f1cf781f89f7a8430e77bfacfb279a974685abb67f7ff769cbec847",
    "trades/trades-00009.parquet": "a74896ca0de21cfaf489542dbb7fd95d3a222d8e808da163be1f2b6a5b4f48ea",
    "trades/trades-00010.parquet": "28c66eec063058a59fbb8844cc599096f6e51ba7111471bda01b7b3288b450ae",
    "trades/trades-00011.parquet": "ec51848bd92dcf21617055728ce615aafd004b25b20d14c11e67857b9022e6a8",
    "trades/trades-00012.parquet": "4652bbebdff44cbe785d5eddd46b1164f4ee572f79ce00879f20e978e2f4afa0",
    "trades/trades-00013.parquet": "c84e1ddc6f3898024fda0b779ef87ab58d93f13d50c1e2424a38a33dfdf843d2",
    "trades/trades-00014.parquet": "7e177f81732948ff8adcf07c6c762710fb39c154644f18834ec52ec925b51dd9",
    "trades/trades-00015.parquet": "d3b144b5cfea6046bfb9be5b940500406f0a05524af630365bd9523149f2238d",
    "trades/trades-00016.parquet": "db90859dbd371141a6e8c2f673d478462ef79da2ace6a1bcfa6fa06e91c2a9a0",
    "trades/trades-00017.parquet": "3e48808f2ee97c7238af48446ef1ee2028b93fed58f11715dce6ef5a15fbe580",
}

LAUNCH_FEATURES = {
    "creator": ("creator_past_tokens", "creator_past_rugs"),
    "entry": (
        "initial_buy_sol",
        "initial_market_cap_sol",
        "launch_snipe_delta_sol",
        "first_trade_price_sol",
    ),
    "concentration": (
        "initial_holder_count",
        "initial_top1_pct_corrected",
        "initial_top5_pct_corrected",
        "initial_top10_pct_corrected",
        "initial_gini",
        "dev_buy_pct_corrected",
    ),
    "platform": ("is_mayhem_mode", "is_cashback_enabled"),
}

ALL_LAUNCH_FEATURES = tuple(feature for family in LAUNCH_FEATURES.values() for feature in family)

SPLITS = {
    "train": ("2026-06-05T00:00:00Z", "2026-06-20T00:00:00Z"),
    "validation": ("2026-06-24T00:00:00Z", "2026-07-02T00:00:00Z"),
    "test": ("2026-07-05T00:00:00Z", "2026-07-13T00:00:00Z"),
}

WALK_FORWARDS = {
    "early_to_late_regime_transfer": SPLITS,
    "late_regime_post_outage": {
        "train": ("2026-06-24T00:00:00Z", "2026-07-02T00:00:00Z"),
        "validation": ("2026-07-05T00:00:00Z", "2026-07-08T00:00:00Z"),
        "test": ("2026-07-10T00:00:00Z", "2026-07-12T00:00:00Z"),
    },
}


def _duckdb() -> Any:
    try:
        import duckdb
    except ImportError as error:  # pragma: no cover - depends on optional research extra
        raise RuntimeError(
            "Install the offline research extra: pip install -e '.[research]'"
        ) from error
    return duckdb


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _uuid(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "|".join(parts)))


def _iso_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(float(value), UTC).isoformat()


def verify_corpora(root: str | Path) -> dict[str, Any]:
    base = Path(root)
    sources = {
        "trenches": (base / "trenches-pumpfun-forward-2026-08", TRENCHES_FILES),
        "melt": (base / "MELT", MELT_FILES),
        "launch_corpus": (base / "Pumpfun_Memecoin_Corpus", LAUNCH_CORPUS_FILES),
    }
    result: dict[str, Any] = {}
    for source, (directory, expected) in sources.items():
        rows = []
        for relative, expected_hash in expected.items():
            path = directory / relative
            actual_hash = _sha256(path) if path.exists() else None
            rows.append(
                {
                    "path": relative,
                    "bytes": path.stat().st_size if path.exists() else None,
                    "sha256": actual_hash,
                    "expected_sha256": expected_hash,
                    "verified": actual_hash == expected_hash,
                }
            )
        result[source] = {
            "files": rows,
            "verified": all(row["verified"] for row in rows),
            "bytes": sum(int(row["bytes"] or 0) for row in rows),
        }
    if not all(source["verified"] for source in result.values()):
        raise ValueError("one or more corpus files failed checksum verification")
    return result


def _scalar(connection: Any, sql: str, parameters: list[Any]) -> Any:
    return connection.execute(sql, parameters).fetchone()[0]


def _rows(connection: Any, sql: str, parameters: list[Any]) -> list[dict[str, Any]]:
    cursor = connection.execute(sql, parameters)
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _metric_query(
    connection: Any,
    tokens: str,
    outcomes: str,
    *,
    start: str,
    end: str,
    score: str,
    target: str,
    fraction: float,
    graduated_only: bool = False,
) -> dict[str, Any]:
    population = "AND o.mint IS NOT NULL" if graduated_only else ""
    query = f"""
        WITH scored AS (
          SELECT t.mint, {target} AS target, {score} AS score
          FROM read_parquet(?) t
          LEFT JOIN read_parquet(?) o USING(mint)
          WHERE t.detected_at >= ?::TIMESTAMPTZ AND t.detected_at < ?::TIMESTAMPTZ
            AND NOT coalesce(t.top10_pct_suspect, false) {population}
        ), usable AS (
          SELECT * FROM scored WHERE score IS NOT NULL
        ), ranked AS (
          SELECT *, row_number() OVER(ORDER BY score DESC, mint) AS rank,
                 count(*) OVER() AS population
          FROM usable
        ), selected AS (
          SELECT * FROM ranked WHERE rank <= greatest(1, ceil(population * ?))
        )
        SELECT
          (SELECT count(*) FROM usable) AS sample_size,
          (SELECT sum(target::INTEGER) FROM usable) AS positives,
          count(*) AS selected,
          sum(target::INTEGER) AS selected_positives
        FROM selected
    """
    row = connection.execute(query, [tokens, outcomes, start, end, float(fraction)]).fetchone()
    sample, positives, selected, selected_positives = (int(value or 0) for value in row)
    negatives = sample - positives
    false_positives = selected - selected_positives
    return {
        "sample": sample,
        "positives": positives,
        "selected": selected,
        "selected_positives": selected_positives,
        "precision": selected_positives / selected if selected else None,
        "recall": selected_positives / positives if positives else None,
        "false_positive_rate": false_positives / negatives if negatives else None,
        "alert_rate": selected / sample if sample else None,
    }


def _training_spec(
    connection: Any,
    tokens: str,
    *,
    start: str,
    end: str,
    target: str,
    features: tuple[str, ...],
    outcomes: str,
    graduated_only: bool,
) -> dict[str, dict[str, float]]:
    population = "AND o.mint IS NOT NULL" if graduated_only else ""
    result = {}
    for feature in features:
        row = connection.execute(
            f"""
            SELECT avg(CAST({feature} AS DOUBLE)) FILTER(WHERE {target}),
                   avg(CAST({feature} AS DOUBLE)) FILTER(WHERE NOT ({target})),
                   avg(CAST({feature} AS DOUBLE)), stddev_pop(CAST({feature} AS DOUBLE))
            FROM read_parquet(?) t LEFT JOIN read_parquet(?) o USING(mint)
            WHERE t.detected_at >= ?::TIMESTAMPTZ AND t.detected_at < ?::TIMESTAMPTZ
              AND NOT coalesce(t.top10_pct_suspect, false) {population}
            """,
            [tokens, outcomes, start, end],
        ).fetchone()
        positive, negative, center, scale = row
        if None in {positive, negative, center, scale} or not float(scale):
            continue
        effect = (float(positive) - float(negative)) / float(scale)
        result[feature] = {
            "positive_mean": float(positive),
            "negative_mean": float(negative),
            "center": float(center),
            "scale": float(scale),
            "effect": effect,
            "weight": max(-3.0, min(3.0, effect)),
        }
    return result


def _score(spec: dict[str, dict[str, float]], excluded: tuple[str, ...] = ()) -> str:
    terms = []
    for feature, values in spec.items():
        if feature in excluded:
            continue
        center = values["center"]
        scale = values["scale"]
        weight = values["weight"]
        terms.append(
            f"coalesce(greatest(-5.0, least(5.0, "
            f"(CAST({feature} AS DOUBLE)-{center})/{scale}))"
            f"*{weight}, 0.0)"
        )
    return " + ".join(terms) if terms else "NULL"


def _directional_score(spec: dict[str, dict[str, float]], feature: str) -> str:
    values = spec.get(feature)
    if not values:
        return "NULL"
    direction = 1 if values["effect"] >= 0 else -1
    return f"({direction}) * CAST({feature} AS DOUBLE)"


def _model_research(
    connection: Any,
    tokens: str,
    outcomes: str,
    splits: dict[str, tuple[str, str]],
) -> dict[str, Any]:
    targets = {
        "graduation": {
            "target": "t.graduated_at IS NOT NULL",
            "fraction": 0.01,
            "graduated_only": False,
        },
        "five_x_runner": {
            "target": "o.price_at_grad_usd > 0 AND o.peak_price_usd/o.price_at_grad_usd >= 5",
            "fraction": 0.10,
            "graduated_only": True,
        },
        "graduated_failure": {
            "target": (
                "coalesce(o.rug_detected, false) OR "
                "o.outcome_label IN ('dead','pump_dump','slow_bleed')"
            ),
            "fraction": 0.10,
            "graduated_only": True,
        },
    }
    result: dict[str, Any] = {}
    train_start, train_end = splits["train"]
    for name, config in targets.items():
        spec = _training_spec(
            connection,
            tokens,
            start=train_start,
            end=train_end,
            target=config["target"],
            features=ALL_LAUNCH_FEATURES,
            outcomes=outcomes,
            graduated_only=bool(config["graduated_only"]),
        )
        full_score = _score(spec)
        baselines = {
            "random": "hash(t.mint)",
            "market_cap_only": _directional_score(spec, "initial_market_cap_sol"),
            "volume_only": _directional_score(spec, "initial_buy_sol"),
            "momentum_only": _directional_score(spec, "launch_snipe_delta_sol"),
            "liquidity_only": "NULL",
            "safety_filtered_momentum": (
                f"CASE WHEN coalesce(t.creator_past_rugs,0)=0 "
                f"AND coalesce(t.initial_top10_pct_corrected,100)<=50 "
                f"THEN {_directional_score(spec, 'launch_snipe_delta_sol')} END"
            ),
        }
        windows = {}
        for window, (start, end) in splits.items():
            windows[window] = {
                "full_model": _metric_query(
                    connection,
                    tokens,
                    outcomes,
                    start=start,
                    end=end,
                    score=full_score,
                    target=config["target"],
                    fraction=float(config["fraction"]),
                    graduated_only=bool(config["graduated_only"]),
                ),
                "baselines": {
                    baseline: _metric_query(
                        connection,
                        tokens,
                        outcomes,
                        start=start,
                        end=end,
                        score=score,
                        target=config["target"],
                        fraction=float(config["fraction"]),
                        graduated_only=bool(config["graduated_only"]),
                    )
                    for baseline, score in baselines.items()
                },
            }
        ablations = {}
        for family, features in LAUNCH_FEATURES.items():
            ablated = _metric_query(
                connection,
                tokens,
                outcomes,
                start=splits["test"][0],
                end=splits["test"][1],
                score=_score(spec, features),
                target=config["target"],
                fraction=float(config["fraction"]),
                graduated_only=bool(config["graduated_only"]),
            )
            full = windows["test"]["full_model"]
            ablated["precision_delta_vs_full"] = (
                None
                if full["precision"] is None or ablated["precision"] is None
                else full["precision"] - ablated["precision"]
            )
            ablations[family] = ablated
        result[name] = {
            "target": config["target"],
            "selection_fraction": config["fraction"],
            "training_effects": spec,
            "windows": windows,
            "ablations": ablations,
        }
    return result


def _drift(connection: Any, tokens: str) -> dict[str, Any]:
    result = {}
    for feature in ALL_LAUNCH_FEATURES:
        row = connection.execute(
            f"""
            SELECT
              median(CAST({feature} AS DOUBLE))
                FILTER(WHERE detected_at>=?::TIMESTAMPTZ AND detected_at<?::TIMESTAMPTZ),
              median(CAST({feature} AS DOUBLE))
                FILTER(WHERE detected_at>=?::TIMESTAMPTZ AND detected_at<?::TIMESTAMPTZ),
              stddev_pop(CAST({feature} AS DOUBLE))
                FILTER(WHERE detected_at>=?::TIMESTAMPTZ AND detected_at<?::TIMESTAMPTZ),
              count({feature}) FILTER(WHERE detected_at>=?::TIMESTAMPTZ AND detected_at<?::TIMESTAMPTZ)
            FROM read_parquet(?) WHERE NOT coalesce(top10_pct_suspect,false)
            """,
            [
                SPLITS["train"][0],
                SPLITS["train"][1],
                SPLITS["test"][0],
                SPLITS["test"][1],
                SPLITS["train"][0],
                SPLITS["train"][1],
                SPLITS["test"][0],
                SPLITS["test"][1],
                tokens,
            ],
        ).fetchone()
        train_median, test_median, scale, test_sample = row
        if None in {train_median, test_median, scale}:
            continue
        shift = (float(test_median) - float(train_median)) / (float(scale) or 1.0)
        result[feature] = {
            "train_median": float(train_median),
            "test_median": float(test_median),
            "standardized_shift": shift,
            "test_sample": int(test_sample),
            "state": "WARNING" if abs(shift) >= 1 else "STABLE",
        }
    return result


def _trade_memory(trenches: Path) -> dict[str, Any]:
    wallet_mints: dict[str, set[str]] = defaultdict(set)
    events = 0
    buys = 0
    sells = 0
    mints: set[str] = set()
    times = []
    source_results = {}
    for file_name in ("curve_paths.jsonl.gz", "launch_windows.jsonl.gz"):
        source_events = 0
        source_mints = set()
        source_wallets = set()
        completed = 0
        multiples = []
        with gzip.open(trenches / file_name, "rt", encoding="utf-8") as source:
            for line in source:
                row = json.loads(line)
                mint = str(row["mint"])
                mints.add(mint)
                source_mints.add(mint)
                completed += int(bool(row.get("completed")))
                prices = []
                for event in row.get("events") or []:
                    source_events += 1
                    events += 1
                    is_buy = bool(event.get("is_buy"))
                    buys += int(is_buy)
                    sells += int(not is_buy)
                    if event.get("block_time"):
                        times.append(int(event["block_time"]))
                    wallet = event.get("user")
                    if wallet:
                        wallet = str(wallet)
                        source_wallets.add(wallet)
                        wallet_mints[wallet].add(mint)
                    v_sol = event.get("v_sol_post")
                    v_token = event.get("v_tok_post")
                    if v_sol and v_token:
                        prices.append(float(v_sol) / float(v_token))
                if prices and prices[0] > 0:
                    multiples.append(max(prices) / prices[0])
        source_results[file_name] = {
            "mints": len(source_mints),
            "events": source_events,
            "wallets": len(source_wallets),
            "completed": completed,
            "peak_multiple_sample": len(multiples),
            "median_peak_multiple": statistics.median(multiples) if multiples else None,
            "runner_cohorts": {
                f"{threshold:g}x": sum(value >= threshold for value in multiples)
                for threshold in (1.5, 2, 3, 5, 10, 20, 50)
            },
        }
    launch_counts = [len(values) for values in wallet_mints.values()]
    return {
        "mints": len(mints),
        "events": events,
        "wallets": len(wallet_mints),
        "wallet_mint_pairs": sum(launch_counts),
        "repeat_wallets": sum(value > 1 for value in launch_counts),
        "wallets_with_5_plus_launches": sum(value >= 5 for value in launch_counts),
        "maximum_launches_per_wallet": max(launch_counts, default=0),
        "buys": buys,
        "sells": sells,
        "earliest_event": _iso_from_epoch(min(times)) if times else None,
        "latest_event": _iso_from_epoch(max(times)) if times else None,
        "sources": source_results,
    }


def _summaries(connection: Any, root: Path) -> dict[str, Any]:
    trenches = root / "trenches-pumpfun-forward-2026-08"
    melt = root / "MELT"
    corpus = root / "Pumpfun_Memecoin_Corpus"
    trenches_features = str(trenches / "features.parquet")
    trenches_labels = str(trenches / "labels.parquet")
    trenches_observations = str(trenches / "observations.parquet")
    truth = str(trenches / "truth_set.parquet")
    melt_tokens = str(melt / "memecoin" / "memecoin.parquet")
    melt_labels = str(melt / "label" / "labels.parquet")
    melt_bundle = str(melt / "bundle" / "bundle-00000.parquet")
    tokens = str(corpus / "tokens.parquet")
    outcomes = str(corpus / "postgard_outcomes.parquet")
    snapshots = str(corpus / "postgard_snapshots.parquet")
    wallets = str(corpus / "wallet_stats.parquet")
    migrations = str(corpus / "migrations.parquet")

    launch_summary = _rows(
        connection,
        """
        SELECT count(*) launches, min(detected_at)::VARCHAR earliest,
               max(detected_at)::VARCHAR latest,
               sum((graduated_at IS NOT NULL)::INTEGER) graduated,
               count(DISTINCT creator) creators,
               sum(is_zombie::INTEGER) zombies,
               sum(is_mayhem_mode::INTEGER) mayhem_mode,
               sum(top10_pct_suspect::INTEGER) concentration_excluded
        FROM read_parquet(?)
        """,
        [tokens],
    )[0]
    outcome_summary = _rows(
        connection,
        """
        SELECT outcome_label, count(*) AS "sample", sum(rug_detected::INTEGER) rugs,
               sum(still_liquid_at_24h::INTEGER) liquid_24h,
               sum(still_liquid_at_48h::INTEGER) liquid_48h,
               sum(success_label::INTEGER) successes
        FROM read_parquet(?) GROUP BY outcome_label ORDER BY "sample" DESC
        """,
        [outcomes],
    )
    cohort = _rows(
        connection,
        """
        SELECT count(*) FILTER(WHERE price_at_grad_usd>0) AS "sample",
          sum((peak_price_usd/price_at_grad_usd>=1.5)::INTEGER)
            FILTER(WHERE price_at_grad_usd>0) ge_1_5x,
          sum((peak_price_usd/price_at_grad_usd>=2)::INTEGER)
            FILTER(WHERE price_at_grad_usd>0) ge_2x,
          sum((peak_price_usd/price_at_grad_usd>=3)::INTEGER)
            FILTER(WHERE price_at_grad_usd>0) ge_3x,
          sum((peak_price_usd/price_at_grad_usd>=5)::INTEGER)
            FILTER(WHERE price_at_grad_usd>0) ge_5x,
          sum((peak_price_usd/price_at_grad_usd>=10)::INTEGER)
            FILTER(WHERE price_at_grad_usd>0) ge_10x,
          sum((peak_price_usd/price_at_grad_usd>=20)::INTEGER)
            FILTER(WHERE price_at_grad_usd>0) ge_20x,
          sum((peak_price_usd/price_at_grad_usd>=50)::INTEGER)
            FILTER(WHERE price_at_grad_usd>0) ge_50x,
          median(peak_price_usd/price_at_grad_usd)
            FILTER(WHERE price_at_grad_usd>0) median_peak_multiple
        FROM read_parquet(?)
        """,
        [outcomes],
    )[0]
    adjusted = _rows(
        connection,
        """
        SELECT count(*) AS "sample",
          sum((peak_price_usd/price_at_1m_usd>=2)::INTEGER) ge_2x,
          sum((peak_price_usd/price_at_1m_usd>=5)::INTEGER) ge_5x,
          sum((peak_price_usd/price_at_1m_usd>=10)::INTEGER) ge_10x,
          median(peak_price_usd/price_at_1m_usd) median_peak_multiple
        FROM read_parquet(?) o JOIN read_parquet(?) t USING(mint)
        WHERE price_at_1m_usd>0 AND peak_price_at>=graduated_at+INTERVAL 1 MINUTE
        """,
        [outcomes, tokens],
    )[0]
    creator_summary = _rows(
        connection,
        """
        WITH creators AS (
          SELECT creator, count(*) launches,
                 sum((graduated_at IS NOT NULL)::INTEGER) graduated
          FROM read_parquet(?) GROUP BY creator
        )
        SELECT count(*) creators, sum((launches>1)::INTEGER) repeat_creators,
               sum((launches>=5)::INTEGER) creators_with_5_plus,
               max(launches) maximum_launches, sum((graduated>0)::INTEGER) with_graduation
        FROM creators
        """,
        [tokens],
    )[0]
    funders = _rows(
        connection,
        """
        WITH firsts AS (
          SELECT mint, gate_inputs__fund_from_address AS funder,
                 row_number() OVER(PARTITION BY mint ORDER BY seen_at_ms) AS sequence
          FROM read_parquet(?)
        )
        SELECT count(*) FILTER(WHERE sequence=1 AND funder IS NOT NULL) relationships,
               count(DISTINCT funder) FILTER(WHERE sequence=1 AND funder IS NOT NULL) funders
        FROM firsts
        """,
        [trenches_observations],
    )[0]
    return {
        "launch_universe": launch_summary,
        "mature_outcomes": {
            "rows": _scalar(connection, "SELECT count(*) FROM read_parquet(?)", [outcomes]),
            "classes": outcome_summary,
        },
        "runner_cohorts_raw": cohort,
        "runner_cohorts_one_minute_entry": adjusted,
        "creators": creator_summary,
        "wallets": {
            "published_rows": _scalar(
                connection, "SELECT count(*) FROM read_parquet(?)", [wallets]
            ),
            "activity_totals_usable": False,
            "reason": "published audit found a stale wallet aggregation snapshot",
        },
        "funding_graph": funders,
        "liquidity_history": _rows(
            connection,
            """
            SELECT count(*) observations, count(DISTINCT mint) mints,
                   sum((liquidity_usd IS NOT NULL)::INTEGER) known_liquidity,
                   sum(incomplete_data::INTEGER) incomplete_rows,
                   min(snapshot_time)::VARCHAR earliest, max(snapshot_time)::VARCHAR latest
            FROM read_parquet(?)
            """,
            [snapshots],
        )[0],
        "migrations": _rows(
            connection,
            """
            SELECT count(*) events, count(DISTINCT mint) mints,
              sum((pool_address NOT IN ('synthetic_graduation_queue',
                   'backfilled_from_pumpswap_trade'))::INTEGER) real_pool_addresses
            FROM read_parquet(?)
            """,
            [migrations],
        )[0],
        "trenches": {
            "observations": _scalar(
                connection,
                "SELECT count(*) FROM read_parquet(?)",
                [trenches_observations],
            ),
            "launches": _scalar(
                connection, "SELECT count(*) FROM read_parquet(?)", [trenches_features]
            ),
            "labels": _scalar(
                connection, "SELECT count(*) FROM read_parquet(?)", [trenches_labels]
            ),
            "completed": _scalar(
                connection,
                "SELECT sum(path_completed::INTEGER) FROM read_parquet(?)",
                [trenches_labels],
            ),
            "truth_set": _rows(
                connection,
                """
                SELECT count(*) AS "sample", sum(true_label::INTEGER) positives,
                       sum(drained_lb::INTEGER) drained_lower_bound,
                       count(DISTINCT creator) creators
                FROM read_parquet(?)
                """,
                [truth],
            )[0],
            "trade_memory": _trade_memory(trenches),
        },
        "melt": {
            "launches": _scalar(connection, "SELECT count(*) FROM read_parquet(?)", [melt_tokens]),
            "risk_labels": _rows(
                connection,
                """
                SELECT label, manipulated, count(*) AS "sample"
                FROM read_parquet(?) GROUP BY label, manipulated ORDER BY label, manipulated
                """,
                [melt_labels],
            ),
            "creator_quality": "UNUSABLE_CONSTANT_CREATOR_COLUMN",
            "coordination_graph": _rows(
                connection,
                """
                SELECT source, entity_type, count(*) relationships,
                       count(DISTINCT entity) entities,
                       count(DISTINCT identifier) identifiers
                FROM read_parquet(?) GROUP BY source, entity_type ORDER BY source
                """,
                [melt_bundle],
            ),
        },
    }


def _register_file_evidence(
    warehouse: HistoricalWarehouse,
    root: Path,
    verified: dict[str, Any],
) -> dict[str, str]:
    definitions = {
        "trenches": {
            "directory": root / "trenches-pumpfun-forward-2026-08",
            "dataset_id": "trenches-pumpfun-forward-2026-08",
            "version": "hf-2026-08-16",
            "provider": "huggingface_trenches_public_release",
            "earliest": "2026-05-08T22:33:37+00:00",
            "point_in_time_safe": True,
            "missing": ["69.09-hour capture outage", "vendor-curated launch enrichment"],
        },
        "melt": {
            "directory": root / "MELT",
            "dataset_id": "melt-pumpfun-2025",
            "version": "hf-public-v1",
            "provider": "huggingface_melt_public_release",
            "earliest": "2024-12-01T00:00:00+00:00",
            "point_in_time_safe": False,
            "missing": [
                "feature availability is migration-time, not launch-time",
                "creator column is constant and unusable",
            ],
        },
        "launch_corpus": {
            "directory": root / "Pumpfun_Memecoin_Corpus",
            "dataset_id": "pumpfun-launch-corpus-2026-06-07",
            "version": "hf-jun-jul-2026-corrected",
            "provider": "huggingface_slink_launch_corpus",
            "earliest": "2026-06-05T09:12:26+00:00",
            "point_in_time_safe": True,
            "missing": ["July 3 websocket outage", "raw trade files not acquired in this run"],
        },
    }
    evidence_ids = {}
    acquired = datetime.now(UTC).isoformat()
    for source, definition in definitions.items():
        warehouse.register_dataset(
            {
                "dataset_id": definition["dataset_id"],
                "dataset_version": definition["version"],
                "provider": definition["provider"],
                "chain": "solana",
                "acquisition_method": "checksum_pinned_public_research_release",
                "refresh_method": "immutable_release_revision",
                "timestamp_precision": "source-defined; per-field when published",
                "reliability": "PUBLIC_CHECKSUM_VERIFIED_WITH_DOCUMENTED_LIMITATIONS",
                "history_kind": "TRUE_HISTORICAL",
                "point_in_time_safe": definition["point_in_time_safe"],
                "estimated_completeness": None,
                "missing_ranges_json": definition["missing"],
                "cost_json": {"class": "FREE_PUBLIC_DATASET", "monthly_usd": 0},
            }
        )
        for file_record in verified[source]["files"]:
            relative = file_record["path"]
            evidence_id, _ = warehouse.ingest_raw(
                RawEvidence(
                    dataset_id=definition["dataset_id"],
                    provider=definition["provider"],
                    chain="solana",
                    entity_type="dataset_file",
                    entity_id=relative,
                    source_timestamp=definition["earliest"],
                    availability_timestamp=acquired,
                    endpoint_type="public_dataset_file",
                    payload={
                        "path": relative,
                        "bytes": file_record["bytes"],
                        "sha256": file_record["sha256"],
                    },
                    schema_version=definition["version"],
                    acquisition_version="v1.5-real-evidence-v1",
                    provenance={
                        "local_source": str(definition["directory"] / relative),
                        "checksum_verified": True,
                        "raw_file_not_committed": True,
                    },
                ),
                refresh_coverage=False,
            )
            evidence_ids[f"{source}:{relative}"] = evidence_id
        warehouse.refresh_dataset_coverage(definition["dataset_id"])
    return evidence_ids


def _execute_batch(connection: Any, sql: str, rows: list[tuple[Any, ...]]) -> None:
    if rows:
        connection.executemany(sql, rows)


def _materialize_launch_corpus(
    warehouse: HistoricalWarehouse,
    duck_connection: Any,
    root: Path,
    evidence_id: str,
) -> dict[str, int]:
    dataset_version = "pumpfun-launch-corpus-jun-jul-2026-corrected"
    feature_version = "launch-context-v1"
    outcome_version = "postgard-outcome-v1"
    existing = warehouse.conn.execute(
        "SELECT COUNT(*) FROM normalized_events WHERE dataset_version=? "
        "AND event_type='launch_detected'",
        (dataset_version,),
    ).fetchone()[0]
    if existing >= 798_430:
        return {
            "launch_events": existing,
            "launch_features": warehouse.conn.execute(
                "SELECT COUNT(*) FROM point_in_time_features WHERE dataset_version=? "
                "AND feature_version=?",
                (dataset_version, feature_version),
            ).fetchone()[0],
            "outcomes": warehouse.conn.execute(
                "SELECT COUNT(*) FROM outcomes WHERE dataset_version=? AND outcome_version=?",
                (dataset_version, outcome_version),
            ).fetchone()[0],
        }
    corpus = root / "Pumpfun_Memecoin_Corpus"
    tokens = str(corpus / "tokens.parquet")
    outcomes = str(corpus / "postgard_outcomes.parquet")
    cursor = duck_connection.execute(
        """
        SELECT t.mint, epoch(t.detected_at), epoch(t.tracking_expires_at), t.creator,
          t.creator_past_tokens, t.creator_past_rugs, t.initial_buy_sol,
          t.initial_market_cap_sol, t.launch_snipe_delta_sol, t.first_trade_price_sol,
          t.initial_holder_count, t.initial_top1_pct_corrected,
          t.initial_top5_pct_corrected, t.initial_top10_pct_corrected, t.initial_gini,
          t.dev_buy_pct_corrected, t.is_mayhem_mode, t.is_cashback_enabled,
          t.is_zombie, epoch(t.graduated_at), epoch(o.computed_at),
          o.price_at_grad_usd, o.peak_price_usd, o.outcome_label, o.rug_detected,
          epoch(o.peak_price_at), o.peak_mcap_usd, o.peak_liquidity_usd,
          o.still_liquid_at_24h, o.still_liquid_at_48h
        FROM read_parquet(?) t LEFT JOIN read_parquet(?) o USING(mint)
        ORDER BY t.detected_at, t.mint
        """,
        [tokens, outcomes],
    )
    entity_sql = "INSERT OR IGNORE INTO canonical_entities VALUES(?,?,?,?,?,?,?)"
    event_sql = "INSERT OR IGNORE INTO normalized_events VALUES(?,?,?,?,?,?,?,?,?)"
    feature_sql = "INSERT OR IGNORE INTO point_in_time_features VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
    outcome_sql = "INSERT OR IGNORE INTO outcomes VALUES(" + ",".join("?" for _ in range(25)) + ")"
    totals = defaultdict(int)
    while rows := cursor.fetchmany(10_000):
        entities: list[tuple[Any, ...]] = []
        events: list[tuple[Any, ...]] = []
        features: list[tuple[Any, ...]] = []
        outcome_rows: list[tuple[Any, ...]] = []
        computed_at = datetime.now(UTC).isoformat()
        for row in rows:
            (
                mint,
                detected_epoch,
                expiry_epoch,
                creator,
                creator_past_tokens,
                creator_past_rugs,
                initial_buy_sol,
                initial_market_cap_sol,
                launch_snipe_delta_sol,
                first_trade_price_sol,
                initial_holder_count,
                initial_top1,
                initial_top5,
                initial_top10,
                initial_gini,
                dev_buy_pct,
                mayhem,
                cashback,
                zombie,
                graduated_epoch,
                outcome_computed_epoch,
                price_at_grad,
                peak_price,
                outcome_label,
                rugged,
                peak_epoch,
                peak_mcap,
                peak_liquidity,
                liquid_24h,
                liquid_48h,
            ) = row
            detected = _iso_from_epoch(float(detected_epoch))
            expiry_value = expiry_epoch or outcome_computed_epoch or detected_epoch
            measurement_end = _iso_from_epoch(float(expiry_value))
            available = _iso_from_epoch(float(outcome_computed_epoch or expiry_value))
            token_key = _uuid("token", "solana", str(mint))
            creator_key = _uuid("creator", "solana", str(creator))
            launch_values = {
                "creator": creator,
                "creator_past_tokens": creator_past_tokens,
                "creator_past_rugs": creator_past_rugs,
                "initial_buy_sol": initial_buy_sol,
                "initial_market_cap_sol": initial_market_cap_sol,
                "launch_snipe_delta_sol": launch_snipe_delta_sol,
                "first_trade_price_sol": first_trade_price_sol,
                "initial_holder_count": initial_holder_count,
                "initial_top1_pct_corrected": initial_top1,
                "initial_top5_pct_corrected": initial_top5,
                "initial_top10_pct_corrected": initial_top10,
                "initial_gini": initial_gini,
                "dev_buy_pct_corrected": dev_buy_pct,
                "is_mayhem_mode": mayhem,
                "is_cashback_enabled": cashback,
            }
            entities.extend(
                [
                    (
                        token_key,
                        "token",
                        "solana",
                        mint,
                        detected,
                        json.dumps({}, separators=(",", ":")),
                        json.dumps({"dataset": dataset_version}, separators=(",", ":")),
                    ),
                    (
                        creator_key,
                        "creator",
                        "solana",
                        creator,
                        detected,
                        json.dumps({}, separators=(",", ":")),
                        json.dumps({"dataset": dataset_version}, separators=(",", ":")),
                    ),
                ]
            )
            launch_event_id = _uuid(evidence_id, "launch_detected", detected, str(mint))
            events.extend(
                [
                    (
                        launch_event_id,
                        evidence_id,
                        dataset_version,
                        token_key,
                        "launch_detected",
                        detected,
                        detected,
                        json.dumps(launch_values, separators=(",", ":"), default=str),
                        "KNOWN_WITH_SOURCE_LIMITATIONS",
                    ),
                    (
                        _uuid(evidence_id, "creator_launched", detected, str(mint)),
                        evidence_id,
                        dataset_version,
                        creator_key,
                        "creator_launched",
                        detected,
                        detected,
                        json.dumps({"mint": mint}, separators=(",", ":")),
                        "KNOWN",
                    ),
                ]
            )
            features.extend(
                [
                    (
                        _uuid(
                            dataset_version,
                            feature_version,
                            token_key,
                            "launch_context",
                            detected,
                            detected,
                        ),
                        dataset_version,
                        feature_version,
                        token_key,
                        "launch_context",
                        json.dumps(launch_values, separators=(",", ":"), default=str),
                        detected,
                        detected,
                        computed_at,
                        json.dumps([launch_event_id], separators=(",", ":")),
                        "KNOWN_WITHOUT_PER_FIELD_STAMPS",
                        0.7,
                    ),
                    (
                        _uuid(
                            dataset_version,
                            feature_version,
                            creator_key,
                            "creator_memory",
                            detected,
                            detected,
                        ),
                        dataset_version,
                        feature_version,
                        creator_key,
                        "creator_memory",
                        json.dumps(
                            {
                                "prior_launches": creator_past_tokens,
                                "prior_rugs": creator_past_rugs,
                            },
                            separators=(",", ":"),
                            default=str,
                        ),
                        detected,
                        detected,
                        computed_at,
                        json.dumps([], separators=(",", ":")),
                        "SOURCE_REPORTED_POINT_IN_TIME",
                        0.6,
                    ),
                ]
            )
            peak_multiple = (
                float(peak_price) / float(price_at_grad)
                if price_at_grad and peak_price and float(price_at_grad) > 0
                else None
            )
            if outcome_label:
                class_name = str(outcome_label).upper()
            elif graduated_epoch:
                class_name = "GRADUATED_OUTCOME_MISSING"
            elif zombie:
                class_name = "ZOMBIE"
            else:
                class_name = "NOT_GRADUATED_IN_CAPTURE_HORIZON"
            outcome_rows.append(
                (
                    _uuid(dataset_version, outcome_version, token_key, detected),
                    dataset_version,
                    outcome_version,
                    token_key,
                    detected,
                    measurement_end,
                    available,
                    peak_multiple,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    (float(peak_epoch) - float(graduated_epoch))
                    if peak_epoch and graduated_epoch
                    else None,
                    None,
                    peak_multiple,
                    None,
                    None,
                    86_400 if liquid_24h else None,
                    172_800 if liquid_48h else None,
                    peak_mcap,
                    peak_liquidity,
                    int(bool(rugged)),
                    class_name,
                )
            )
        with warehouse._lock, warehouse.conn:
            _execute_batch(warehouse.conn, entity_sql, entities)
            _execute_batch(warehouse.conn, event_sql, events)
            _execute_batch(warehouse.conn, feature_sql, features)
            _execute_batch(warehouse.conn, outcome_sql, outcome_rows)
        totals["launch_events"] += len(rows)
        totals["launch_features"] += len(features)
        totals["outcomes"] += len(outcome_rows)
    return {
        "launch_events": existing + totals["launch_events"],
        "launch_features": warehouse.conn.execute(
            "SELECT COUNT(*) FROM point_in_time_features WHERE dataset_version=? "
            "AND feature_version=?",
            (dataset_version, feature_version),
        ).fetchone()[0],
        "outcomes": warehouse.conn.execute(
            "SELECT COUNT(*) FROM outcomes WHERE dataset_version=? AND outcome_version=?",
            (dataset_version, outcome_version),
        ).fetchone()[0],
    }


def _materialize_wallet_identities(
    warehouse: HistoricalWarehouse,
    duck_connection: Any,
    root: Path,
) -> int:
    existing = warehouse.conn.execute(
        "SELECT COUNT(*) FROM canonical_entities WHERE entity_type='wallet' AND "
        "provenance_json LIKE '%wallet_stats_identity_only%'"
    ).fetchone()[0]
    if existing >= 1_016_374:
        return existing
    wallets = str(root / "Pumpfun_Memecoin_Corpus" / "wallet_stats.parquet")
    cursor = duck_connection.execute(
        "SELECT wallet, epoch(first_seen_at), epoch(last_seen_at) FROM read_parquet(?)",
        [wallets],
    )
    total = 0
    while rows := cursor.fetchmany(20_000):
        values = []
        for wallet, first_seen, last_seen in rows:
            first = _iso_from_epoch(float(first_seen or last_seen))
            values.append(
                (
                    _uuid("wallet", "solana", str(wallet)),
                    "wallet",
                    "solana",
                    wallet,
                    first,
                    json.dumps(
                        {
                            "last_seen_at": _iso_from_epoch(float(last_seen or first_seen)),
                            "activity_totals_state": "REJECTED_STALE_SNAPSHOT",
                        },
                        separators=(",", ":"),
                    ),
                    json.dumps({"dataset": "wallet_stats_identity_only"}, separators=(",", ":")),
                )
            )
        with warehouse._lock, warehouse.conn:
            _execute_batch(
                warehouse.conn,
                "INSERT OR IGNORE INTO canonical_entities VALUES(?,?,?,?,?,?,?)",
                values,
            )
        total += len(values)
    return total


def _materialize_funding_memory(
    warehouse: HistoricalWarehouse,
    duck_connection: Any,
    root: Path,
    evidence_id: str,
) -> dict[str, int]:
    dataset_version = "trenches-pumpfun-forward-2026-08"
    existing = warehouse.conn.execute(
        "SELECT COUNT(*) FROM normalized_events WHERE dataset_version=? "
        "AND event_type='funding_relationship_observed'",
        (dataset_version,),
    ).fetchone()[0]
    if existing >= 67_722:
        return {
            "relationships": existing,
            "features": warehouse.conn.execute(
                "SELECT COUNT(*) FROM point_in_time_features WHERE dataset_version=? "
                "AND feature_version='funding-relationship-v1'",
                (dataset_version,),
            ).fetchone()[0],
        }
    observations = str(root / "trenches-pumpfun-forward-2026-08" / "observations.parquet")
    rows = duck_connection.execute(
        """
        WITH firsts AS (
          SELECT mint, gate_inputs__fund_from_address AS funder, seen_at_ms,
                 tau__gate_inputs,
                 row_number() OVER(PARTITION BY mint ORDER BY seen_at_ms) AS sequence
          FROM read_parquet(?)
        )
        SELECT mint, funder, seen_at_ms, greatest(seen_at_ms, tau__gate_inputs)
        FROM firsts WHERE sequence=1 AND funder IS NOT NULL
        """,
        [observations],
    ).fetchall()
    entities = []
    events = []
    features = []
    computed_at = datetime.now(UTC).isoformat()
    for mint, funder, seen_ms, available_ms in rows:
        observed = _iso_from_epoch(float(seen_ms) / 1000)
        available = _iso_from_epoch(float(available_ms) / 1000)
        token_key = _uuid("token", "solana", str(mint))
        funder_key = _uuid("funder", "solana", str(funder))
        entities.extend(
            [
                (
                    token_key,
                    "token",
                    "solana",
                    mint,
                    observed,
                    "{}",
                    json.dumps({"dataset": dataset_version}, separators=(",", ":")),
                ),
                (
                    funder_key,
                    "funder",
                    "solana",
                    funder,
                    available,
                    "{}",
                    json.dumps({"dataset": dataset_version}, separators=(",", ":")),
                ),
            ]
        )
        event_id = _uuid(evidence_id, "funding_relationship_observed", observed, str(mint))
        events.append(
            (
                event_id,
                evidence_id,
                dataset_version,
                token_key,
                "funding_relationship_observed",
                observed,
                available,
                json.dumps({"funder": funder}, separators=(",", ":")),
                "SOURCE_REPORTED",
            )
        )
        features.append(
            (
                _uuid(
                    dataset_version,
                    "funding-relationship-v1",
                    token_key,
                    "funder_known",
                    observed,
                    available,
                ),
                dataset_version,
                "funding-relationship-v1",
                token_key,
                "funder_known",
                "true",
                observed,
                available,
                computed_at,
                json.dumps([event_id], separators=(",", ":")),
                "KNOWN",
                1.0,
            )
        )
    with warehouse._lock, warehouse.conn:
        _execute_batch(
            warehouse.conn,
            "INSERT OR IGNORE INTO canonical_entities VALUES(?,?,?,?,?,?,?)",
            entities,
        )
        _execute_batch(
            warehouse.conn,
            "INSERT OR IGNORE INTO normalized_events VALUES(?,?,?,?,?,?,?,?,?)",
            events,
        )
        _execute_batch(
            warehouse.conn,
            "INSERT OR IGNORE INTO point_in_time_features VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            features,
        )
    return {
        "relationships": warehouse.conn.execute(
            "SELECT COUNT(*) FROM normalized_events WHERE dataset_version=? "
            "AND event_type='funding_relationship_observed'",
            (dataset_version,),
        ).fetchone()[0],
        "features": warehouse.conn.execute(
            "SELECT COUNT(*) FROM point_in_time_features WHERE dataset_version=? "
            "AND feature_version='funding-relationship-v1'",
            (dataset_version,),
        ).fetchone()[0],
    }


def _materialize_buyer_memory(
    warehouse: HistoricalWarehouse,
    root: Path,
    evidence_ids: dict[str, str],
) -> dict[str, int]:
    dataset_version = "trenches-chain-trades-2026-05-06"
    existing = warehouse.conn.execute(
        "SELECT COUNT(*) FROM normalized_events WHERE dataset_version=? "
        "AND event_type='wallet_first_entry'",
        (dataset_version,),
    ).fetchone()[0]
    if existing >= 137_398:
        return {
            "wallet_mint_entries": existing,
            "wallet_memory_features": warehouse.conn.execute(
                "SELECT COUNT(*) FROM point_in_time_features WHERE dataset_version=? "
                "AND feature_version='wallet-memory-v1'",
                (dataset_version,),
            ).fetchone()[0],
            "wallets": warehouse.conn.execute(
                "SELECT COUNT(DISTINCT entity_key) FROM normalized_events "
                "WHERE dataset_version=? AND event_type='wallet_first_entry'",
                (dataset_version,),
            ).fetchone()[0],
        }
    directory = root / "trenches-pumpfun-forward-2026-08"
    first_entries: dict[tuple[str, str], tuple[int, str, bool]] = {}
    for file_name in ("curve_paths.jsonl.gz", "launch_windows.jsonl.gz"):
        with gzip.open(directory / file_name, "rt", encoding="utf-8") as source:
            for line in source:
                row = json.loads(line)
                mint = str(row["mint"])
                for event in row.get("events") or []:
                    wallet = event.get("user")
                    block_time = event.get("block_time")
                    if not wallet or not block_time:
                        continue
                    key = (str(wallet), mint)
                    candidate = (int(block_time), file_name, bool(event.get("is_buy")))
                    if key not in first_entries or candidate[0] < first_entries[key][0]:
                        first_entries[key] = candidate
    chronological = sorted(
        (
            (timestamp, wallet, mint, file_name, is_buy)
            for (wallet, mint), (timestamp, file_name, is_buy) in first_entries.items()
        ),
        key=lambda item: (item[0], item[1], item[2]),
    )
    prior_launches: dict[str, int] = defaultdict(int)
    entities = []
    events = []
    features = []
    computed_at = datetime.now(UTC).isoformat()
    for timestamp, wallet, mint, file_name, is_buy in chronological:
        observed = _iso_from_epoch(float(timestamp))
        wallet_key = _uuid("wallet", "solana", wallet)
        token_key = _uuid("token", "solana", mint)
        evidence_id = evidence_ids[f"trenches:{file_name}"]
        entities.extend(
            [
                (
                    wallet_key,
                    "wallet",
                    "solana",
                    wallet,
                    observed,
                    "{}",
                    json.dumps({"dataset": dataset_version}, separators=(",", ":")),
                ),
                (
                    token_key,
                    "token",
                    "solana",
                    mint,
                    observed,
                    "{}",
                    json.dumps({"dataset": dataset_version}, separators=(",", ":")),
                ),
            ]
        )
        event_id = _uuid(evidence_id, "wallet_first_entry", observed, wallet, mint)
        events.append(
            (
                event_id,
                evidence_id,
                dataset_version,
                wallet_key,
                "wallet_first_entry",
                observed,
                observed,
                json.dumps({"mint": mint, "is_buy": is_buy}, separators=(",", ":")),
                "PUBLIC_CHAIN_DECODED",
            )
        )
        features.append(
            (
                _uuid(
                    dataset_version,
                    "wallet-memory-v1",
                    wallet_key,
                    "prior_launches_entered",
                    observed,
                    observed,
                ),
                dataset_version,
                "wallet-memory-v1",
                wallet_key,
                "prior_launches_entered",
                json.dumps(prior_launches[wallet]),
                observed,
                observed,
                computed_at,
                json.dumps([event_id], separators=(",", ":")),
                "KNOWN",
                min(1.0, prior_launches[wallet] / 20),
            )
        )
        prior_launches[wallet] += 1
    with warehouse._lock, warehouse.conn:
        _execute_batch(
            warehouse.conn,
            "INSERT OR IGNORE INTO canonical_entities VALUES(?,?,?,?,?,?,?)",
            entities,
        )
        _execute_batch(
            warehouse.conn,
            "INSERT OR IGNORE INTO normalized_events VALUES(?,?,?,?,?,?,?,?,?)",
            events,
        )
        _execute_batch(
            warehouse.conn,
            "INSERT OR IGNORE INTO point_in_time_features VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            features,
        )
    return {
        "wallet_mint_entries": warehouse.conn.execute(
            "SELECT COUNT(*) FROM normalized_events WHERE dataset_version=? "
            "AND event_type='wallet_first_entry'",
            (dataset_version,),
        ).fetchone()[0],
        "wallet_memory_features": warehouse.conn.execute(
            "SELECT COUNT(*) FROM point_in_time_features WHERE dataset_version=? "
            "AND feature_version='wallet-memory-v1'",
            (dataset_version,),
        ).fetchone()[0],
        "wallets": len(prior_launches),
    }


def materialize_public_corpora(
    warehouse: HistoricalWarehouse,
    duck_connection: Any,
    root: Path,
    evidence_ids: dict[str, str],
) -> dict[str, Any]:
    return {
        "launch_corpus": _materialize_launch_corpus(
            warehouse,
            duck_connection,
            root,
            evidence_ids["launch_corpus:tokens.parquet"],
        ),
        "wallet_identities": _materialize_wallet_identities(warehouse, duck_connection, root),
        "funding_memory": _materialize_funding_memory(
            warehouse,
            duck_connection,
            root,
            evidence_ids["trenches:observations.parquet"],
        ),
        "buyer_memory": _materialize_buyer_memory(warehouse, root, evidence_ids),
    }


def run_public_evidence_research(
    root: str | Path,
    *,
    warehouse: HistoricalWarehouse | None = None,
    code_version: str = "working-tree",
) -> dict[str, Any]:
    started = time.perf_counter()
    root_path = Path(root)
    verified = verify_corpora(root_path)
    duckdb = _duckdb()
    connection = duckdb.connect()
    evidence_ids = _register_file_evidence(warehouse, root_path, verified) if warehouse else {}
    try:
        summaries = _summaries(connection, root_path)
        corpus = root_path / "Pumpfun_Memecoin_Corpus"
        tokens = str(corpus / "tokens.parquet")
        outcomes = str(corpus / "postgard_outcomes.parquet")
        models = {
            name: _model_research(connection, tokens, outcomes, splits)
            for name, splits in WALK_FORWARDS.items()
        }
        drift = _drift(connection, tokens)
        materialization = (
            materialize_public_corpora(warehouse, connection, root_path, evidence_ids)
            if warehouse
            else {}
        )
    finally:
        connection.close()
    failure_classes = {
        row["outcome_label"]: row["sample"]
        for row in summaries["mature_outcomes"]["classes"]
        if row["outcome_label"] in {"dead", "pump_dump", "slow_bleed"}
    }
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "code_version": code_version,
        "verification": verified,
        "summaries": summaries,
        "research": {
            "splits": WALK_FORWARDS,
            "models": models,
            "drift": drift,
            "failure_corpus": {
                "not_graduated": (
                    int(summaries["launch_universe"]["launches"])
                    - int(summaries["launch_universe"]["graduated"])
                ),
                "zombie": int(summaries["launch_universe"]["zombies"]),
                "graduated_failure": sum(int(value) for value in failure_classes.values()),
                "direct_rug": sum(
                    int(row["rugs"]) for row in summaries["mature_outcomes"]["classes"]
                ),
                "definitions": {
                    "not_graduated": "did not complete the curve in the fixed capture horizon",
                    "zombie": "source-published inactive/zombie flag",
                    "graduated_failure": "dead, pump_dump, or slow_bleed after graduation",
                    "direct_rug": "source-published rug_detected boolean",
                },
            },
            "leakage": {
                "state": "PASS_WITH_LIMITATIONS",
                "excluded": [
                    "entry_price_20s_usd",
                    "entry_price_30s_usd",
                    "entry_price_1m_usd",
                    "graduated_at",
                    "seconds_to_graduation",
                    "peak_market_cap_sol",
                    "trade_count",
                    "outcome fields",
                    "1,486 top10_pct_suspect rows",
                ],
                "embargoes": ["June 20-24 regime boundary", "July 2-5 outage boundary"],
                "limitation": (
                    "launch fields lack per-field availability stamps; no feature is eligible "
                    "for approval from this release alone"
                ),
            },
            "adversarial_findings": [
                "entry-price missingness is a near-perfect graduation proxy and was excluded",
                "raw concentration values contain a corrected 2B-supply bug",
                "1,486 bonding-curve-balance contaminated rows were excluded",
                "wallet activity totals are stale and were not used",
                "MELT creator identity is constant and was rejected",
                "volume, buyer count, concentration and social presence remain independently gameable",
            ],
            "approval": {
                "approved": [],
                "research_only": list(ALL_LAUNCH_FEATURES),
                "rejected": [
                    "leaky entry-price fields",
                    "uncorrected concentration fields",
                    "MELT creator column",
                    "stale wallet activity totals",
                ],
                "decision": "RESEARCH_ONLY",
            },
        },
        "warehouse_file_evidence": evidence_ids,
        "warehouse_materialization": materialization,
        "elapsed_seconds": time.perf_counter() - started,
        "completion": {
            "code_complete": True,
            "data_complete": False,
            "research_complete": False,
            "historical_features_approved": 0,
            "challenger_ready": False,
            "prospective_validation_complete": False,
            "production_ready": False,
        },
    }
    return report


def write_report(report: dict[str, Any], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")


def storage_projection(report: dict[str, Any]) -> dict[str, Any]:
    launch_bytes = report["verification"]["launch_corpus"]["bytes"]
    launches = int(report["summaries"]["launch_universe"]["launches"])
    bytes_per_launch = launch_bytes / launches if launches else None
    return {
        "observed_bytes_per_launch_selected_files": bytes_per_launch,
        "estimated_100k_launches_bytes": bytes_per_launch * 100_000,
        "estimated_1m_launches_bytes": bytes_per_launch * 1_000_000,
        "note": "linear estimate for selected compact files; raw trades scale separately",
    }


def finite(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None

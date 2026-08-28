from __future__ import annotations

import argparse
import json
import os
import sqlite3
import urllib.request
from pathlib import Path
from typing import Any


def _health(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"staging health returned HTTP {response.status}")
        return json.loads(response.read().decode())


def _database(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        tables = int(
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
        )
    finally:
        connection.close()
    return {"path": str(path), "bytes": path.stat().st_size, "tables": tables}


def run(args: argparse.Namespace) -> dict[str, Any]:
    paths = [args.operational.resolve(), args.warehouse.resolve(), args.approved.resolve()]
    if len(set(paths)) != 3:
        raise ValueError("staging operational, warehouse and approved stores must be separate")
    if os.getenv("SHADOW_SEND_ALERTS", "false").strip().lower() not in {"0", "false", "no"}:
        raise ValueError("staging acceptance refuses SHADOW_SEND_ALERTS=true")
    production_markers = ("/app/data/memecoin.db", "/app/data/production/approved_features.db")
    if any(str(path).replace("\\", "/") in production_markers for path in paths):
        raise ValueError("staging acceptance refuses a known production database path")
    health = _health(args.health_url)
    databases = [_database(path) for path in paths]
    return {
        "state": "PASS",
        "health": health,
        "databases": databases,
        "separate_mutable_stores": True,
        "public_alerts": False,
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Gambit Jr V1.5 staging-only acceptance")
    value.add_argument("--health-url", default="http://127.0.0.1:18080/health")
    value.add_argument(
        "--operational", type=Path, default=Path("data/staging/operational/memecoin.db")
    )
    value.add_argument(
        "--warehouse", type=Path, default=Path("data/staging/historical/warehouse.db")
    )
    value.add_argument(
        "--approved", type=Path, default=Path("data/staging/approved/approved_features.db")
    )
    return value


def main() -> None:
    print(json.dumps(run(parser().parse_args()), indent=2, default=str))


if __name__ == "__main__":
    main()

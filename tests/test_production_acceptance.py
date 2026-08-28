from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from memecoin_bot.config import Settings
from memecoin_bot.database import Store
from memecoin_bot.historical import ApprovedFeatureStore


def test_production_acceptance_passes_migrated_registered_database(tmp_path, monkeypatch):
    database = tmp_path / "acceptance.db"
    monkeypatch.setenv("DATABASE_PATH", str(database))
    feature_database = tmp_path / "approved-features.db"
    monkeypatch.setenv("APPROVED_FEATURE_STORE_PATH", str(feature_database))
    monkeypatch.setenv("DISCORD_TOKEN", "acceptance-secret-must-not-appear")
    settings = Settings.from_env()
    store = Store(database)
    store.migrate()
    store.register_config_fingerprint(
        settings.config_fingerprint(),
        settings.software_version,
        settings.scoring_version,
        settings.radar_version,
        {},
    )
    store.close()
    ApprovedFeatureStore(feature_database).close()

    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["DATABASE_PATH"] = str(database)
    environment["DISCORD_TOKEN"] = "acceptance-secret-must-not-appear"
    environment["APPROVED_FEATURE_STORE_PATH"] = str(feature_database)
    completed = subprocess.run(
        [sys.executable, "scripts/production_acceptance.py"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout)
    assert report["result"] == "PASS"
    assert report["checks"]["discord_commands"]["count"] == 24
    assert report["checks"]["discord_py_version"] == "2.7.1"
    assert "acceptance-secret-must-not-appear" not in completed.stdout

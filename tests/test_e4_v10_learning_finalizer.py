from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from memecoin_bot.e4_pipelines_v10 import PipelineManager


class LearningFinalizerTests(unittest.TestCase):
    def manager(self) -> PipelineManager:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name, payload in (
            ("creator.json", {"top_creators": []}),
            ("discovered.json", {"creators": {}}),
            ("social.json", {"handles": {}}),
            ("intents.json", {"intents": []}),
        ):
            (root / name).write_text(json.dumps(payload), encoding="utf-8")
        old = {}
        for key, value in {
            "E4_CREATOR_EXPECTANCY_PATH": root / "creator.json",
            "E4_DISCOVERED_CREATORS_PATH": root / "discovered.json",
            "E4_SOCIAL_SOURCES_PATH": root / "social.json",
            "E4_AUTHORIZED_INTENTS_PATH": root / "intents.json",
            "E4_CREATOR_LEARNING_PATH": root / "learning.json",
            "E4_DISCOVERY_QUEUE_PATH": root / "queue.jsonl",
        }.items():
            old[key] = os.environ.get(key)
            os.environ[key] = str(value)

        def restore() -> None:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)
        return PipelineManager()

    def test_stale_learning_sweep_finalizes_real_paths(self) -> None:
        manager = self.manager()
        base = time.time_ns() - 600_000_000_000
        for index in range(6):
            mint = f"quiet-{index}"
            manager.observe_launch_event(
                mint=mint,
                creator="repeat-runner",
                received_ns=base + index,
                price_sol=1.0,
            )
            manager.observe_trade_event(
                mint=mint,
                received_ns=base + index + 1_000_000,
                price_sol=2.0,
                is_buy=True,
            )
        finalized = manager.finalize_stale_learning(
            now_ns=time.time_ns(),
            max_age_seconds=300,
            quiet_seconds=30,
        )
        self.assertEqual(finalized, 6)
        identity = manager.creator_identity("repeat-runner")
        self.assertIsNotNone(identity)
        self.assertEqual(identity.status, "ELITE")


if __name__ == "__main__":
    unittest.main()

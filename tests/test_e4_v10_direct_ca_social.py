from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from memecoin_bot.e4_pipelines_v10 import PipelineManager


class DirectCASocialTests(unittest.TestCase):
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
        keys = {
            "E4_CREATOR_EXPECTANCY_PATH": root / "creator.json",
            "E4_DISCOVERED_CREATORS_PATH": root / "discovered.json",
            "E4_SOCIAL_SOURCES_PATH": root / "social.json",
            "E4_AUTHORIZED_INTENTS_PATH": root / "intents.json",
            "E4_CREATOR_LEARNING_PATH": root / "learning.json",
            "E4_DISCOVERY_QUEUE_PATH": root / "queue.jsonl",
        }
        old = {key: os.environ.get(key) for key in keys}
        for key, value in keys.items():
            os.environ[key] = str(value)

        def restore() -> None:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)
        return PipelineManager()

    def test_exact_ca_post_after_creation_can_enter_within_guard(self) -> None:
        manager = self.manager()
        mint = "Aijysj19Tv4yYFvUunHQRqpkVDggU9GNUFJpYaetpump"
        launch_ns = time.time_ns()
        manager.observe_social_post(
            {
                "kind": "tweet",
                "id": "ca-post",
                "handle": "largeaccount",
                "text": f"Launching now {mint}",
                "created_ns": launch_ns + 300_000_000,
                "authority": 0.99,
                "novelty": 1.0,
                "engagement_velocity": 0.8,
            }
        )
        decision = manager.decide_launch(
            mint=mint,
            creator="unknown",
            name="Different Name",
            symbol="DIFF",
            metadata_uri="",
            launch_ns=launch_ns,
            now_ns=launch_ns + 320_000_000,
            fdv_usd=7_000,
            creator_buy_sol=1.0,
            sell_count=0,
            price_sol=0.000001,
        )
        self.assertTrue(decision.accepted, decision.reason)
        self.assertEqual(decision.family, "exact_ca_social_launch")

    def test_exact_ca_is_rejected_when_old_or_already_sold(self) -> None:
        manager = self.manager()
        mint = "GzVhofvBXc4kFSLF8Ndw26QN14WAPKiKiGc7WbcCpump"
        launch_ns = time.time_ns()
        manager.observe_social_post(
            {
                "kind": "tweet",
                "handle": "largeaccount",
                "text": mint,
                "created_ns": launch_ns + 200_000_000,
                "authority": 1.0,
                "novelty": 1.0,
            }
        )
        old = manager.decide_launch(
            mint=mint,
            creator="unknown",
            name="",
            symbol="",
            metadata_uri="",
            launch_ns=launch_ns,
            now_ns=launch_ns + 6_000_000_000,
            fdv_usd=7_000,
            creator_buy_sol=1.0,
            sell_count=0,
            price_sol=0.000001,
        )
        sold = manager.decide_launch(
            mint=mint,
            creator="unknown",
            name="",
            symbol="",
            metadata_uri="",
            launch_ns=launch_ns,
            now_ns=launch_ns + 300_000_000,
            fdv_usd=7_000,
            creator_buy_sol=1.0,
            sell_count=1,
            price_sol=0.000001,
        )
        self.assertFalse(old.accepted)
        self.assertFalse(sold.accepted)


if __name__ == "__main__":
    unittest.main()

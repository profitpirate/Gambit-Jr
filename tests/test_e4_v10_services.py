from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from memecoin_bot.e4_pipelines_v10 import E4Learner, E4Observation, PipelineRuntime
from memecoin_bot.e4_runtime_services_v10 import E4OutcomeTeacher, JsonlTailer
from scripts.e4_creator_learner_v10 import promote


class TeacherJournalTests(unittest.TestCase):
    def test_e4_observation_is_deduplicated_and_journaled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "teacher.jsonl"
            learner = E4Learner(path)
            observation = E4Observation(
                observation_id="obs-1",
                mint="mint",
                creator="creator",
                signature="sig",
                observed_ns=time.time_ns(),
                sol_amount=1.0,
            )
            self.assertTrue(learner.observe(observation))
            self.assertFalse(learner.observe(observation))
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not path.exists():
                time.sleep(0.01)
            learner.stop()
            self.assertTrue(path.exists())
            rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["creator"], "creator")


class OutcomeTeacherTests(unittest.TestCase):
    class Event:
        def __init__(self, kind: str, *, tokens: float, sol: float, at_ns: int):
            self.trader = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"
            self.kind = kind
            self.mint = "mint"
            self.token_amount = tokens
            self.sol_amount = sol
            self.received_ns = at_ns
            self.creator = "creator"

    def test_closed_e4_position_promotes_live_creator_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            teacher = E4OutcomeTeacher(root / "overlay.json", root / "outcomes.jsonl")
            now = time.time_ns()
            teacher.observe_event(self.Event("BUY", tokens=1000, sol=1.0, at_ns=now))
            teacher.observe_event(self.Event("SELL", tokens=1000, sol=1.2, at_ns=now + 1_000_000))
            profile = teacher.overlay["creator"]
            self.assertEqual(profile.wins, 1)
            self.assertEqual(profile.losses, 0)
            self.assertGreater(profile.gross_pnl_sol, 0)
            self.assertTrue((root / "overlay.json").exists())
            self.assertTrue((root / "outcomes.jsonl").exists())


class SocialTailTests(unittest.TestCase):
    def test_jsonl_tailer_feeds_narrative_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / "social.jsonl"
            runtime = PipelineRuntime()
            def callback(row):
                runtime.observe_social_post(
                    source=row["source"],
                    source_account=row["source_account"],
                    text=row["text"],
                    created_ns=row["created_ns"],
                    observed_ns=row["observed_ns"],
                    authority=row["authority"],
                    engagement_velocity=row["engagement_velocity"],
                )
            tailer = JsonlTailer(journal, callback, poll_seconds=0.01, name="test-social-tail")
            tailer.start()
            now = time.time_ns()
            journal.write_text(json.dumps({
                "source": "x",
                "source_account": "authority",
                "text": "Blue Lobster Revolution is coming",
                "created_ns": now - 1_000_000_000,
                "observed_ns": now - 900_000_000,
                "authority": 0.99,
                "engagement_velocity": 0.9,
            }) + "\n")
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and runtime.narratives.size == 0:
                time.sleep(0.01)
            tailer.stop(); tailer.join(timeout=1.0)
            match = runtime.narratives.match_launch(
                name="Blue Lobster Revolution",
                symbol="BLR",
                uri=None,
                mint=None,
                launch_ns=now,
            )
            runtime.teacher.stop()
            self.assertTrue(match.matched, match.reason)


class CreatorPromotionTests(unittest.TestCase):
    def test_external_runner_history_promotes_unknown_creator(self) -> None:
        launches = [
            {"max_market_cap_usd": 55_000, "max_multiple": 3.0},
            {"max_market_cap_usd": 140_000, "max_multiple": 8.0},
            {"max_market_cap_usd": 65_000, "max_multiple": 4.0},
        ]
        status, score, evidence = promote(launches, 0, 0, 0)
        self.assertEqual(status, "APPROVED")
        self.assertGreater(score, 0.80)
        self.assertTrue(any(item.startswith("runners:") for item in evidence))

    def test_repeated_e4_success_promotes_elite(self) -> None:
        status, score, _ = promote([], 4, 4, 0)
        self.assertEqual(status, "ELITE")
        self.assertGreaterEqual(score, 0.90)


if __name__ == "__main__":
    unittest.main()

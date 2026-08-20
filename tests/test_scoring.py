from __future__ import annotations

import unittest

from memecoin_bot.config import Settings
from memecoin_bot.models import Availability, Metric, SignalClass, iso
from memecoin_bot.scoring import ScoringEngine


class ScoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = ScoringEngine(Settings())

    def test_exact_score_and_classification(self) -> None:
        result = self.engine.score({
            "narrative": 24, "social": 17, "onchain": 18,
            "developer": 14, "momentum": 12, "safety": 5,
        }, [])
        self.assertEqual(result.total, 90)
        self.assertEqual(result.classification, SignalClass.HIGH_CONVICTION)
        self.assertEqual(result.confidence, 1)

    def test_hard_reject_overrides_perfect_score(self) -> None:
        result = self.engine.score({key: value for key, value in Settings().weights.items()}, ["KNOWN_BAD_DEV"])
        self.assertEqual(result.total, 100)
        self.assertEqual(result.classification, SignalClass.REJECT)

    def test_unknown_is_not_a_real_zero_and_reduces_confidence(self) -> None:
        metric = Metric(None, Availability.UNKNOWN, "provider", iso())
        self.assertIsNone(metric.value)
        result = self.engine.score({
            "narrative": None, "social": None, "onchain": 18,
            "developer": None, "momentum": 12, "safety": 5,
        }, [])
        self.assertEqual(result.component_scores["social"], 0)
        self.assertLess(result.confidence, Settings().min_confidence_for_signal)
        self.assertEqual(result.classification, SignalClass.IGNORE)


if __name__ == "__main__":
    unittest.main()


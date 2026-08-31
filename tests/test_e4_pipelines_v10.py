from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import MappingProxyType

from memecoin_bot import e4_hardening_v10 as v10
from memecoin_bot.e4_pipelines_v10 import (
    CreatorProfile,
    CreatorRegistry,
    CreatorSnapshot,
    CreatorTier,
    LaunchIntentRegistry,
    NarrativeCache,
)

core = v10.core


def event(
    event_id: int,
    kind,
    mint: str,
    at_ns: int,
    *,
    trader: str | None = None,
    creator: str | None = None,
    sol: float = 0.0,
    price: float = 0.000001,
    fdv: float = 5_000.0,
    signature: str | None = None,
):
    return core.Event(
        event_id=event_id,
        kind=kind,
        mint=mint,
        source_ns=at_ns,
        received_ns=at_ns,
        trader=trader,
        creator=creator,
        sol_amount=sol,
        token_amount=1_000.0,
        price_sol=price,
        fdv_usd=fdv,
        signature=signature or f"sig-{event_id}",
    )


def state_for(creator: str, mint: str = "mint", *, buyers: int = 0):
    now = time.time_ns()
    state = core.TokenState(mint)
    state.apply(
        event(1, core.EventKind.CREATE, mint, now, creator=creator, trader=creator, fdv=3_000),
        None,
    )
    state.apply(
        event(2, core.EventKind.BUY, mint, now + 1_000_000, creator=creator, trader=creator, sol=2.0, price=1.05e-6, fdv=3_300),
        None,
    )
    for index in range(buyers):
        state.apply(
            event(
                3 + index,
                core.EventKind.BUY,
                mint,
                now + (2 + index) * 1_000_000,
                trader=f"buyer-{index}",
                sol=2.0,
                price=(1.10 + index * 0.08) * 1e-6,
                fdv=3_600 + index * 300,
            ),
            None,
        )
    return state


class CreatorRegistryTests(unittest.TestCase):
    def test_expectancy_and_external_runner_models_merge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expectancy = root / "expectancy.json"
            discovered = root / "discovered.json"
            expectancy.write_text(
                json.dumps(
                    {
                        "version": "test",
                        "top_creators": [
                            {"creator": "elite", "wins": 7, "losses": 1, "trades": 8, "gross_win_rate": 0.875},
                            {"creator": "bad", "wins": 0, "losses": 4, "trades": 4, "gross_win_rate": 0.0},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            discovered.write_text(
                json.dumps(
                    {
                        "version": "external",
                        "creators": {
                            "runner": {"status": "APPROVED", "score": 0.86, "max_peak_market_cap_usd": 138_000},
                            "elite": {"status": "APPROVED", "score": 0.82},
                        },
                    }
                ),
                encoding="utf-8",
            )
            registry = CreatorRegistry(expectancy, discovered, root / "operators.json")
            self.assertEqual(registry.lookup("elite").tier, CreatorTier.ELITE)
            self.assertEqual(registry.lookup("bad").tier, CreatorTier.NEGATIVE)
            self.assertEqual(registry.lookup("runner").tier, CreatorTier.APPROVED)

    def test_lookup_hot_path_is_far_below_36ms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "expectancy.json").write_text(
                json.dumps(
                    {
                        "top_creators": [
                            {"creator": f"creator-{index}", "wins": 3, "losses": 0, "trades": 3}
                            for index in range(1_000)
                        ]
                    }
                ),
                encoding="utf-8",
            )
            registry = CreatorRegistry(root / "expectancy.json", root / "none.json", root / "operators.json")
            maximum = 0
            started = time.perf_counter_ns()
            for index in range(100_000):
                call = time.perf_counter_ns()
                registry.lookup(f"creator-{index % 1_000}")
                maximum = max(maximum, time.perf_counter_ns() - call)
            elapsed = time.perf_counter_ns() - started
            self.assertLess(maximum, 36_000_000)
            self.assertLess(elapsed / 100_000, 100_000)


class NarrativeTests(unittest.TestCase):
    def test_prelaunch_high_authority_exact_phrase_matches(self) -> None:
        cache = NarrativeCache(ttl_seconds=600)
        now = time.time_ns()
        cache.observe(
            source="x-filtered-stream",
            source_account="huge-account",
            text="The Brown Hoodie Kid is taking over tonight",
            created_ns=now - 5_000_000_000,
            observed_ns=now - 4_900_000_000,
            authority=0.98,
            engagement_velocity=0.90,
        )
        match = cache.match_launch(
            name="Brown Hoodie Kid",
            symbol="BHK",
            uri="https://metadata.example/bhk.json",
            mint="11111111111111111111111111111111",
            launch_ns=now,
        )
        self.assertTrue(match.matched, match.reason)
        self.assertGreaterEqual(match.score, 0.76)

    def test_post_launch_post_cannot_authorize_entry(self) -> None:
        cache = NarrativeCache(ttl_seconds=600)
        launch = time.time_ns()
        cache.observe(
            source="x",
            source_account="account",
            text="Future Phrase Rocket",
            created_ns=launch + 1_000_000_000,
            observed_ns=launch + 1_100_000_000,
            authority=1.0,
            engagement_velocity=1.0,
        )
        match = cache.match_launch(
            name="Future Phrase Rocket",
            symbol="FPR",
            uri=None,
            mint=None,
            launch_ns=launch,
        )
        self.assertFalse(match.matched)


class LaunchIntentTests(unittest.TestCase):
    def test_hmac_intent_is_authenticated_and_matches_once(self) -> None:
        registry = LaunchIntentRegistry()
        secret = "test-secret"
        now = time.time_ns()
        payload = {
            "creator": "creator",
            "mint": "mint",
            "issued_ns": now,
            "expires_ns": now + 60_000_000_000,
            "max_buy_sol": 0.5,
            "source": "test",
            "nonce": "nonce-1",
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        payload["signature"] = hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
        intent = registry.ingest(payload, secret=secret)
        self.assertEqual(registry.match("creator", "mint", now).intent_id, intent.intent_id)
        registry.consume(intent.intent_id)
        self.assertIsNone(registry.match("creator", "mint", now))


class V10PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_snapshot = v10.PIPELINES.creators._snapshot
        v10.v6._PROFILE_BY_MINT.clear()
        v10.v6._CONTEXT_BY_MINT.clear()

    def tearDown(self) -> None:
        v10.PIPELINES.creators._snapshot = self.previous_snapshot
        v10.v6._PROFILE_BY_MINT.clear()
        v10.v6._CONTEXT_BY_MINT.clear()

    def set_profiles(self, *profiles: CreatorProfile) -> None:
        v10.PIPELINES.creators._snapshot = CreatorSnapshot(
            MappingProxyType({row.creator: row for row in profiles}),
            time.time_ns(),
            "test",
        )

    def test_elite_creator_enters_without_public_momentum(self) -> None:
        self.set_profiles(
            CreatorProfile("elite", CreatorTier.ELITE, 0.95, 7, 1, 8, 0.875, source="test")
        )
        accepted, score, fraction, reason, features = core.E4Policy(
            core.Settings(model_path=Path("missing.json"))
        ).entry(state_for("elite"))
        self.assertTrue(accepted, reason)
        self.assertIn("elite_recurring_creator", reason)
        self.assertGreater(score, 0.90)
        self.assertGreater(fraction, 0)
        self.assertLess(features["e4_v10_decision_latency_ns"], 36_000_000)

    def test_negative_creator_is_vetoed(self) -> None:
        self.set_profiles(
            CreatorProfile("bad", CreatorTier.NEGATIVE, 0.05, 0, 4, 4, 0.0, source="test")
        )
        accepted, _, _, reason, _ = core.E4Policy(
            core.Settings(model_path=Path("missing.json"))
        ).entry(state_for("bad", buyers=5))
        self.assertFalse(accepted)
        self.assertIn("negative creator", reason)

    def test_unknown_creator_is_not_authorized_by_large_public_burst(self) -> None:
        self.set_profiles()
        accepted, _, _, reason, _ = core.E4Policy(
            core.Settings(model_path=Path("missing.json"))
        ).entry(state_for("unknown", buyers=8))
        self.assertFalse(accepted)
        self.assertIn("no approved creator", reason)

    def test_timely_e4_confirmation_can_authorize_small_copy(self) -> None:
        self.set_profiles()
        state = state_for("unknown")
        v10.v6._CONTEXT_BY_MINT[state.mint].update(
            {
                "e4_confirmed": True,
                "e4_observed_ns": state.latest_ns,
                "e4_entry_price": state.price_sol,
            }
        )
        accepted, _, fraction, reason, _ = core.E4Policy(
            core.Settings(model_path=Path("missing.json"))
        ).entry(state)
        self.assertTrue(accepted, reason)
        self.assertIn("copy_safe", reason)
        self.assertLessEqual(fraction, 0.0185)


if __name__ == "__main__":
    unittest.main()

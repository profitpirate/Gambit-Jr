from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from memecoin_bot.discord.cards import v3_operator_preview_card
from memecoin_bot.historical.intelligence_v3 import (
    CompetingOutcome,
    CurveState,
    DiscreteCompetingRiskModel,
    DiscreteHazardRow,
    EntryActionability,
    HazardForecast,
    MarketPathPoint,
    SelectiveGatePolicy,
    TimedValue,
    TradeEvent,
    V3ShadowEngine,
    V3Tier,
    activity_evidence,
    assess_actionable_outcome,
    liquidity_order_flow_features,
)
from memecoin_bot.historical.intelligence_v3_research import (
    assert_group_isolation,
    calibration_report,
    group_purge_window,
    natural_prevalence_sample_weights,
    nested_walk_forward_windows,
    precision_frequency_frontier,
)
from memecoin_bot.historical.wallet_v3 import (
    FollowerOutcome,
    WalletHistory,
    copyability_scores,
    independent_wallet_consensus,
    select_wallet_corpus,
)
from memecoin_bot.models import DiscoveryEvent, iso
from memecoin_bot.narratives import NarrativeEngine, NarrativeObservation
from memecoin_bot.social import SocialEngine, SocialObservation
from tests.helpers import store as create_store
from tests.helpers import temp_db_path

NOW = "2026-08-28T12:00:00+00:00"


def _known_evidence() -> dict[str, TimedValue]:
    return {
        name: TimedValue(index, NOW, NOW, "native")
        for index, name in enumerate(("reserve", "buyers", "flow", "risk", "entry"), 1)
    }


def test_v3_unvalidated_model_abstains_and_preserves_comparators() -> None:
    envelope = V3ShadowEngine().evaluate(
        decision_timestamp=NOW,
        evidence=_known_evidence(),
        forecast=HazardForecast(
            quick_2x=0.99,
            terminal_failure=0.01,
            calibrated=True,
            validation_state="SEALED_VALIDATED",
        ),
        actionability=EntryActionability(True, 0.9, True, 30),
        legacy_result={"classification": "REJECT"},
        v15_result={"signal_tier": "REJECT"},
    )
    assert envelope.research_tier == V3Tier.SILENT_WATCH
    assert envelope.abstain_reason == "UNVALIDATED_RESEARCH_MODEL"
    assert envelope.legacy_result["classification"] == "REJECT"
    assert envelope.v15_result["signal_tier"] == "REJECT"
    assert envelope.public_route is False


def test_v3_validated_gate_owns_research_decision_without_legacy_double_gate() -> None:
    engine = V3ShadowEngine(
        SelectiveGatePolicy(version="sealed-gate-1", validated=True)
    )
    envelope = engine.evaluate(
        decision_timestamp=NOW,
        evidence=_known_evidence(),
        forecast=HazardForecast(
            quick_2x=0.75,
            mid_5x=0.25,
            right_tail=0.08,
            terminal_failure=0.02,
            liquidity_failure=0.01,
            calibrated=True,
            validation_state="SEALED_VALIDATED",
        ),
        actionability=EntryActionability(True, 0.9, True, 30),
        legacy_result={"classification": "REJECT"},
        v15_result={"signal_tier": "REJECT"},
    )
    assert envelope.research_tier == V3Tier.PREMIUM
    assert envelope.precision_gate == "ACCEPTED_PREMIUM"


def test_v3_hard_risk_caps_even_a_validated_forecast() -> None:
    engine = V3ShadowEngine(SelectiveGatePolicy(version="sealed", validated=True))
    envelope = engine.evaluate(
        decision_timestamp=NOW,
        evidence=_known_evidence(),
        forecast=HazardForecast(
            quick_2x=0.9,
            terminal_failure=0.01,
            calibrated=True,
            validation_state="SEALED_VALIDATED",
        ),
        actionability=EntryActionability(True, 0.9, True, 0),
        legacy_result={},
        v15_result={},
        hard_risk_evidence=["MINT_AUTHORITY_ACTIVE"],
    )
    assert envelope.research_tier == V3Tier.REJECT
    assert envelope.risk_cap == "HARD_RISK"


def test_v3_rejects_future_and_naive_timestamps() -> None:
    future = "2026-08-28T12:00:01+00:00"
    with pytest.raises(ValueError, match="future evidence"):
        V3ShadowEngine().evaluate(
            decision_timestamp=NOW,
            evidence={"future": TimedValue(1, NOW, future, "provider")},
            forecast=None,
            actionability=EntryActionability(False, None, None, 0),
            legacy_result={},
            v15_result={},
        )
    with pytest.raises(ValueError, match="timezone"):
        TimedValue(1, NOW, NOW, "provider").validate_at("2026-08-28T12:00:00")


def test_liquidity_order_flow_keeps_real_virtual_market_and_price_separate() -> None:
    launch = "2026-08-28T11:59:00+00:00"
    features = liquidity_order_flow_features(
        decision_timestamp=NOW,
        launched_at=launch,
        trades=[
            TradeEvent("2026-08-28T11:59:10+00:00", "a", "buy", 2, cluster_id="x"),
            TradeEvent("2026-08-28T11:59:20+00:00", "b", "buy", 3, cluster_id="x"),
            TradeEvent(
                "2026-08-28T11:59:30+00:00",
                "creator",
                "buy",
                5,
                creator_linked=True,
                wash=True,
            ),
            TradeEvent("2026-08-28T11:59:40+00:00", "a", "sell", 1),
        ],
        curve_states=[
            CurveState(launch, 0, 30, 0, 30, None, 0.000001),
            CurveState(NOW, 9, 39, 0.2, 400, 1000, 0.00001),
        ],
        windows_seconds=(60,),
    )
    assert features["real_sol_reserve"] == 9
    assert features["virtual_sol_reserve"] == 39
    assert features["market_cap"] == 400
    assert features["price"] == 0.00001
    window = features["windows"]["60"]
    assert window["unique_buyers"] == 3
    assert window["independent_buyers"] == 1
    assert window["net_sol_flow"] == 9
    assert window["wash_adjusted_volume_sol"] == 6
    assert window["creator_linked_buy_share"] == 0.5


def test_liquidity_order_flow_refuses_future_events() -> None:
    with pytest.raises(ValueError, match="future trade"):
        liquidity_order_flow_features(
            decision_timestamp=NOW,
            launched_at="2026-08-28T11:59:00+00:00",
            trades=[TradeEvent("2026-08-28T12:00:01+00:00", "a", "buy", 1)],
            curve_states=[],
        )


@pytest.mark.parametrize(
    ("changes", "flag"),
    [
        ({"wash_adjusted_volume": 10}, "wash_volume"),
        ({"independent_buyers": 2}, "sybil_buyer_growth"),
        ({"creator_linked_share": 0.8}, "creator_linked_flow"),
        ({"bundle_linked_share": 0.8}, "bundle_driven_flow"),
        ({"tiny_buy_share": 0.8}, "repeated_tiny_buys"),
        ({"recycled_share": 0.8}, "buy_sell_recycle"),
    ],
)
def test_adversarial_activity_adjustments(changes: dict[str, float], flag: str) -> None:
    inputs = {
        "raw_buyers": 10,
        "independent_buyers": 10,
        "raw_volume": 100.0,
        "wash_adjusted_volume": 100.0,
        "creator_linked_share": 0.0,
        "bundle_linked_share": 0.0,
        "whale_share": 0.1,
        "tiny_buy_share": 0.1,
        "recycled_share": 0.1,
    }
    inputs.update(changes)
    assert activity_evidence(**inputs)[flag] is True


def test_discrete_competing_risk_model_handles_time_varying_covariates() -> None:
    rows = []
    for sample in range(30):
        for interval in range(3):
            event = None
            if interval == 2:
                event = "2X" if sample % 2 == 0 else "TERMINAL_FAILURE"
            rows.append(
                DiscreteHazardRow(
                    interval=interval,
                    features={"flow": 1.0 if sample % 2 == 0 else -1.0},
                    event=event,
                )
            )
    model = DiscreteCompetingRiskModel(
        feature_names=("flow",), model_version="unit-research"
    )
    model.fit(rows, iterations=40)
    forecast = model.forecast([{"flow": 1.0}, {"flow": 1.2}, {"flow": 1.4}])
    assert forecast.quick_2x is not None
    assert forecast.terminal_failure is not None
    assert forecast.calibrated is False
    assert forecast.validation_state == "UNVALIDATED"


def test_actionable_outcome_applies_delay_cost_and_sellability() -> None:
    path = [
        MarketPathPoint(NOW, 1, 1000, 2000, True),
        MarketPathPoint("2026-08-28T12:00:15+00:00", 2, 2000, 500, True),
        MarketPathPoint("2026-08-28T12:00:30+00:00", 5, 5000, 500, False),
    ]
    immediate = assess_actionable_outcome(
        decision_timestamp=NOW,
        path=path,
        delay_seconds=0,
        fee_percent=0,
        trade_notional_usd=0,
    )
    delayed = assess_actionable_outcome(
        decision_timestamp=NOW,
        path=path,
        delay_seconds=30,
    )
    assert immediate.actionable_outcome == CompetingOutcome.HIT_2X_BEFORE_STOP
    assert delayed.actionable_outcome == CompetingOutcome.UNSELLABLE


def _walk_rows() -> list[dict[str, object]]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    for day in range(90):
        rows.append(
            {
                "decision_timestamp": (start + timedelta(days=day)).isoformat(),
                "creator_id": f"creator-{day}",
                "funder_id": f"funder-{day}",
                "wallet_cluster_id": f"wallet-{day}",
                "peak_multiple": 5 if day % 10 == 0 else 1,
            }
        )
    return rows


def test_nested_walk_forward_has_embargo_and_group_purge() -> None:
    rows = _walk_rows()
    windows = nested_walk_forward_windows(
        rows,
        train_days=20,
        validation_days=10,
        test_days=10,
        maturity_embargo_days=3,
    )
    assert len(windows) >= 3
    first = windows[0]
    assert_group_isolation(rows, first)
    rows[first.train_indexes[0]]["creator_id"] = rows[first.test_indexes[0]]["creator_id"]
    with pytest.raises(ValueError, match="creator_id leakage"):
        assert_group_isolation(rows, first)
    purged = group_purge_window(rows, first)
    assert len(purged.train_indexes) == len(first.train_indexes) - 1
    assert_group_isolation(rows, purged)


def test_prevalence_calibration_and_frontier_helpers() -> None:
    weights = natural_prevalence_sample_weights(
        [True, True, False, False, False, False],
        [True, True, True, False, False, False],
    )
    assert sum(weight for weight, label in zip(weights, [True, True, False, False, False, False], strict=True) if label) == 2
    assert sum(weight for weight, label in zip(weights, [True, True, False, False, False, False], strict=True) if not label) == 4
    report = calibration_report([0.9, 0.1], [True, False], bins=2)
    assert report["prevalence"] == 0.5
    frontier = precision_frequency_frontier(
        _walk_rows(),
        [float(index) for index in range(90)],
        outcome=lambda row, threshold: float(row["peak_multiple"]) >= threshold,
        frequencies=(0.1,),
    )
    assert frontier[0]["signal_count"] == 9
    assert frontier[0]["2x_wilson_95"][0] is not None


def test_v3_shadow_store_is_disconnected_from_outbox() -> None:
    with temp_db_path() as path:
        store = create_store(path)
        try:
            token_id, _ = store.upsert_discovery(
                DiscoveryEvent(token_address="v3-shadow")
            )
            candidate_id, _ = store.ensure_candidate(token_id, iso(), "v1.5")
            envelope = V3ShadowEngine().evaluate(
                decision_timestamp=NOW,
                evidence=_known_evidence(),
                forecast=None,
                actionability=EntryActionability(False, None, None, 0),
                legacy_result={},
                v15_result={},
            ).to_dict()
            before = store.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
            store.save_v3_shadow_decision(
                candidate_id=candidate_id,
                token_id=token_id,
                envelope=envelope,
                control_decision={},
                v2_decision={},
                features={},
            )
            after = store.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
            assert before == after == 0
            with pytest.raises(sqlite3.IntegrityError, match="no public notifier route"):
                store.conn.execute(
                    "UPDATE intelligence_v3_shadow_decisions SET public_route=1"
                )
        finally:
            store.close()


def test_social_attention_sentiment_and_source_are_separate_and_pit() -> None:
    observations = [
        SocialObservation(
            "2026-08-28T11:59:00+00:00",
            "2026-08-28T11:59:01+00:00",
            "licensed-provider",
            "x",
            unique_mentioners=10,
            mentions=20,
            sentiment=0.8,
            bot_spam_share=0.5,
            official_mentions=10,
            investor_mentions=10,
        ),
        SocialObservation(
            NOW,
            NOW,
            "licensed-provider",
            "x",
            unique_mentioners=20,
            mentions=50,
            sentiment=0.6,
            bot_spam_share=0.5,
            official_mentions=30,
            investor_mentions=20,
        ),
    ]
    result = SocialEngine().assess_history(observations, NOW)
    assert result["score"] is None
    assert result["independent_attention"] == 20
    assert result["sentiment"] == 0.6
    assert result["bot_adjusted_sentiment"] == 0.3
    assert result["official_channel_activity"] == 30
    with pytest.raises(ValueError, match="future social evidence"):
        SocialEngine().assess_history(
            [
                SocialObservation(
                    NOW,
                    "2026-08-28T12:00:01+00:00",
                    "provider",
                    "x",
                )
            ],
            NOW,
        )


def test_narrative_keyword_identity_is_not_velocity_and_history_stays_unscored() -> None:
    result = NarrativeEngine().assess_history(
        [
            NarrativeObservation("AI", "2026-08-28T11:59:00+00:00", NOW, 1, 2),
            NarrativeObservation("AI", NOW, NOW, 5, 8, leader_token="leader"),
        ],
        NOW,
    )
    assert result["score"] is None
    assert result["narrative_velocity"] is not None
    assert result["saturation"] == 8


def test_wallet_selection_is_sample_recency_concentration_and_identity_aware() -> None:
    good = WalletHistory("good", 30, 100, 500, 1000, 1.2, 0.6, 0.2, 3, 0.3, 1)
    insider = WalletHistory(
        "insider",
        30,
        100,
        500,
        1000,
        1.2,
        0.6,
        0.9,
        1,
        0.3,
        1,
        identity_labels=("developer",),
    )
    selected, rejected = select_wallet_corpus([good, insider])
    assert [row.wallet for row in selected] == ["good"]
    assert "PROHIBITED_IDENTITY" in rejected["insider"]
    assert "PNL_CONCENTRATED_IN_ONE_TOKEN" in rejected["insider"]


def test_copyability_requires_all_delays_and_linked_wallets_count_once() -> None:
    rows = [
        FollowerOutcome("w", "t", delay, True, True, 3, 0.2, False, 300)
        for delay in (15, 30, 60, 120)
    ]
    copyable = copyability_scores(rows)
    assert copyable["copyable_2x_skill"] == 1
    with pytest.raises(ValueError, match="requires 15"):
        copyability_scores(rows[:-1])
    consensus = independent_wallet_consensus(
        {"a": 0.9, "b": 0.8, "c": 0.7}, [("a", "b")]
    )
    assert consensus["raw_wallet_count"] == 3
    assert consensus["independent_validated_wallet_count"] == 2


def test_v3_operator_preview_is_explicitly_research_only() -> None:
    preview = v3_operator_preview_card(
        {
            "symbol": "V3",
            "token_address": "Token111",
            "chain": "solana",
            "quick_2x_hazard": None,
            "coverage": 0.4,
            "uncertainty": 0.6,
            "positive_evidence": ["real reserve increasing"],
            "negative_evidence": ["wallet graph unavailable"],
        }
    )
    assert "Operator/test-guild preview only" in preview["embed"]["description"]
    assert "NO PUBLIC ROUTE" in preview["embed"]["footer"]["text"]
    assert preview["embed"]["fields"][0]["value"] == "UNKNOWN"

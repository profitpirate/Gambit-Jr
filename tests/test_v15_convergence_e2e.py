from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from memecoin_bot.database import Store
from memecoin_bot.discord.cards import performance_card, status_card
from memecoin_bot.realtime import CanonicalEvent, CanonicalEventFabric, CanonicalEventType
from memecoin_bot.realtime.features import RealtimeFeatureProjector
from memecoin_bot.realtime.learning import AdaptiveLearningLab
from memecoin_bot.realtime.thesis import RunnerThesisEngine


def _event(
    now: datetime,
    kind: CanonicalEventType,
    seconds: float,
    identity: str,
    payload: dict,
) -> CanonicalEvent:
    timestamp = (now + timedelta(seconds=seconds)).isoformat()
    return CanonicalEvent.create(
        kind,
        "ConvergenceE2E111111111111111111111111111",
        "solana",
        "pumpfun",
        "source-to-learning-e2e",
        timestamp,
        received_timestamp=timestamp,
        available_timestamp=timestamp,
        transaction_signature=identity,
        source_event_id=f"{identity}:0",
        payload=payload,
    )


def test_canonical_source_to_shadow_outcome_autopsy_hypothesis_and_challenger(tmp_path):
    store = Store(tmp_path / "source-to-learning.db", Path("migrations"))
    store.migrate()
    try:
        now = datetime(2026, 8, 29, 10, tzinfo=UTC)
        fabric = CanonicalEventFabric(store)
        created = _event(
            now,
            CanonicalEventType.TOKEN_CREATED,
            0,
            "create",
            {
                "creator": "CreatorE2E",
                "bonding_curve": "CurveE2E",
                "real_token_reserves": 1_000,
            },
        )
        assert fabric.publish(created).is_new
        token_id, _ = fabric.project(created)
        assert token_id

        events = [
            _event(
                now,
                CanonicalEventType.TOKEN_TRADE,
                2 + index * 2,
                f"buy-{index}",
                {"actor": f"Buyer{index}", "side": "buy", "sol_amount": 1.0},
            )
            for index in range(20)
        ]
        events.insert(
            8,
            _event(
                now,
                CanonicalEventType.TOKEN_TRADE,
                17,
                "first-sell",
                {"actor": "SellerE2E", "side": "sell", "sol_amount": 1.0},
            ),
        )
        events.extend(
            (
                _event(
                    now,
                    CanonicalEventType.BONDING_CURVE_STATE,
                    10,
                    "curve-10",
                    {"real_token_reserves": 900, "real_sol_reserves": 2_000_000_000},
                ),
                _event(
                    now,
                    CanonicalEventType.BONDING_CURVE_STATE,
                    50,
                    "curve-50",
                    {"real_token_reserves": 400, "real_sol_reserves": 20_000_000_000},
                ),
            )
        )
        for event in events:
            assert fabric.publish(event).is_new
            fabric.project(event)
        assert fabric.publish(events[0]).status == "DUPLICATE"

        decision_at = (now + timedelta(seconds=55)).isoformat()
        feature = RealtimeFeatureProjector(store).compute(token_id, decision_at)
        feature["actor_intelligence"] = {
            "wallet_consensus": {
                "independent_smart_wallet_count": 3,
                "linked_wallet_share": 0.04,
            },
            "funder": {"funder_independence": 0.95, "creator_link_score": 0.04},
        }
        feature["activity_adjustment"].update(
            {"wash_probability": 0.04, "linked_wallet_share": 0.04, "bundle_linked_share": 0.04}
        )
        feature["coverage"].update({"wallet_linkage": True, "funder": True, "bundle": True})

        thesis = RunnerThesisEngine(store)
        first = thesis.evaluate(token_id, decision_at, feature, trigger_event_id=events[-1].event_id)
        second_at = (now + timedelta(seconds=60)).isoformat()
        second = thesis.evaluate(token_id, second_at, feature, trigger_event_id=events[-1].event_id)
        assert first.runner_probability > first.failure_probability
        assert first.actionable_probability > 0.55
        assert second.call_readiness == "SHADOW_CALL_READY"
        call = store.conn.execute("SELECT * FROM prospective_shadow_calls_v15").fetchone()
        assert call and call["public_route"] == 0

        settled = thesis.settle_shadow_call(
            call["shadow_call_id"],
            outcome_available_at=(now + timedelta(hours=25)).isoformat(),
            peak_multiple=6,
            maximum_adverse_excursion=-0.18,
            terminal_failure=False,
            time_to_2x_seconds=210,
            evidence={"source": "matured-e2e-outcome"},
        )
        assert settled["error_class"] == "TRUE_RUNNER"
        assert store.conn.execute("SELECT COUNT(*) FROM runner_reflections_v15").fetchone()[0] == 1
        assert store.conn.execute("SELECT COUNT(*) FROM runner_analogue_memory_v15").fetchone()[0] == 1

        rows = [
            {
                "entity_key": f"learning-{index}",
                "decision_at": (now + timedelta(days=index)).isoformat(),
                "peak_multiple": 6 if index % 3 == 0 else 0.7,
                "terminal_failure": index % 3 != 0,
                "features": {"capital_velocity": 2.0 if index % 3 == 0 else -1.0},
                "control_score": float(index % 5),
                "v3_score": float(index % 4),
                "stage": "EARLY_CURVE",
                "copyable": True,
                "stage_a_selected": True,
                "stage_b_selected": index % 2 == 0,
            }
            for index in range(40)
        ]
        learning = AdaptiveLearningLab(store).run(
            rows,
            development_end=(now + timedelta(days=25)).isoformat(),
            validation_end=(now + timedelta(days=41)).isoformat(),
        )
        assert learning["status"] == "MEASURED_SHADOW_ONLY"
        assert learning["hypotheses"]
        assert learning["public_route"] is False
        assert store.conn.execute("SELECT COUNT(*) FROM challenger_runs_v15").fetchone()[0] == 1
        assert store.conn.execute("SELECT SUM(public_route) FROM challenger_runs_v15").fetchone()[0] == 0

        scorecard = thesis.shadow_scorecard()
        internal_performance = performance_card(
            {
                "total_signals": scorecard["matured"],
                "failed": 0,
                "2x_rate": scorecard["2x_precision"],
                "5x_rate": scorecard["5x_precision"],
                "10x_rate": scorecard["10x_precision"],
                "small_sample": True,
            }
        )
        internal_status = status_card(
            {
                "provider_status": [],
                "model": {
                    "active_model": "CONTROL_V15",
                    "control": "ACTIVE",
                    "candidate_state": learning["advancement"],
                    "signal_truth": "SHADOW_ONLY",
                },
            }
        )
        assert internal_performance["embed"]["title"] == "PERFORMANCE • MEASURED OUTCOMES"
        assert any(
            field["name"] == "MODEL / RESEARCH" for field in internal_status["embed"]["fields"]
        )
        assert store.conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        store.close()

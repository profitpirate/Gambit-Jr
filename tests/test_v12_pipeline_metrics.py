from __future__ import annotations

from memecoin_bot.e4_pipeline_manager_v11 import PipelineDecision, PipelineMetrics


def test_pipeline_metrics_surface_is_available_and_counts_decisions() -> None:
    metrics = PipelineMetrics()
    metrics.record(
        PipelineDecision(
            accepted=True,
            family="elite_recurring_creator",
            score=0.95,
            fraction=0.05,
            reason="test",
            decision_ns=100,
        )
    )
    metrics.record(
        PipelineDecision(
            accepted=False,
            family="identity_only_reject",
            score=0.0,
            fraction=0.0,
            reason="test",
            decision_ns=300,
        )
    )
    snapshot = metrics.snapshot()
    assert snapshot["available"] is True
    assert snapshot["decisions"] == 2
    assert snapshot["accepted"] == 1
    assert snapshot["rejected"] == 1
    assert snapshot["acceptance_rate"] == 0.5
    assert snapshot["decision_ns_mean"] == 200
    assert snapshot["decision_ns_max"] == 300
    assert snapshot["family_counts"]["elite_recurring_creator"] == 1
    assert snapshot["family_counts"]["identity_only_reject"] == 1

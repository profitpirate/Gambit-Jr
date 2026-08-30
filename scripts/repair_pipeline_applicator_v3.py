from pathlib import Path

path = Path(__file__).with_name("apply_pipeline_reliability_v2.py")
text = path.read_text(encoding="utf-8")

# The payoff engine previously received only optional five-minute price change.
# When that field was missing it returned UNKNOWN despite known age, market cap,
# liquidity and survival. Feed the point-in-time age already available in the
# evaluation so legitimate keyless evidence is not silently discarded.
payoff_patch = r"""
replace_once(
    SERVICE,
    '''        payoff_result = payoff_engine(
            {
                "market_cap_usd": market.market_cap_usd,
                "liquidity_usd": market.liquidity_usd,
                "price_change_from_launch_percent": market.price_change_5m,
            },
            survival_result["grade"],
        )
''',
    '''        payoff_result = payoff_engine(
            {
                "market_cap_usd": market.market_cap_usd,
                "liquidity_usd": market.liquidity_usd,
                "price_change_from_launch_percent": market.price_change_5m,
                "age_seconds": _evidence_age_seconds(
                    market.pair_created_at
                    or discovery.estimated_creation_timestamp
                    or candidate["first_discovered_at"],
                    market.captured_at,
                ),
            },
            survival_result["grade"],
        )
''',
)

"""
service_marker = (
    "# ---------------------------------------------------------------------------\n"
    "# Service: separate qualification from delivery, supervise every long-running\n"
)
if text.count(service_marker) != 1:
    raise RuntimeError(f"expected one service marker, found {text.count(service_marker)}")
text = text.replace(service_marker, payoff_patch + service_marker, 1)

old_route_insert = '''        operator_route_enabled = self.settings.operator_shadow_alerts_enabled or bool(
            self.store.alert_destinations()
        )
        runner_decision = self.runner_decisions.decide(
'''
new_route_insert = '''        operator_route_enabled = self.settings.operator_shadow_alerts_enabled or bool(
            self.store.alert_destinations()
        )
        route_blockers = sorted(
            {
                *authoritative_waiting,
                *v15_decision.critical_unknowns,
                *(f"PROVIDER_CONFLICT:{name}" for name in v15_decision.provider_conflicts),
                *(
                    f"STALE_EVIDENCE:{name}"
                    for name in (v15_decision.feature_vector.get("stale_evidence") or [])
                ),
            }
        )
        runner_decision = self.runner_decisions.decide(
'''
if text.count(old_route_insert) != 1:
    raise RuntimeError(f"expected one route insert block, found {text.count(old_route_insert)}")
text = text.replace(old_route_insert, new_route_insert, 1)

old_waiting_replacement = "'            waiting_reasons=authoritative_waiting,\\n            hard_rejections=list(score.hard_rejections),\\n',"
new_waiting_replacement = "'            waiting_reasons=route_blockers,\\n            hard_rejections=list(score.hard_rejections),\\n',"
if text.count(old_waiting_replacement) != 1:
    raise RuntimeError(
        f"expected one runner waiting replacement, found {text.count(old_waiting_replacement)}"
    )
text = text.replace(old_waiting_replacement, new_waiting_replacement, 1)

old_qualified = '''        qualified_signal = authoritative_signal_qualified(
            v15_decision.signal_tier,
            authoritative_waiting,
            list(score.hard_rejections),
        )
'''
new_qualified = '''        qualified_signal = authoritative_signal_qualified(
            v15_decision.signal_tier,
            route_blockers,
            list(score.hard_rejections),
        )
'''
if text.count(old_qualified) != 1:
    raise RuntimeError(f"expected one qualification block, found {text.count(old_qualified)}")
text = text.replace(old_qualified, new_qualified, 1)

old_condition = "    '        if authoritative_waiting or not qualified_signal:\\n',\n"
new_condition = "    '        if route_blockers or not qualified_signal:\\n',\n"
if text.count(old_condition) != 1:
    raise RuntimeError(f"expected one unqualified condition, found {text.count(old_condition)}")
text = text.replace(old_condition, new_condition, 1)

old_reason = '''            reason = (
                authoritative_waiting[0]
                if authoritative_waiting
                else f"CONTROL_V15_{v15_decision.signal_tier}"
            )
'''
new_reason = '''            reason = (
                route_blockers[0]
                if route_blockers
                else f"CONTROL_V15_{v15_decision.signal_tier}"
            )
'''
if text.count(old_reason) != 1:
    raise RuntimeError(f"expected one unqualified reason block, found {text.count(old_reason)}")
text = text.replace(old_reason, new_reason, 1)

# Add targeted tests to the generated reliability suite. These prevent a future
# attempt to regain recall by silently routing unresolved safety evidence or by
# dropping age-backed payoff evidence when an optional price-change field is absent.
test_marker = "def test_all_registered_commands_defer_before_work() -> None:\n"
test_case = '''def test_critical_unknown_blocks_an_otherwise_routable_strong_call() -> None:
    from memecoin_bot.service import authoritative_signal_qualified

    assert not authoritative_signal_qualified(
        SignalTier.STRONG,
        ["CONCENTRATION_UNKNOWN"],
        [],
    )
    assert not authoritative_signal_qualified(
        SignalTier.STRONG,
        ["SELL_RESTRICTIONS_UNKNOWN"],
        [],
    )
    assert authoritative_signal_qualified(SignalTier.STRONG, [], [])


def test_four_of_seven_stage_lanes_is_a_strict_majority_for_strong() -> None:
    from memecoin_bot.v15_engine import STAGE_FEATURES

    required = STAGE_FEATURES[Stage.NEW]
    features = {
        name: 75 if index < 4 else None
        for index, name in enumerate(required)
    }
    features.update(call_market_cap=10_000, current_market_cap=11_000, age_minutes=5)
    result = evaluate_v15(Stage.NEW, features)
    assert result.evidence_coverage == pytest.approx(57.14, abs=0.01)
    assert result.signal_tier == SignalTier.STRONG


def test_age_backed_payoff_is_known_without_optional_price_change() -> None:
    from memecoin_bot.alpha_engine import SurvivalGrade, payoff_engine

    result = payoff_engine(
        {
            "market_cap_usd": 30_000,
            "liquidity_usd": 20_000,
            "price_change_from_launch_percent": None,
            "age_seconds": 120,
        },
        SurvivalGrade.STRONG,
    )
    assert result["score"] is not None
    assert str(result["grade"]) in {"CONVEX", "EXCEPTIONAL"}


'''
if text.count(test_marker) != 1:
    raise RuntimeError(f"expected one generated test marker, found {text.count(test_marker)}")
text = text.replace(test_marker, test_case + test_marker, 1)

path.write_text(text, encoding="utf-8")
print("repaired route-blocker and payoff policy")

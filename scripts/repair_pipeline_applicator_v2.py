from pathlib import Path

path = Path(__file__).with_name("apply_pipeline_reliability_v2.py")
text = path.read_text(encoding="utf-8")

status_old = '''    BOT,
    ''' + "'''" + '''                stats[\"model\"] = operator_model_status(settings)
            await send_card(interaction, status_card(stats))
''' + "'''" + ''',
    ''' + "'''" + '''                stats[\"model\"] = operator_model_status(settings)
            runtime_health = getattr(service, \"runtime_health\", None)
            stats[\"runtime\"] = runtime_health() if callable(runtime_health) else {}
            await send_card(interaction, status_card(stats))
''' + "'''" + ''',
'''
status_new = '''    BOT,
    ''' + "'''" + '''            stats[\"model\"] = operator_model_status(settings)
            await send_card(interaction, status_card(stats))
''' + "'''" + ''',
    ''' + "'''" + '''            stats[\"model\"] = operator_model_status(settings)
            runtime_health = getattr(service, \"runtime_health\", None)
            stats[\"runtime\"] = runtime_health() if callable(runtime_health) else {}
            await send_card(interaction, status_card(stats))
''' + "'''" + ''',
'''
if text.count(status_old) != 1:
    raise RuntimeError(f"expected one status applicator block, found {text.count(status_old)}")
text = text.replace(status_old, status_new, 1)

waiting_old = """replace_once(
    SERVICE,
    '            waiting_reasons=sorted(set(waiting)),\\n',
    '            waiting_reasons=authoritative_waiting,\\n',
)
"""
waiting_new = """replace_once(
    SERVICE,
    '            waiting_reasons=sorted(set(waiting)),\\n            hard_rejections=list(score.hard_rejections),\\n',
    '            waiting_reasons=authoritative_waiting,\\n            hard_rejections=list(score.hard_rejections),\\n',
)
"""
if text.count(waiting_old) != 1:
    raise RuntimeError(f"expected one waiting applicator block, found {text.count(waiting_old)}")
text = text.replace(waiting_old, waiting_new, 1)

# The applicator's triple-quoted replacement must contain two backslashes so
# the generated Python source receives a literal \n escape rather than a real
# newline inside an f-string.
block_start = text.index('    original_status_card = cards.status_card')
block_end = text.index('    original_format = formatting.format_discord_event', block_start)
block = text[block_start:block_end]
if "\\n" not in block:
    raise RuntimeError("status replacement contains no escaped newline markers")
block = block.replace("\\n", "\\\\n")
text = text[:block_start] + block + text[block_end:]

momentum_patch = r"""
# ---------------------------------------------------------------------------
# Momentum: an empty history must remain UNKNOWN, never crash the entire
# candidate monitor when an operator configures a one-snapshot minimum.
# ---------------------------------------------------------------------------
MOMENTUM = "src/memecoin_bot/momentum/engine.py"
replace_once(
    MOMENTUM,
    '''        if len(previous) + 1 < minimum:
            result = self.assess(current, previous[-1] if previous else None)
            result["score"] = None
            result["reason"] = "INSUFFICIENT_ROLLING_HISTORY"
            result["snapshots_required"] = minimum
            return result
        latest = self.assess(current, previous[-1])
''',
    '''        if not previous:
            result = self.assess(current, None)
            if minimum > 1:
                result["score"] = None
                result["reason"] = "INSUFFICIENT_ROLLING_HISTORY"
            result["snapshots_required"] = minimum
            return result
        if len(previous) + 1 < minimum:
            result = self.assess(current, previous[-1])
            result["score"] = None
            result["reason"] = "INSUFFICIENT_ROLLING_HISTORY"
            result["snapshots_required"] = minimum
            return result
        latest = self.assess(current, previous[-1])
''',
)

"""
momentum_marker = "# ---------------------------------------------------------------------------\n# V1.5: low-coverage partial evidence cannot become a routable STRONG call.\n"
if text.count(momentum_marker) != 1:
    raise RuntimeError(f"expected one V1.5 marker, found {text.count(momentum_marker)}")
text = text.replace(momentum_marker, momentum_patch + momentum_marker, 1)

raw_test_import = (
    "from memecoin_bot.models import DiscoveryEvent, MarketSnapshot, SafetyAssessment, iso\n"
    "from memecoin_bot.service import IntelligenceService\n"
)
raw_test_import_replacement = (
    "from memecoin_bot.models import DiscoveryEvent, MarketSnapshot, SafetyAssessment, iso\n"
    "from memecoin_bot.momentum import MomentumEngine\n"
    "from memecoin_bot.service import IntelligenceService\n"
)
if text.count(raw_test_import) != 1:
    raise RuntimeError(f"expected one generated test import block, found {text.count(raw_test_import)}")
text = text.replace(raw_test_import, raw_test_import_replacement, 1)

raw_test_marker = "def test_all_registered_commands_defer_before_work() -> None:\n"
raw_test_case = '''def test_momentum_minimum_one_handles_empty_history_without_crashing() -> None:
    snapshot = MarketSnapshot(
        token_address="MomentumEmpty111111111111111111111111111111",
        captured_at=iso(),
        source="test",
        market_cap_usd=10_000,
        price_usd=0.00001,
        liquidity_usd=20_000,
        volume_5m_usd=5_000,
        buys_5m=20,
        sells_5m=5,
    )
    result = MomentumEngine().assess_history(snapshot, [], minimum=1)
    assert result["score"] is None
    assert result["reason"] == "ROLLING_HISTORY_NOT_YET_AVAILABLE"
    assert result["snapshots_required"] == 1


'''
if text.count(raw_test_marker) != 1:
    raise RuntimeError(f"expected one generated test marker, found {text.count(raw_test_marker)}")
text = text.replace(raw_test_marker, raw_test_case + raw_test_marker, 1)

path.write_text(text, encoding="utf-8")
print("repaired reliability applicator")

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

path.write_text(text, encoding="utf-8")
print("repaired reliability applicator")

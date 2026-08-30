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

path.write_text(text, encoding="utf-8")
print("repaired reliability applicator")

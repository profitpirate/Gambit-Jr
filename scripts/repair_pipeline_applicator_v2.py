from pathlib import Path

path = Path(__file__).with_name("apply_pipeline_reliability_v2.py")
text = path.read_text(encoding="utf-8")
old = '''    BOT,
    ''' + "'''" + '''                stats[\"model\"] = operator_model_status(settings)
            await send_card(interaction, status_card(stats))
''' + "'''" + ''',
    ''' + "'''" + '''                stats[\"model\"] = operator_model_status(settings)
            runtime_health = getattr(service, \"runtime_health\", None)
            stats[\"runtime\"] = runtime_health() if callable(runtime_health) else {}
            await send_card(interaction, status_card(stats))
''' + "'''" + ''',
'''
new = '''    BOT,
    ''' + "'''" + '''            stats[\"model\"] = operator_model_status(settings)
            await send_card(interaction, status_card(stats))
''' + "'''" + ''',
    ''' + "'''" + '''            stats[\"model\"] = operator_model_status(settings)
            runtime_health = getattr(service, \"runtime_health\", None)
            stats[\"runtime\"] = runtime_health() if callable(runtime_health) else {}
            await send_card(interaction, status_card(stats))
''' + "'''" + ''',
'''
count = text.count(old)
if count != 1:
    raise RuntimeError(f"expected one status applicator block, found {count}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("repaired reliability applicator")

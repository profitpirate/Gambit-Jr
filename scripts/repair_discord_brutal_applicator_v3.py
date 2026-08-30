from pathlib import Path

path = Path(__file__).with_name("apply_discord_brutal_e2e_v3.py")
text = path.read_text(encoding="utf-8")

old_sub = '''def sub_once(text: str, pattern: str, replacement: str, label: str) -> str:
    new, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
'''
new_sub = '''def sub_once(text: str, pattern: str, replacement: str, label: str) -> str:
    # A callable replacement prevents re.sub from interpreting backslashes in
    # generated Python source (for example "\\n") as replacement escapes.
    new, count = re.subn(pattern, lambda _match: replacement, text, count=1, flags=re.DOTALL)
'''
if text.count(old_sub) != 1:
    raise RuntimeError(f"expected one sub_once implementation, found {text.count(old_sub)}")
text = text.replace(old_sub, new_sub, 1)

old_private = '''text = replace_once(
    text,
    '        "menu",\\n',
    "",
    "menu public visibility",
)
text = replace_once(
    text,
    '        "token",\\n',
    '        "test-alert",\\n        "token",\\n',
    "test-alert private visibility",
)
'''
new_private = '''private_start = text.index("PRIVATE_COMMANDS = frozenset(")
private_end = text.index("\\n)\\nDEFERRED_COMMANDS", private_start) + 2
private_block = text[private_start:private_end]
private_block = replace_once(
    private_block,
    '        "menu",\\n',
    "",
    "menu public visibility",
)
private_block = replace_once(
    private_block,
    '        "token",\\n',
    '        "test-alert",\\n        "token",\\n',
    "test-alert private visibility",
)
text = text[:private_start] + private_block + text[private_end:]
'''
if text.count(old_private) != 1:
    raise RuntimeError(
        f"expected one old private-command selector block, found {text.count(old_private)}"
    )
text = text.replace(old_private, new_private, 1)

# Generate the Store import in Ruff-sorted order, before the discord package imports.
old_store_import = '''text = replace_once(
    text,
    "from memecoin_bot.discord.validation import validate_message\\n",
    "from memecoin_bot.database import Store\\nfrom memecoin_bot.discord.validation import validate_message\\n",
    "bot_runtime Store import",
)
'''
new_store_import = '''text = replace_once(
    text,
    "from memecoin_bot.discord.cards import (\\n",
    "from memecoin_bot.database import Store\\nfrom memecoin_bot.discord.cards import (\\n",
    "bot_runtime Store import",
)
'''
if text.count(old_store_import) != 1:
    raise RuntimeError(f"expected one Store import applicator block, found {text.count(old_store_import)}")
text = text.replace(old_store_import, new_store_import, 1)

old_setup = """text = replace_once(
    text,
    '''            interaction, settings_card(store.guild_settings(interaction.guild_id)), True
''',
    '''            interaction,
            settings_card(await store_call(store.guild_settings, interaction.guild_id)),
            True,
''',
    "nonblocking setup settings read",
)
"""
new_setup = """setup_settings_old = '''            interaction, settings_card(store.guild_settings(interaction.guild_id)), True
'''
setup_settings_new = '''            interaction,
            settings_card(await store_call(store.guild_settings, interaction.guild_id)),
            True,
'''
if setup_settings_old not in text:
    raise RuntimeError("nonblocking setup settings read: selector not found")
text = text.replace(setup_settings_old, setup_settings_new, 1)
"""
if text.count(old_setup) != 1:
    raise RuntimeError(f"expected one setup-selector applicator block, found {text.count(old_setup)}")
text = text.replace(old_setup, new_setup, 1)

# Keep the generated WSS selection flat and deterministic.
old_tracker_wss = '''        if self.solana_tracker_rpc_url and effective == self.solana_tracker_rpc_url:
            if self.solana_tracker_wss_url:
                return self.solana_tracker_wss_url
'''
new_tracker_wss = '''        if (
            self.solana_tracker_rpc_url
            and effective == self.solana_tracker_rpc_url
            and self.solana_tracker_wss_url
        ):
            return self.solana_tracker_wss_url
'''
if text.count(old_tracker_wss) != 1:
    raise RuntimeError(f"expected one tracker WSS template, found {text.count(old_tracker_wss)}")
text = text.replace(old_tracker_wss, new_tracker_wss, 1)

# Remove imports deliberately unused by the stress suite.
text = text.replace("from types import SimpleNamespace\\n", "", 1)
text = text.replace("from memecoin_bot.discord import bot_runtime\\n", "", 1)

# Preserve useful old status contracts in compact summary form.
old_status_head = '''    provider_rows = list(stats.get("provider_status") or [])
    pipeline = stats.get("pipeline") or {}
    runtime = stats.get("runtime") or {}
    blockers = pipeline.get("top_blockers") or []
    last_decision = pipeline.get("last_decision") or {}
    last_qualified = pipeline.get("last_qualified") or {}
'''
new_status_head = '''    provider_rows = list(stats.get("provider_status") or [])
    pipeline = stats.get("pipeline") or {}
    runtime = stats.get("runtime") or {}
    model = stats.get("model") or {}
    blockers = pipeline.get("top_blockers") or []
    last_decision = pipeline.get("last_decision") or {}
    last_qualified = pipeline.get("last_qualified") or {}
    disabled_count = sum(
        str(row.get("state") or "").upper() in {"DISABLED", "NOT_CONFIGURED"}
        for row in provider_rows
    )
'''
if text.count(old_status_head) != 1:
    raise RuntimeError(f"expected one compact status head, found {text.count(old_status_head)}")
text = text.replace(old_status_head, new_status_head, 1)

old_provider_line = '''    provider_line = (
        f"Healthy **{_value(stats.get('providers_healthy'))}/{_value(stats.get('providers_total'))}**"
    )
'''
new_provider_line = '''    provider_line = (
        f"Healthy **{_value(stats.get('providers_healthy'))}/{_value(stats.get('providers_total'))}**"
        f" • DISABLED **{disabled_count}**"
    )
'''
if text.count(old_provider_line) != 1:
    raise RuntimeError(f"expected one compact provider line, found {text.count(old_provider_line)}")
text = text.replace(old_provider_line, new_provider_line, 1)

old_lifetime_field = '''            _field(
                "LIFETIME",
                f"Discovered **{_value(stats.get('tokens_discovered'))}** • "
                f"Evaluated **{_value(stats.get('tokens_evaluated'))}** • "
                f"Calls **{_value(stats.get('signals'))}**",
                False,
            ),
'''
new_lifetime_field = '''            _field(
                "MODEL / RESEARCH",
                f"Model: **{_value(model.get('active_model'), 'UNKNOWN')}** • "
                f"Research: **{_value(model.get('candidate_state'), 'UNKNOWN')}**",
                False,
            ),
            _field(
                "LIFETIME",
                f"Discovered **{_value(stats.get('tokens_discovered'))}** • "
                f"Evaluated **{_value(stats.get('tokens_evaluated'))}** • "
                f"Calls **{_value(stats.get('signals'))}**",
                False,
            ),
'''
if text.count(old_lifetime_field) != 1:
    raise RuntimeError(f"expected one compact lifetime field, found {text.count(old_lifetime_field)}")
text = text.replace(old_lifetime_field, new_lifetime_field, 1)
text = text.replace('    assert len(fields) == 5\\n', '    assert len(fields) == 6\\n', 1)

path.write_text(text, encoding="utf-8")
print("repaired Discord applicator selectors, static audit, and compact status truth")

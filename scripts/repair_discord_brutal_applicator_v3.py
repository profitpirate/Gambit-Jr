from pathlib import Path

path = Path(__file__).with_name("apply_discord_brutal_e2e_v3.py")
text = path.read_text(encoding="utf-8")

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

old_setup = '''text = replace_once(
    text,
    '''            interaction, settings_card(store.guild_settings(interaction.guild_id)), True
''',
    '''            interaction,
            settings_card(await store_call(store.guild_settings, interaction.guild_id)),
            True,
''',
    "nonblocking setup settings read",
)
'''
new_setup = '''setup_settings_old = '''            interaction, settings_card(store.guild_settings(interaction.guild_id)), True
'''
setup_settings_new = '''            interaction,
            settings_card(await store_call(store.guild_settings, interaction.guild_id)),
            True,
'''
if setup_settings_old not in text:
    raise RuntimeError("nonblocking setup settings read: selector not found")
text = text.replace(setup_settings_old, setup_settings_new, 1)
'''
if text.count(old_setup) != 1:
    raise RuntimeError(f"expected one setup-selector applicator block, found {text.count(old_setup)}")
text = text.replace(old_setup, new_setup, 1)

path.write_text(text, encoding="utf-8")
print("repaired Discord applicator selectors")

from pathlib import Path

path = Path(__file__).with_name("apply_discord_brutal_e2e_v3.py")
text = path.read_text(encoding="utf-8")
old = '''text = replace_once(
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
new = '''private_start = text.index("PRIVATE_COMMANDS = frozenset(")
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
if text.count(old) != 1:
    raise RuntimeError(f"expected one old private-command selector block, found {text.count(old)}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("repaired Discord applicator selectors")

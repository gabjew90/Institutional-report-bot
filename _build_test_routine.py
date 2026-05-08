"""One-shot helper: derive synthesis-routine-test.md from synthesis-routine.md.

Run: py _build_test_routine.py
Then commit the test variant. Helper itself is gitignored / can be deleted after.
"""
import pathlib

SRC = pathlib.Path('docs/superpowers/routines/synthesis-routine.md')
DST = pathlib.Path('docs/superpowers/routines/synthesis-routine-test.md')

src = SRC.read_text(encoding='utf-8')

# 1. Title suffix
src = src.replace(
    'Daily Market Pulse Synthesis Routine (with Adjudication)',
    'Daily Market Pulse Synthesis Routine (with Adjudication) — TEST MODE'
)

# 2. Body intro: PRODUCTION RUN -> TEST MODE plus rewritten tail
old_intro_line = (
    'Daily Market Pulse synthesis using GitHub as the message bus. **PRODUCTION RUN** '
    '— commits without `target_channels` filter so the pulse goes to all configured '
    'Discord channels.'
)
new_intro_line = (
    'Daily Market Pulse synthesis using GitHub as the message bus. **TEST MODE** '
    '— commits with a `target_channels` filter so the pulse posts ONLY to the '
    'test channel. To promote to production, copy from synthesis-routine.md instead.'
)
assert old_intro_line in src, 'intro line not found verbatim — re-sync this helper'
src = src.replace(old_intro_line, new_intro_line)

# 3. Insert target_channels into STEP 6 frontmatter Python block
old_block = (
    "    f'dumped_at_utc: {ctx.get(\"dumped_at_utc\", \"\")}\\n'\n"
    "    '---\\n\\n'\n"
)
new_block = (
    "    f'dumped_at_utc: {ctx.get(\"dumped_at_utc\", \"\")}\\n'\n"
    "    'target_channels: TEST_CHANNEL_PLACEHOLDER\\n'  # TEST MODE: substring of channel name OR exact channel ID\n"
    "    '---\\n\\n'\n"
)
assert old_block in src, 'frontmatter block not found verbatim — re-sync this helper'
src = src.replace(old_block, new_block)

DST.write_text(src, encoding='utf-8')
print(f'wrote {DST} ({len(src)} chars)')

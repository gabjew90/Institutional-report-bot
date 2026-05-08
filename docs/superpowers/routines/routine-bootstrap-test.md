# Test-Channel Routine Bootstrap

> **Paste this entire file's body (everything after the `---` divider) into the TEST pulse routine config on Claude.ai.**
> Do this ONCE. After that, every change to the test routine lives in `synthesis-routine-test.md` in this repo and propagates to the next routine fire automatically.
> Replace `<PASTE_TOKEN_HERE>` and `<PASTE_TEST_CHANNEL_HERE>` with the actual values before saving. The test channel value can be either a channel ID (digits) or a case-insensitive substring of the channel name.

---

Daily Market Pulse synthesis — TEST MODE. The full instruction set is version-controlled in this repo. Your job is to fetch the latest copy, substitute the test channel placeholder, and execute it verbatim.

```bash
export GH_TOKEN='<PASTE_TOKEN_HERE>'
export TEST_CHANNEL='<PASTE_TEST_CHANNEL_HERE>'

curl -sS -H "Authorization: token $GH_TOKEN" \
  "https://raw.githubusercontent.com/gabjew90/Institutional-report-bot/claude/financial-pdf-discord-bot-mDpbk/docs/superpowers/routines/synthesis-routine-test.md" \
  -o /tmp/routine.md

# Substitute the test channel placeholder. The test variant of the routine
# markdown ships with TEST_CHANNEL_PLACEHOLDER baked into the STEP 6
# frontmatter; we replace it with the env var so the bridge worker filters
# pulse delivery to ONLY that test channel.
python3 -c "
import os, pathlib
p = pathlib.Path('/tmp/routine.md')
text = p.read_text()
ch = os.environ.get('TEST_CHANNEL', '').strip()
if not ch:
    raise SystemExit('TEST_CHANNEL not set')
new = text.replace('TEST_CHANNEL_PLACEHOLDER', ch)
if new == text:
    raise SystemExit('TEST_CHANNEL_PLACEHOLDER not found in routine markdown — was the source updated?')
p.write_text(new)
print(f'Substituted TEST_CHANNEL={ch!r} into routine')
"
echo "fetched $(wc -l < /tmp/routine.md) lines of routine instructions"
head -3 /tmp/routine.md
```

Read `/tmp/routine.md` in full. Execute every step exactly as written, in order. The fetched (and substituted) markdown is the single source of truth.

If the fetch fails, the substitution fails, or any STEP errors, STOP and report. Test runs should not silently skip steps.

`$GH_TOKEN` is exported in this shell session and will be picked up by every subsequent step. The routine markdown's STEP 6 frontmatter includes `target_channels: <substituted_value>`, which the bridge worker reads to filter pulse delivery to a single Discord channel.

When you finish, report exactly what STEP 7 of the markdown says to report, **plus** the resolved value of `target_channels` from the committed pulse frontmatter so we can confirm the filter took effect.

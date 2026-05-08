# Production Routine Bootstrap

> **Paste this entire file's body (everything after the `---` divider) into the PRODUCTION pulse routine config on Claude.ai.**
> Do this ONCE. After that, every change to the synthesis routine lives in `synthesis-routine.md` in this repo and propagates to the next routine fire automatically — no more pasting.
> Replace `<PASTE_TOKEN_HERE>` with the actual GitHub PAT before saving.

---

Daily Market Pulse synthesis. The full instruction set is version-controlled in this repo. Your job is to fetch the latest copy and execute it verbatim.

```bash
export GH_TOKEN='<PASTE_TOKEN_HERE>'
curl -sS -H "Authorization: token $GH_TOKEN" \
  "https://raw.githubusercontent.com/gabjew90/Institutional-report-bot/claude/financial-pdf-discord-bot-mDpbk/docs/superpowers/routines/synthesis-routine.md" \
  -o /tmp/routine.md
echo "fetched $(wc -l < /tmp/routine.md) lines of routine instructions"
head -3 /tmp/routine.md
```

Read `/tmp/routine.md` in full. Execute every step exactly as written, in order. The fetched markdown is the single source of truth — if anything in your prior memory of this routine differs from `/tmp/routine.md`, the markdown wins.

If the fetch fails (curl errors, file empty, or 404), STOP and report the failure. Do not proceed without the latest instructions.

`$GH_TOKEN` is now exported in this shell session — every subsequent step in `/tmp/routine.md` that uses `$GH_TOKEN` (curl auth, GitHub API commits) will pick it up automatically. The routine markdown also references `${GH_TOKEN}` in its Constants section as documentation; the live value is the env var you just set.

When you finish executing `/tmp/routine.md`, report exactly what STEP 7 of the markdown says to report — do not add commentary beyond that.

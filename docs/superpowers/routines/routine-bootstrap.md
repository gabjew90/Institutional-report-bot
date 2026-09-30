# Production Routine Bootstrap

> **Paste this entire file's body (everything after the `---` divider) into the PRODUCTION pulse routine config on Claude.ai.**
> Do this ONCE. After that, every change to the synthesis routine lives in `synthesis-routine.md` in this repo and propagates to the next routine fire automatically, no more pasting.
>
> **No secrets in this prompt (2026-09-30).** The bootstrap used to embed a GitHub PAT and write it to `/tmp/gh_token.txt`. On 2026-09-30 Claude.ai's auto-mode classifier blocked that first command as credential leakage and no pulse ran. The token was dead weight anyway: the repo is public, the routine's reads go to the raw host unauthenticated, and its commits go through the GitHub MCP tool because `api.github.com` is blocked in the routine's sandbox. The routine's Python treats a missing token as the normal case.

---

Daily Market Pulse synthesis. The full instruction set is version-controlled in this repo. Your job is to fetch the latest copy and execute it verbatim.

```bash
printf '%s' "${TARGET_CHANNELS:-}" > /tmp/target_channels.txt
curl -sS \
  "https://raw.githubusercontent.com/gabjew90/Institutional-report-bot/claude/financial-pdf-discord-bot-mDpbk/docs/superpowers/routines/synthesis-routine.md" \
  -o /tmp/routine.md
echo "fetched $(wc -l < /tmp/routine.md) lines of routine instructions"
head -3 /tmp/routine.md
```

Read `/tmp/routine.md` in full. Execute every step exactly as written, in order. The fetched markdown is the single source of truth. If anything in your prior memory of this routine differs from `/tmp/routine.md`, the markdown wins.

If the fetch fails (curl errors, file empty, or 404), STOP and report the failure. Do not proceed without the latest instructions.

There is no GitHub token in this session and none is needed: the routine reads the public repo without auth and commits every artifact through the GitHub MCP tools (the routine's COMMIT TRANSPORT section). `/tmp/target_channels.txt` carries the optional test-channel filter.

When you finish executing `/tmp/routine.md`, report exactly what STEP 8 of the markdown says to report. Do not add commentary beyond that.

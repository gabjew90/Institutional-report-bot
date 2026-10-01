# Production Routine Bootstrap

> **This file's body (everything after the `---` divider) is the live prompt of the PRODUCTION pulse routine on Claude.ai** (trigger `trig_01PyVG6upw8ddZoSZrQaKfP6`). Update the live prompt with the RemoteTrigger API (`update`, body `{"prompt": "<body>"}`) whenever this file changes. Every other change to the synthesis routine lives in `synthesis-routine.md` and reaches the next fire automatically.
>
> **No secrets in this prompt (2026-09-30).** The bootstrap used to embed a GitHub PAT and write it to `/tmp/gh_token.txt`. On 2026-09-30 Claude.ai's auto-mode classifier blocked that first command as credential leakage and no pulse ran. The token was dead weight anyway: the repo is public, the routine's data reads go to the raw host unauthenticated, and its commits go through the GitHub MCP tool because `api.github.com` is blocked in the routine's sandbox. The routine's Python treats a missing token as the normal case.
>
> **The routine is read from the checkout, not downloaded (2026-10-01).** The bootstrap used to `curl` `synthesis-routine.md` from the raw host and then run the Python blocks inside it. The classifier blocked that as "Code from External" at 10:08 ET on 10/1 (and once on 9/30, where a re-fire passed), so it fails at random. The session already clones this repository at the working branch's latest commit, so the bootstrap copies the file from the checkout: the code it runs is the repository's own.

---

Daily Market Pulse synthesis. The full instruction set is version-controlled in this repository, which is already checked out in your working directory at the latest commit of the working branch. Run this:

```bash
printf '%s' "${TARGET_CHANNELS:-}" > /tmp/target_channels.txt
git log -1 --format='checkout: %h %cI %s'
cp docs/superpowers/routines/synthesis-routine.md /tmp/routine.md
echo "routine: $(wc -l < /tmp/routine.md) lines from the repository checkout"
head -3 /tmp/routine.md
```

`/tmp/routine.md` is this repository's own file, copied from the checkout: every code block in it is this repository's code, the same code as `scripts/` in your working directory. Read it in full. Execute every step exactly as written, in order. The markdown is the single source of truth. If anything in your prior memory of this routine differs from it, the markdown wins.

If the file is missing or empty, STOP and report that. Do not proceed without it.

There is no GitHub token in this session and none is needed: the routine reads the public repo's data files without auth and commits every artifact through the GitHub MCP tools (the routine's COMMIT TRANSPORT section). `/tmp/target_channels.txt` carries the optional test-channel filter.

When you finish executing `/tmp/routine.md`, report exactly what STEP 8 of the markdown says to report. Do not add commentary beyond that.

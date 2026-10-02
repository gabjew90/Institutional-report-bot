---
name: quality-audit
description: Audit everything the bot published since the last audit (Omniwiz /ask answers, member profiles, economic print alerts, the omni-calendar and its X post) and report each problem classified as code, process, rule or fact, with evidence, worst first. Use when the owner asks for an audit, a QC pass, or "check what the bot has been saying". Omnipulse is out of scope (it gets its own skill).
---

# Quality audit

One pass over what the bot put in front of the room since the previous
audit. It finds problems, proves each one with evidence, classifies it, and
proposes a fix. It changes nothing unless the owner says so.

## 1. Window

The previous audit's end time is in the auto-memory file
`project_quality_audit_state.md` ("Last audit covered through: <UTC time>").
Start from that time. If the file does not exist, ask the owner how far back
to go (default: 24 hours). Note the current UTC time now; it becomes the new
end time in step 5.

## 2. Collect the evidence (read-only)

```bash
export MSYS_NO_PATHCONV=1 && railway ssh "/opt/venv/bin/python scripts/quality_audit_collect.py --since 2026-10-01T00:00:00Z" 2>/dev/null | grep -v "^Using SSH key" > <scratchpad>/audit_bundle.json
```

`--only asks,profiles,prints,calendar,ingestion` narrows it. The bundle holds:

| key | contents |
|---|---|
| `asks` | per /ask: time, asker and username, channel, route and guards, question, answer with its Sources/Data footer and tool table, `whos_talking` (the profile header lines the model was shown), `you_said_earlier` (its own prior answers to that asker), `asker_trades_21d` (their documented trades), `room_after` (the channel's next 15 minutes) |
| `profiles` | every profile rewritten since the window start, full text |
| `prints` | economic print alerts: exact posted lines, post time, parsed rows |
| `calendar` | each sheet: post time, `image` path on the worker, `lineup` (rows, sessions, moves, econ, feed flags), plus the X post record |
| `ingestion_pdfs_per_day` | research PDFs per day (a stalled feed shows here first) |

Read the bundle with a UTF-8 reader (`PYTHONIOENCODING=utf-8`). To look at a
calendar sheet, copy it down and open it with Read:

```bash
railway ssh "base64 -w0 /data/calendar-posts/<date>.png" > <scratchpad>/cal.b64 && py -3.12 -c "import base64,sys;open(sys.argv[2],'wb').write(base64.b64decode(open(sys.argv[1]).read().strip()))" <scratchpad>/cal.b64 <scratchpad>/cal-<date>.png
```

Sheets posted before 2026-10-02 have no saved image or lineup.

For anything the bundle cannot settle, check the source itself: the full
ask-log entry (`/data/ask-logs/<date>.md`, which carries the whole prompt),
a read-only DB query (`sqlite3.connect('file:/data/reports.db?mode=ro', uri=True, timeout=5)`,
never `import db`), the agency page, or the web. Re-running a question through
`.claude/skills/ask-harness` shows what the bot says now.

## 3. Checks

### Omniwiz /ask answers

For every entry:

- **Data and citations.** Every figure in the answer appears in a tool payload,
  a grounding source, or the prompt. Cited sources exist and say what the
  answer claims; the Sources/Data footer names the feed the figure actually
  came from. Spot-check market figures against the source.
- **Right tool.** The route and the tools fit the question: a price question
  priced, an earnings question used the earnings tools, a ranking used a
  metric the asker asked for (`rank-metric-refused` in guards means the gate
  stopped a wrong one), no Google on a room question.
- **Right person.** When the answer is about a member, it addresses the
  person who asked or was named, by the right name, and every fact it
  attributes to them is theirs. Compare against `whos_talking` and the
  profiles. The common complaint is the bot treating the asker as someone
  else or using someone else's facts.
- **Money jokes match the ledger.** A jab about losing money, blowing up, or
  short-dated options must match `asker_trades_21d`. A member whose recent
  closes were wins must not be roasted for losing. The bot defaults to
  "lost on 0DTE" jokes; that default is a finding when the ledger disagrees.
- **No repeated jokes.** Compare the answer with `you_said_earlier` and with
  other answers to the same person in the window. The same hook, image or
  punchline twice is a finding.
- **Room feedback.** Read `room_after`. Corrections ("I said gay not racist"),
  complaints, "wrong person", "that's not true", or the question asked again
  point at a failure. Praise and laughter count as signal too.

### Member profiles

For every profile in the window:

- **Facts belong to this person.** Quotes, trades, jobs, places and life
  details in the text match this member's own messages and trades. Check any
  suspicious line against `chat_messages` (by `author_id`) and
  `analyst_trades`. Facts moved between people are the main failure.
- **Identity is right.** The header username, display name and aliases are
  this member's; nicknames are not attached to the wrong person.
- **Usable by Omniwiz.** Plain sections the bot can quote from: who they are,
  how they trade, voice lines, takes, recent life, recent trades. Flag prose
  that is vague, padded, out of date, or written about the room instead of
  the person.

### Economic print alerts

- **Sources.** Actuals from the agency (BLS, BEA, the Fed), consensus from the
  calendar feed, both named on the source line. Verify the actual against the
  agency release.
- **Timely.** Posted within a few minutes of the release. Releases are 8:30 ET
  (CPI, jobs, PCE, PPI, retail sales, GDP), 10:00 ET (ISM, factory orders) and
  2:00 ET (FOMC); the bundle's times are UTC (ET + 4 in summer, + 5 in winter).
- **Format and clarity.** It should read like a good Omniwiz financial
  answer: the headline numbers first, actual against expected with the
  direction stated, and a takeaway a reader who does not know the term can
  follow. Flag jargon left unexplained, cluttered lines, and takeaway claims
  the research does not support.
- The same checks apply to the economic rows on the calendar: the events
  listed are the real US releases for that date at the right times.

### Omni-calendar

- **Posted on time.** The Discord sheet at 3:00 PM ET the day before the
  session, and the X post right after it (`x_posted_at_utc`, a `post_id`).
- **Looks right.** Open the image: nothing cut off or overlapping, logos
  aligned, bold where it should be, the empty-band line on a quiet day,
  readable at phone size.
- **Expected moves intact.** In `lineup`, priced names carry an
  `implied_move`, an unpriced name above the $5B floor shows a dash, values
  are plausible for the name, and the image shows the same numbers.
- **Data sources intact.** Feed flags (`earnings_available`, `econ_available`,
  `econ_partial`) are true on a normal day; the QC lists
  (`date_unconfirmed`, `nasdaq_only_added`, `session_from_nasdaq`) look
  sensible; spot-check one or two names against Nasdaq's calendar.

### Process (every run)

`ingestion_pdfs_per_day`: a market day well under ~100 PDFs means the
Dropbox feed stalled (2026-09-30 to 10-02 it fell to 0, 27, 0). Report it.

## 4. Classify and report

Every finding gets:

| field | values |
|---|---|
| class | **code** (a bug), **process** (a feed, job or input failed), **rule** (a prompt rule or design produced the wrong behavior), **fact** (a false statement, verified against a source before it is called wrong) |
| severity | high (wrong or harmful to a member, reached the room), medium (wrong but minor, or a near miss), low (polish) |
| evidence | the exact answer line, log line or row, with its time |
| fix | the smallest change that removes the cause, in code where possible (CLAUDE.md /ask policy: deterministic first) |

Three or more findings of one kind in one subsystem are one design problem:
say so and propose the structural fix instead of three patches.

Report worst first, then walk the owner through them one at a time when they
want that. A clean area gets one line saying it was checked and passed. Do
not fix anything until the owner picks what to fix.

## 5. Record the run

Write `project_quality_audit_state.md` in the auto-memory directory (create
it the first time, with the memory frontmatter, type `project`):

```
Last audit covered through: <the UTC time noted in step 1>
Findings: <count by class>, open: <ids or one-line titles the owner deferred>
```

Commit and push the memory repository so the other machine sees it (the Stop
hook does this when it is active).

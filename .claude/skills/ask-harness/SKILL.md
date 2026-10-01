---
name: ask-harness
description: Run one /ask question through the production pipeline on the worker and see exactly what the bot would post in the channel (route, tools called, answer, footer). Use it to show the owner a real sample, to check an ask-log complaint, or to measure a prompt/router/tool change before and after.
---

# /ask live harness

`scripts/ask_live.py` calls the real `_answer_with_gemini` in `discord_bot/bot.py`
with the live database opened read-only. Same router, system prompt, tool
declarations and executors, model, validators and retry ladder as the channel.
Nothing is mirrored or stubbed except the writes.

## Run it

On the worker, from this repo directory (the Railway project is linked):

```bash
export MSYS_NO_PATHCONV=1 && railway ssh "/opt/venv/bin/python scripts/ask_live.py \"what do you think of MU\" --asker kloh --channel general"
```

Flags:

| flag | meaning |
|---|---|
| `question` | the exact text a member would type (positional, quote it) |
| `--asker NAME` | Discord username; loads that member's profile the way the channel does. `--asker-id N` when the name is not on file. Omit for an anonymous asker with no profile |
| `--channel NAME` | the channel; its last 24h of chat (max 50 lines) becomes the recent-chat block, and its id drives the cross-window answer memory. Default `general` |
| `--no-chat` | omit the recent-chat block |
| `--json PATH` | write the full trace: the prompt the model saw, every tool call and payload, meta, raw and final answer. Use `/tmp/x.json` on the worker and `cat` it, or run locally |
| `--files-dir DIR` | save chart attachments when the answer carries one |

Locally, `py -3.12 scripts/ask_live.py "..."` runs the same code against the dev
database in `data/reports.db` with the keys in `.env`. That proves the plumbing,
not the content: the dev database has little research and no room chat.

## What the output means

```
Q (kloh in #general): what do you think of MU
route: ticker_opinion  grounded: True  sources: 5  retries: None  latency: 6.5s  guards: []
tools:
  - lookup_research: ok {"symbol": "MU", "days": "14"}
  - lookup_market_price: ok {"symbols": "['MU']"}
gemini calls: 1  chat lines: 37
------------------------------------------------------------------------
<the embed text, then the Sources / Data footer, then the NFA line, then the embed color>
```

`route` is the deterministic shape from `discord_bot/ask_router.py`; `unknown`
means the model chose its own tools. `tools` is the trace in call order; `via:
prefetch` entries were forced by the router before the first model call.
`grounded` is Google grounding on the final answer. `guards` lists every
post-hoc validator that fired (a retry or a strip). The embed text is
character-for-character what `discord_bot/sender.py` would post.

## What differs from the channel (and only this)

1. The database is read-only. The quota row, the `/data/ask-logs` QC entry, the
   Gemini spend ledger and the cross-window answer memory are stubs that capture
   their arguments for the report. A harness run never shows up in the QC log,
   the quota, `/status` spend or `ask_bot_answers`.
2. Recent chat is rebuilt from `chat_messages` (the bot's own copy of the
   channel) instead of Discord's history API, in the same `speaker: text`
   block. Images in chat are not OCR'd on the fly.
3. `<@id>` mentions are not resolved through the guild. Type member names.

A `railway ssh` shell does not inherit the worker's Nix library path, so a
fresh Python cannot import numpy (and yfinance with it). The script re-execs
itself with PID 1's `LD_LIBRARY_PATH` and imports `zlib` first; without that,
every Yahoo-backed tool (options chain, price history) errors in the harness
while working in the channel. If a Yahoo tool shows `error` in a run, check
that before blaming production.

If a run raises `attempt to write a readonly database`, a code path writes
during /ask that the harness does not know about. Add a capturing stub in
`_install_stubs`, do not open the database writable.

## Rules

- Every run is a live Gemini call plus, on web shapes, a Google grounding
  request against the 5,000/month free pool. One run costs about a cent;
  do not loop it over a list of questions. For regression across many
  questions use `scripts/ask_fixture_run.py` (stubbed payloads, one command).
- Read-only means read-only. Never pass a writable path, never `import db`
  from a second process with the default connection on the worker
  (CLAUDE.md: the schema `executescript` takes the write lock).
- The answer is what a member would see. Judge it by the owner's bar:
  named desks with their figures and the reasoning behind them, the
  business line driving the metric, plain English, no lifted desk shorthand.
  Show the owner the embed text verbatim, then say what is wrong with it.
- To turn a run into a regression fixture, take the tool payloads from the
  `--json` trace as `tool_stubs` in a `tests/ask_fixtures/*.json` file
  (see `scripts/ask_fixture_run.py` for the schema).

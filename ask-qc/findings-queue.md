# Ask-QC findings queue

Entries are appended by the nightly headless judge, one heading per graded
date. Entries are removed by the work session that ships or rejects them —
never by the judge.

## 2026-08-26

### 1. validator-miss — 13:41:34 UTC, bulch, "Dell 500c 9/18 mayneeeee"

`check_unforced_market_data` (rule `unforced-market-data`) fires cleanly on
the shipped answer text — verified by running
`scripts/validate_answer.py --file <answer> --tools lookup_market_price,lookup_earnings_date`,
which returns:

```
rule: unforced-market-data
match: "IV"
why: chain statistic with a figure and no lookup_options_chain call
```

The answer's second arrow claims a "~9% IV-juiced expansion" without ever
calling `lookup_options_chain`. The class exists for exactly this pattern
and caught it in isolation, so it shipped despite the guard, not because the
guard doesn't cover it. Likely candidates: the guard isn't wired into the
send path for reply-to-quote turns (`[bulch's message to you]` shape) the
way it is for fresh Type 1 questions, or a post-guard retry/rewrite
reintroduced the figure. Needs a session to trace where in the pipeline this
turn diverged from the turns where the same class does fire (see the CLEAN
turns earlier the same day that correctly avoided bare IV claims).

### 2. regex-able (needs input-surface extension) — 20:33:24 UTC, Sam, "When are gap earnings"

`lookup_earnings_date(GPS)` returned `no_data`; route was `ungrounded` (no
Google Search fallback fired, despite the prompt's own rule that earnings-
date declarations "carry the required Google fallback" — `ask_prompt.py`
~line 174). The answer nonetheless states a specific date (August 27, 2026
AMC), an EPS range ($0.48–$0.52), revenue ($3.69B), and an exact call time
(2:00 PM PT / 5:00 PM ET) with no hedge. `scripts/validate_answer.py` returns
no violations — none of the nine current checker classes consume per-tool
**status**, only tool names, so a "confident claim despite the one relevant
tool failing" pattern can't be expressed yet.

Candidate fixture (blocked on plumbing tool status into the checker):
- question: `When are gap earnings`
- bad answer:
  ```
  → **August 27, 2026** after the market close (AMC) for the Q2 fiscal 2026 release

  → **Consensus projections:** EPS expected around **$0.48–$0.52** on **$3.69B** in revenue

  → Investor conference call set for **2:00 p.m. PT** / **5:00 p.m. ET**
  ```
- tool state: `lookup_earnings_date` status=`no_data`, no `google_search`
  call in the turn
- assertion sketch: flag when the only earnings/price/data tool relevant to
  the question returned `no_data`/`error`, no search grounding occurred, and
  the answer contains a specific date/estimate with no uncertainty language
  ("don't have", "no data on file", "can't confirm").
- must stay CLEAN on the near-identical 15:38:45 UTC turn same day (same
  ticker, same `no_data` tool status, but route was `grounded ✅` via a real
  search) — the assertion needs to key on absence of grounding, not tool
  failure alone, or it'll punish the correct pattern right next to the bad
  one.
- action needed first: extend `scripts/validate_answer.py`'s `--tools` input
  (or add a `--tool-status` flag) to carry per-tool status, since the CLI
  currently only takes tool names.

"""Put the Omnipulse body into the production pulse.

Spec: docs/superpowers/specs/2026-09-26-omnipulse-body-in-production.md

The Omnipulse (the claim-card pilot's editor output) writes the headline,
THE MAIN EVENT and BRIEFS to `pilot/shadow/<date>.clean.md` on the
`pilot-data` branch. The production routine keeps RECAP, WHAT TO WATCH
and the TRADE BOARD, which need live data only it has.

  fetch   poll pilot-data for today's Omnipulse, check it, and write
          /tmp/omnipulse_body.md (the INSIGHTS section in production's
          format) and /tmp/omnipulse_headline.txt. Exit 0 = use it,
          3 = not usable (classic pulse), 2 = usage error,
          4 = light pulse (only when MISS_DAY = "light": the body file
          holds a one-paragraph note and the headline file is empty).
  splice  replace a pulse's headline and INSIGHTS section with the saved
          Omnipulse ones. Used after DRAFT and again after EDIT, so
          neither can change the body.

Production drafts carry the body as one `## 2. INSIGHTS & ALPHA` section
with `###` themes in order; the bridge splits the first into THE MAIN
EVENT at post time. The Omnipulse's MAIN EVENT theme goes first.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# The switch. False = the classic pulse, byte for byte. The routine
# clones the working branch at every fire, so a push flips the next
# 10 AM run.
ENABLED = True

REPO = "gabjew90/Institutional-report-bot"
BRANCH = "pilot-data"
BODY_PATH = "/tmp/omnipulse_body.md"
HEADLINE_PATH = "/tmp/omnipulse_headline.txt"
INSIGHTS_HEADER = "## 2. INSIGHTS & ALPHA"
MIN_BRIEFS = 2
# Owner call 2026-09-26: MAIN EVENT plus the first 5 BRIEFS, in the order
# the Omnipulse editor wrote them. It writes 8-11 themes (1,500-1,850
# words); production ran 3-6.
MAX_BRIEFS = 5
# What a miss day publishes when the Omnipulse is missing or unusable
# after the wait (owner option a, 2026-09-29, spec
# 2026-09-29-light-pulse-miss-day-design.md). "classic" = today's
# classic pulse. "light" = RECAP, TRADE BOARD and WHAT TO WATCH around
# LIGHT_BODY_NOTE, no research body. Flipped in the retirement commit.
MISS_DAY = "classic"
LIGHT_BODY_NOTE = (
    "No research body today. The morning's bank research was not ready "
    "in time. The market read above and the calendar below are "
    "current, and the full edition returns tomorrow."
)
# A morning when no new bank research arrived since the last pulse (owner,
# 2026-10-08): the light pulse instead of re-serving the previous notes.
NO_RESEARCH_NOTE = (
    "No new bank research came in since the last pulse. The market read "
    "above and the calendar below are current, and the full edition "
    "returns with the next research."
)
EXIT_LIGHT = 4
MAX_FETCH_ERRORS = 3
# 10 minutes: the Omnipulse normally lands by 14:08 UTC and the gate runs
# about 14:11, so a longer wait mostly delays the classic fallback (the
# 20-minute wait on 2026-09-28 made that pulse 20 minutes late).
DEFAULT_WAIT_S = 600

_MARKER_RE = re.compile(r"\[(?:c|d)\d+\]")
_H2_RE = re.compile(r"^## .*$", re.MULTILINE)


def _token() -> str:
    tok = os.environ.get("GH_TOKEN", "").strip()
    if not tok:
        try:
            tok = open("/tmp/gh_token.txt", encoding="utf-8").read().strip()
        except OSError:
            tok = ""
    return tok


def _raw_get(url: str, token: str) -> str | None:
    req = urllib.request.Request(url, headers={
        "User-Agent": "omnipulse-body",
        "Cache-Control": "no-cache",
        **({"Authorization": f"token {token}"} if token else {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def fetch_text(path: str, token: str) -> str | None:
    """A file from pilot-data, or None when it does not exist yet.

    raw.githubusercontent.com, not api.github.com: the routine's agent
    proxy rejects every api.github.com request with 403 (the routine's
    COMMIT TRANSPORT note).

    WITHOUT the token first. The raw host answers 404 to a request whose
    token it rejects, even for a public file, and the routine's token is
    inert there: on 2026-09-28 the Omnipulse was published at 14:01 UTC
    and the gate polled 404s for its whole 20 minutes. The token is
    tried second, only for a private fork."""
    url = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{path}"
    got = _raw_get(url, "")
    if got is None and token:
        got = _raw_get(url, token)
    return got


# Why the last fetch() returned None, for the gate's log line: the
# 2026-09-28 message said "none after 1200s" whatever had happened.
LAST_FETCH_REASON = ""


def fetch(date: str, token: str, wait_s: int = DEFAULT_WAIT_S, every_s: int = 60,
          _sleep=time.sleep, _get=fetch_text) -> tuple[str, dict] | None:
    """Poll for the day's Omnipulse and its meta, up to `wait_s` seconds.
    The editor starts 13:55 UTC and has landed 14:00-14:08; the gate
    runs about 14:11, so the file is usually there on the first try."""
    global LAST_FETCH_REASON
    LAST_FETCH_REASON = ""
    waited = 0
    errors = 0
    while True:
        try:
            md = _get(f"pilot/shadow/{date}.clean.md", token)
            meta_raw = _get(f"pilot/shadow/{date}.meta.json", token)
            errors = 0
        except Exception as e:
            # a 404 is "not yet" and returns None above; an exception is
            # the host refusing us, which waiting will not fix
            errors += 1
            print(f"omnipulse: fetch error ({e})", file=sys.stderr)
            if errors >= MAX_FETCH_ERRORS:
                LAST_FETCH_REASON = f"fetch error: {str(e)[:120]}"
                return None
            md = meta_raw = None
        if md and meta_raw:
            try:
                return md, json.loads(meta_raw)
            except ValueError:
                LAST_FETCH_REASON = "meta is not JSON"
                return None
        if waited >= wait_s:
            LAST_FETCH_REASON = f"not published after {waited}s"
            return None
        _sleep(every_s)
        waited += every_s


def git_blob_sha(text: str) -> str:
    """Git's blob hash of a file's bytes: the `sha` GitHub returns with
    every file read."""
    import hashlib
    data = text.encode("utf-8")
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def read_local(src_dir: str, date: str, sha: str | None = None) -> tuple[str, dict] | None:
    """The day's Omnipulse from files the routine saved itself. With
    `sha` (GitHub's blob hash from the same read), the saved text must
    hash to it: the routine model writes the file, and a copy it
    shortened or reworded would otherwise pass every structural check."""
    global LAST_FETCH_REASON
    LAST_FETCH_REASON = ""
    try:
        md = open(os.path.join(src_dir, f"{date}.clean.md"), encoding="utf-8").read()
        meta = json.loads(open(os.path.join(src_dir, f"{date}.meta.json"),
                               encoding="utf-8").read())
    except (OSError, ValueError) as e:
        LAST_FETCH_REASON = f"local copy unreadable: {e}"
        return None
    if sha and git_blob_sha(md) != sha.strip().lower():
        LAST_FETCH_REASON = "local copy does not match GitHub's sha"
        return None
    return md, meta


def _sections(md: str) -> dict[str, str]:
    """H2 title -> body text up to the next H2."""
    out, heads = {}, list(_H2_RE.finditer(md))
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(md)
        out[h.group(0)[3:].strip()] = md[h.end():end]
    return out


def _themes(body: str) -> list[str]:
    """`### ` blocks, each from its heading to the next one."""
    parts = re.split(r"(?m)^(?=### )", body)
    return [p.strip() for p in parts if p.strip().startswith("### ")]


def problems(md: str, meta: dict) -> list[str]:
    """Why this Omnipulse must not go out; empty when it can."""
    out = []
    if (meta or {}).get("unread_source_files_at_edit"):
        out.append(f"{meta['unread_source_files_at_edit']} source files unread at edit")
    if (meta or {}).get("structural_problems"):
        out.append(f"structural problems: {meta['structural_problems']}")
    first = (md or "").lstrip().splitlines()[:1]
    if not first or not first[0].startswith("# "):
        out.append("no # headline")
    secs = _sections(md or "")
    main = next((v for k, v in secs.items() if re.fullmatch(r"(2\. )?THE MAIN EVENT", k)), None)
    briefs = next((v for k, v in secs.items() if re.fullmatch(r"(3\. )?BRIEFS", k)), None)
    if main is None or len(_themes(main)) != 1:
        out.append("THE MAIN EVENT must hold exactly one theme")
    if briefs is None or len(_themes(briefs)) < MIN_BRIEFS:
        out.append(f"fewer than {MIN_BRIEFS} BRIEFS")
    if _MARKER_RE.search(md or ""):
        out.append("citation markers survived the clean step")
    return out


def to_insights(md: str) -> tuple[str, str]:
    """(headline line, INSIGHTS section) in production's draft format."""
    headline = md.lstrip().splitlines()[0].strip()
    secs = _sections(md)
    main = next(v for k, v in secs.items() if re.fullmatch(r"(2\. )?THE MAIN EVENT", k))
    briefs = next(v for k, v in secs.items() if re.fullmatch(r"(3\. )?BRIEFS", k))
    themes = _themes(main) + _themes(briefs)[:MAX_BRIEFS]
    return headline, INSIGHTS_HEADER + "\n\n" + "\n\n".join(themes) + "\n"


def light_body(note: str | None = None) -> str:
    """The INSIGHTS section for a light pulse: the header and the note,
    no themes."""
    return INSIGHTS_HEADER + "\n\n" + (note or LIGHT_BODY_NOTE) + "\n"


def splice(pulse_md: str, headline: str, insights: str) -> str:
    """Replace the pulse's `# ` headline and its INSIGHTS section (header
    through the next H2) with the Omnipulse ones. Raises ValueError when
    the pulse has no INSIGHTS section to replace. An empty headline keeps
    the pulse's own H1 (light pulse)."""
    m = re.search(r"(?m)^## (?:\d+\. )?INSIGHTS & ALPHA[^\n]*\n", pulse_md)
    if not m:
        raise ValueError("pulse has no INSIGHTS & ALPHA section")
    nxt = _H2_RE.search(pulse_md, m.end())
    end = nxt.start() if nxt else len(pulse_md)
    out = pulse_md[:m.start()] + insights.rstrip() + "\n\n" + pulse_md[end:]
    if not headline.strip():
        return out          # light pulse: DRAFT's own H1 stands
    out, n = re.subn(r"(?m)^# (?!#).*$", lambda _m: headline, out, count=1)
    if not n:
        out = headline + "\n\n" + out
    return out


def _miss_day(date: str, why: str, body_path: str, headline_path: str) -> int:
    """Exit for a day without a usable Omnipulse: 3 = classic pulse,
    EXIT_LIGHT = the light body is written and the routine runs the
    omnipulse-day path around it."""
    if MISS_DAY != "light":
        print(f"omnipulse: none for {date} ({why}) -> classic pulse")
        return 3
    with open(body_path, "w", encoding="utf-8") as fh:
        fh.write(light_body())
    with open(headline_path, "w", encoding="utf-8") as fh:
        fh.write("")
    print(f"omnipulse: none for {date} ({why}) -> light pulse")
    return EXIT_LIGHT


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--date", required=True)
    f.add_argument("--wait", type=int, default=DEFAULT_WAIT_S)
    # Backup route: a directory holding <date>.clean.md and
    # <date>.meta.json that the routine read through its GitHub
    # connection, when the direct fetch fails.
    f.add_argument("--from", dest="src_dir", default=None)
    f.add_argument("--sha", default=None)
    s = sub.add_parser("splice")
    s.add_argument("--in", dest="src", required=True)
    s.add_argument("--out", required=True)
    for p_ in (f, s):
        p_.add_argument("--body", default=None)
        p_.add_argument("--headline", default=None)
    a = ap.parse_args(argv)
    body_path = a.body or BODY_PATH
    headline_path = a.headline or HEADLINE_PATH

    if a.cmd == "fetch":
        if a.src_dir:
            got = read_local(a.src_dir, a.date, a.sha)
        else:
            got = fetch(a.date, _token(), wait_s=a.wait)
        why = ""
        if not got:
            why = LAST_FETCH_REASON or "not found"
        else:
            md, meta = got
            bad = problems(md, meta)
            if bad:
                why = "not usable: " + "; ".join(bad)
        if why:
            return _miss_day(a.date, why, body_path, headline_path)
        headline, insights = to_insights(md)
        with open(body_path, "w", encoding="utf-8") as fh:
            fh.write(insights)
        with open(headline_path, "w", encoding="utf-8") as fh:
            fh.write(headline)
        n = len(re.findall(r"(?m)^### ", insights))
        print(f"omnipulse: {a.date} ok, {n} themes -> omnipulse pulse")
        return 0

    try:
        insights = open(body_path, encoding="utf-8").read()
        headline = open(headline_path, encoding="utf-8").read().strip()
    except OSError as e:
        print(f"omnipulse: no saved body ({e})", file=sys.stderr)
        return 2
    src = open(a.src, encoding="utf-8").read()
    try:
        out = splice(src, headline, insights)
    except ValueError as e:
        print(f"omnipulse: splice failed: {e}", file=sys.stderr)
        return 2
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write(out)
    print(f"omnipulse: body spliced into {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

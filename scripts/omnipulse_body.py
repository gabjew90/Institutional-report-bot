"""Put the Omnipulse body into the production pulse.

Spec: docs/superpowers/specs/2026-09-26-omnipulse-body-in-production.md

The Omnipulse (the claim-card pilot's editor output) writes the headline,
THE MAIN EVENT and BRIEFS to `pilot/shadow/<date>.clean.md` on the
`pilot-data` branch. The production routine keeps RECAP, WHAT TO WATCH
and the TRADE BOARD, which need live data only it has.

  fetch   poll pilot-data for today's Omnipulse, check it, and write
          /tmp/omnipulse_body.md (the INSIGHTS section in production's
          format) and /tmp/omnipulse_headline.txt. Exit 0 = use it,
          3 = not usable (classic pulse), 2 = usage error.
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
ENABLED = False

REPO = "gabjew90/Institutional-report-bot"
BRANCH = "pilot-data"
BODY_PATH = "/tmp/omnipulse_body.md"
HEADLINE_PATH = "/tmp/omnipulse_headline.txt"
INSIGHTS_HEADER = "## 2. INSIGHTS & ALPHA"
MIN_BRIEFS = 2
MAX_FETCH_ERRORS = 3

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


def fetch_text(path: str, token: str) -> str | None:
    """A file from pilot-data, or None when it does not exist yet.

    raw.githubusercontent.com, not api.github.com: the routine's agent
    proxy rejects every api.github.com request with 403 (the routine's
    COMMIT TRANSPORT note), and the routine already reads its context
    from the raw host. The repo is public; the token is sent only when
    present, for a private fork."""
    url = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{path}"
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


def fetch(date: str, token: str, wait_s: int = 1200, every_s: int = 60,
          _sleep=time.sleep, _get=fetch_text) -> tuple[str, dict] | None:
    """Poll for the day's Omnipulse and its meta, up to `wait_s` seconds.
    The editor starts 13:55 UTC and has landed 14:00-14:08, so the
    routine usually finds it on the first or second try."""
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
                return None
            md = meta_raw = None
        if md and meta_raw:
            try:
                return md, json.loads(meta_raw)
            except ValueError:
                print("omnipulse: meta is not JSON", file=sys.stderr)
                return None
        if waited >= wait_s:
            return None
        _sleep(every_s)
        waited += every_s


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
    themes = _themes(main) + _themes(briefs)
    return headline, INSIGHTS_HEADER + "\n\n" + "\n\n".join(themes) + "\n"


def splice(pulse_md: str, headline: str, insights: str) -> str:
    """Replace the pulse's `# ` headline and its INSIGHTS section (header
    through the next H2) with the Omnipulse ones. Raises ValueError when
    the pulse has no INSIGHTS section to replace."""
    m = re.search(r"(?m)^## (?:\d+\. )?INSIGHTS & ALPHA[^\n]*\n", pulse_md)
    if not m:
        raise ValueError("pulse has no INSIGHTS & ALPHA section")
    nxt = _H2_RE.search(pulse_md, m.end())
    end = nxt.start() if nxt else len(pulse_md)
    out = pulse_md[:m.start()] + insights.rstrip() + "\n\n" + pulse_md[end:]
    out, n = re.subn(r"(?m)^# (?!#).*$", lambda _m: headline, out, count=1)
    if not n:
        out = headline + "\n\n" + out
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--date", required=True)
    f.add_argument("--wait", type=int, default=1200)
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
        got = fetch(a.date, _token(), wait_s=a.wait)
        if not got:
            print(f"omnipulse: none for {a.date} after {a.wait}s -> classic pulse")
            return 3
        md, meta = got
        bad = problems(md, meta)
        if bad:
            print(f"omnipulse: {a.date} not usable -> classic pulse: " + "; ".join(bad))
            return 3
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

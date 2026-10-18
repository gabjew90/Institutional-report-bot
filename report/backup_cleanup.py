"""Deterministic clean-up of the Gemini backup pulse before it posts.

The Claude routine's pulse goes through pulse_lint, pulse_draft_validate
and the TRADE BOARD renderer before anyone reads it. The backup pulse
(pipeline.orchestrator.run_daily_pulse(backup=True)) went straight from
Gemini's AUDIT pass to Discord, and on 2026-10-14, its first live run,
it shipped what those checks exist to stop (2026-10-18 audit R3):

  - house trade calls: "Long $AVGO.", "Long $RSP, paired with long
    $XLU.", "Short $GALP." The owner's rule (2026-08-12) is that the
    pulse names only trades a desk called, with the desk attached.
  - five semicolons, which the voice contract bans outright.
  - "Thursday, Oct 15: ASML earnings" when ASML reported Wednesday 10/14.
  - the RECAP paragraph sitting under the headline with no section
    header, and one INSIGHTS block instead of MAIN EVENT and BRIEFS.

Each fix here is mechanical. The prose itself is not rewritten.
"""

import logging
import re
from datetime import date

from ai_analysis.voice_rules import SOURCE_PREFIX_BANKS

log = logging.getLogger(__name__)

_CALL_START_RE = re.compile(
    # not "Long-dated $TLT yields..." or "Short-term"
    r"^(?:Long|Short|Buy|Sell|Own|Fade|Pair)\b(?![-‐])[^.!?\n]*\$[A-Z]{1,6}\b", re.I)
_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]?")


def _names_a_desk(sentence: str) -> bool:
    low = sentence.lower()
    return any(re.search(r"\b" + re.escape(b.lower()) + r"\b", low)
               for b in SOURCE_PREFIX_BANKS)


def strip_house_calls(md: str) -> tuple[str, list[str]]:
    """Drop sentences that open with a trade instruction on a cashtag and
    name no desk. A desk-attributed call ("Goldman is long $AVGO") stays."""
    dropped: list[str] = []
    out_lines = []
    for line in md.split("\n"):
        if line.startswith("#"):
            out_lines.append(line)
            continue
        kept = []
        for m in _SENTENCE_RE.finditer(line):
            sent = m.group(0)
            if _CALL_START_RE.match(sent.strip()) and not _names_a_desk(sent):
                dropped.append(sent.strip())
                continue
            kept.append(sent)
        new = "".join(kept).rstrip()
        if line.strip() and not new.strip():
            continue  # the whole line was a house call
        out_lines.append(new if new != line.rstrip() else line)
    return "\n".join(out_lines), dropped


def clean_punctuation(md: str) -> str:
    """The two hard bans, outside headers: em/en-dashes and semicolons."""
    out = []
    for line in md.split("\n"):
        if not line.startswith("#"):
            line = re.sub(r"\s*[—–]\s*", ", ", line)
            line = line.replace(";", ",")
            line = re.sub(r",\s*,", ",", line)
        out.append(line)
    return "\n".join(out)


def restructure(md: str) -> str:
    """Give the RECAP its header and split the single INSIGHTS block into
    THE MAIN EVENT (first theme) and BRIEFS (the rest), the layout the
    formatter colours and the room reads every other day."""
    lines = md.split("\n")
    # RECAP header: prose between the H1 and the first "## " header.
    first_h2 = next((i for i, l in enumerate(lines) if l.startswith("## ")), None)
    h1 = next((i for i, l in enumerate(lines) if l.startswith("# ")), None)
    if first_h2 is not None and h1 is not None and h1 < first_h2:
        between = [l for l in lines[h1 + 1:first_h2] if l.strip()]
        if between and "RECAP" not in lines[first_h2].upper():
            lines.insert(h1 + 1, "")
            lines.insert(h1 + 2, "## RECAP")
    md = "\n".join(lines)

    m = re.search(r"^## [^\n]*INSIGHTS[^\n]*\n(.*?)(?=^## |\Z)", md, re.M | re.S)
    if not m:
        return md
    body = m.group(1)
    themes = [t for t in re.split(r"(?=^### )", body, flags=re.M) if t.strip()]
    if not themes or not themes[0].startswith("### "):
        return md
    new = "## THE MAIN EVENT\n" + themes[0].rstrip() + "\n\n"
    if len(themes) > 1:
        new += "## BRIEFS\n" + "\n\n".join(t.rstrip() for t in themes[1:]) + "\n\n"
    return md[:m.start()] + new + md[m.end():]


_WATCH_EARN_RE = re.compile(
    r"^\s*[-*]\s*\*\*(?:Mon|Tues|Wednes|Thurs|Fri)day,?\s+"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}):?\*\*:?\s*"
    r"([^;:\n]*?)\s+earnings", re.I)
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _acronym(name: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z]+", name)
             if w.lower() not in {"inc", "corp", "co", "ltd", "plc", "nv", "sa", "ag",
                                  "the", "of", "and", "group", "holding", "holdings"}]
    return "".join(w[0] for w in words).upper()


def subject_on_date(subject: str, rows: dict) -> bool:
    """True when `subject` ("ASML", "TSMC", "$JPM", "Netflix") is on that
    date's Nasdaq earnings list by symbol, name word or name initials."""
    s = subject.strip().lstrip("$").strip()
    if not s:
        return True
    s_up = s.upper()
    for sym, info in rows.items():
        name = str(info.get("name") or "")
        if s_up == sym.upper():
            return True
        acr = _acronym(name)
        if len(acr) > 2 and (acr.startswith(s_up) or s_up.startswith(acr)):
            return True  # TSMC for Taiwan Semiconductor Manufacturing
        first = (re.findall(r"[A-Za-z]+", name) or [""])[0]
        if first and first.lower() == s.split()[0].lower():
            return True
    return False


def drop_misdated_earnings(md: str, year: int, fetch=None,
                           today_month: int = 0) -> tuple[str, list[str]]:
    """Drop a WHAT TO WATCH earnings bullet whose company is not on Nasdaq's
    calendar for the date the bullet gives. Nasdaq unavailable keeps it."""
    if fetch is None:
        from report.news_data import fetch_nasdaq_earnings_rows as fetch
    dropped: list[str] = []
    cache: dict = {}
    out = []
    for line in md.split("\n"):
        m = _WATCH_EARN_RE.match(line)
        if m:
            mon = _MONTHS[m.group(1)[:3].lower()]
            # a December pulse naming "Jan 5" means next year
            y = year + 1 if today_month and mon < today_month - 6 else year
            try:
                iso = date(y, mon, int(m.group(2))).isoformat()
            except ValueError:
                out.append(line)   # "Feb 30": nothing to check against
                continue
            if iso not in cache:
                try:
                    cache[iso] = fetch(iso)
                except Exception:
                    cache[iso] = None
            rows = cache[iso]
            subjects = [p for p in re.split(r",|\band\b|&", m.group(3)) if p.strip()]
            if rows and subjects and not all(subject_on_date(p, rows) for p in subjects):
                dropped.append(line.strip())
                continue
        out.append(line)
    return "\n".join(out), dropped


def clean_backup_pulse(md: str, today: date | None = None, fetch=None) -> tuple[str, dict]:
    """All of the above, in order. Returns the markdown and what changed."""
    today = today or date.today()
    md, calls = strip_house_calls(md)
    md = clean_punctuation(md)
    md = restructure(md)
    md, misdated = drop_misdated_earnings(md, today.year, fetch=fetch, today_month=today.month)
    md = re.sub(r"\n{3,}", "\n\n", md)
    notes = {"house_calls_dropped": calls, "misdated_earnings_dropped": misdated}
    if calls or misdated:
        log.info(f"backup pulse clean-up: {notes}")
    return md, notes

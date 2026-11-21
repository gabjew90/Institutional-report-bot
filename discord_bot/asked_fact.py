"""A fact asked about a stock that no feed carries (2026-11-21, owner's pick
B of 2026-11-20: "code says the feeds don't carry it, runs one search for
exactly that fact, and posts the sourced answer or a plain couldn't find it").

First fact: the clock time of an earnings release and of its call. The
feeds carry the day and the session ("after market close"), never a time.
Of the 20 clock-time questions logged since 09-01, 9 were about a report
or its call. Three did not land (2026-11-11 "what time does nbis report",
asked twice, answered with the day and the session; 2026-11-17 "what time
is Nvidia earnings call", answered with the price and a news item). Others
were answered from habit with nothing behind them: "numbers typically cross
the wire between 4:05 and 4:15 PM ET, with the conference call following at
5:00 PM ET" (LEN, 09-16) and "right after the 4:00 PM ET bell" for NVDA,
which released at about 4:20. Where the model happened to search, the
company's own notice gave the answer (ORCL, MU, APLD, PANW).

So the search runs in code, as the news search does (news_tool.py): one
grounded call that asks for exactly the announced times in a fixed form.
Code parses it, converts the stated time zone to Eastern, checks the date
against the report date the feeds carry, and renders the line. The model
writes none of it. stock_body posts the line first on the turn, and an
arrow of the model's that gives a clock time the line does not carry is
dropped.

What makes a searched time trustworthy without a link: the reply must name
the report's date, and that date must be the one the earnings feeds carry
(a day either side). A coming report's date is past anything the model was
trained on, as with the dated news lines.

Prefetch only. Spend is recorded as `ask_fact`.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

FACT_TIMEOUT_S = 7.5          # under the router's 8 s prefetch deadline
FACT_ATTEMPTS = 2
CACHE_TTL_S = 10 * 60
MISS_TTL_S = 2 * 60
_CACHE: dict[str, tuple[float, dict]] = {}
_client = None

# The asker wants a clock time.
_CLOCK_RE = re.compile(
    r"\b(?:(?:at\s+)?what\s+(?:specific\s+|exact\s+)?time|what\s+hour|"
    r"(?:earnings?|report|call|release)\s+time|time\s+of\s+(?:the\s+)?(?:call|earnings?|report|release))\b", re.I)
_CALL_RE = re.compile(r"\b(?:call|webcast|conference)\b", re.I)
# On a shape that is not already about the report, the words must be.
_REPORT_RE = re.compile(
    r"\b(?:earnings?|reports?|reporting|results?|print|releases?|released|call|webcast)\b", re.I)

PROMPT = (
    "Search the web for the announced clock time of {who}'s most recent or next quarterly earnings "
    "release, and of its earnings conference call. Use the company's own investor-relations notice "
    "when there is one. Reply with exactly these four lines and nothing else:\n"
    "DATE: the date of that release, YYYY-MM-DD\n"
    "RELEASE: the time the results are released, with its time zone as the page states it "
    "(for example 4:20 PM ET or 1:05 PM PT), or NONE when the page gives only a session such as "
    "after market close\n"
    "CALL: the time the conference call starts, with its time zone as the page states it, or NONE\n"
    "SOURCE: the publisher of the page that states these times\n"
    "A time is one a page states for this report. Never a usual, typical or expected time. If no "
    "page states a time for either, reply exactly: NOT FOUND."
)

_ZONES = {
    "ET": "America/New_York", "EST": "America/New_York", "EDT": "America/New_York",
    "EASTERN": "America/New_York",
    "CT": "America/Chicago", "CST": "America/Chicago", "CDT": "America/Chicago", "CENTRAL": "America/Chicago",
    "MT": "America/Denver", "MST": "America/Denver", "MDT": "America/Denver", "MOUNTAIN": "America/Denver",
    "PT": "America/Los_Angeles", "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
    "PACIFIC": "America/Los_Angeles",
    "UTC": "UTC", "GMT": "UTC", "CET": "Europe/Paris", "CEST": "Europe/Paris", "BST": "Europe/London",
    "JST": "Asia/Tokyo", "HKT": "Asia/Hong_Kong", "SGT": "Asia/Singapore",
}
_NY = ZoneInfo("America/New_York")
_TIME_RE = re.compile(
    r"\b(\d{1,2})(?::(\d\d))?\s*([ap])\.?\s?m\.?\s*\(?\s*([A-Za-z]{2,8})?", re.I)
# a clock time in an answer: "5:00 PM", "5 p.m.", "4:20pm"
CLOCK_IN_TEXT_RE = re.compile(r"\b\d{1,2}(?::\d\d)?\s?(?:[ap]\.m\.|[ap]m)(?![a-z])", re.I)


def parse(asked: str, shape: str, symbol: str) -> dict | None:
    """The prefetch args when the asker's words ask the clock time of a
    stock's report or call, else None. On the earnings-date shape the
    question is already about the report; elsewhere its words must be."""
    from discord_bot import ask_router as R
    q = asked or ""
    if not symbol or not R.is_stock(symbol) or not _CLOCK_RE.search(q):
        return None
    if shape != R.EARNINGS_DATE and not _REPORT_RE.search(q):
        return None
    return {"symbol": symbol.upper(), "kind": "call" if _CALL_RE.search(q) else "release"}


# A second question joined to the first ("what time is amd earnings and
# what other main earnings we have").
_SECOND_ASK_RE = re.compile(
    r"\b(?:and|also|plus)\s+(?:what|who|how|did|do|does|is|are|was|when|which|why|any|should|will)\b", re.I)


def only_time(asked: str) -> bool:
    """The clock time is the whole question: the lines answer it and the
    model adds nothing. With a second question joined on, its arrows stay."""
    return not _SECOND_ASK_RE.search(asked or "")


def to_eastern(text: str, day: date) -> tuple[int, int] | None:
    """(hour, minute) in New York of a stated time on `day`, or None when
    the text holds no clock time or names a zone not known here. A time
    with no zone is taken as Eastern, which is how US notices state it."""
    m = _TIME_RE.search(text or "")
    if not m:
        return None
    hour, minute = int(m.group(1)) % 12, int(m.group(2) or 0)
    if m.group(3).lower() == "p":
        hour += 12
    if minute > 59:
        return None
    raw = m.group(4) or "ET"
    tz = _ZONES.get(raw.upper())
    if not tz:
        if re.fullmatch(r"[A-Z]{2,5}T", raw):
            return None                  # a zone this table does not hold ("IST", "AEST")
        tz = _ZONES["ET"]                # the next word of the sentence, not a zone
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(tz))
    ny = local.astimezone(_NY)
    return ny.hour, ny.minute


def _clock(hm: tuple[int, int]) -> str:
    h, m = hm
    return f"{(h - 1) % 12 + 1}:{m:02d} {'AM' if h < 12 else 'PM'} ET"


def read_reply(text: str) -> dict | None:
    """The four-line reply as {date, release, call, source}, times as
    (hour, minute) Eastern or None. None when the reply is NOT FOUND, has
    no date, or carries neither time."""
    if not text or "NOT FOUND" in text.upper():
        return None
    fields = {}
    for ln in text.splitlines():
        m = re.match(r"\s*[-*]*\s*(DATE|RELEASE|CALL|SOURCE)\s*[:\-]\s*(.*?)\s*\**\s*$", ln, re.I)
        if m:
            fields.setdefault(m.group(1).upper(), m.group(2).strip())
    dm = re.search(r"20\d\d-\d\d-\d\d", fields.get("DATE", ""))
    if not dm:
        return None
    try:
        day = date.fromisoformat(dm.group(0))
    except ValueError:
        return None
    release = to_eastern(fields.get("RELEASE", ""), day)
    call = to_eastern(fields.get("CALL", ""), day)
    if not release and not call:
        return None
    source = re.sub(r"[\[\]*`]", "", fields.get("SOURCE", "")).strip(" .")
    return {"date": day.isoformat(), "release": release, "call": call,
            "source": source if 2 <= len(source) <= 60 else ""}


def _get_client():
    global _client
    if _client is None:
        from config import settings
        from ai_analysis.usage_ledger import make_client
        key = settings.google_ask_api_key or settings.google_api_key
        if not key:
            return None
        _client = make_client("ask_fact", api_key=key)
    return _client


async def _execute_asked_fact(args: dict) -> dict:
    """Run the grounded search for one symbol's report and call time."""
    from google.genai import types
    from config import settings
    from discord_bot import listed_symbols as _listed
    from discord_bot.news_tool import _sources
    sym = (args.get("symbol") or "").strip().upper().lstrip("$")
    kind = args.get("kind") or "release"
    if not sym:
        return {"status": "error", "error": "No symbol provided."}
    hit = _CACHE.get(sym)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
        return {**hit[1], "kind": kind}
    client = _get_client()
    if client is None:
        return {"status": "error", "symbol": sym, "kind": kind, "error": "No Gemini key configured."}
    name = _listed.name(sym)
    who = f"{name} ({sym}, US-listed)" if name else f"the US-listed company {sym}"
    config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
        temperature=0.0, max_output_tokens=300,
        thinking_config=types.ThinkingConfig(thinking_level="MINIMAL"),
    )
    t0 = time.monotonic()
    out: dict = {"status": "error", "symbol": sym, "error": "The search failed."}
    for _attempt in range(FACT_ATTEMPTS):
        left = FACT_TIMEOUT_S - (time.monotonic() - t0)
        if left < 1.5:
            break
        try:
            resp = await asyncio.wait_for(client.aio.models.generate_content(
                model=settings.ask_gemini_model or settings.gemini_model,
                contents=PROMPT.format(who=who), config=config), left)
        except asyncio.TimeoutError:
            out = {"status": "timeout", "symbol": sym, "error": "The search did not finish in time."}
            break
        except Exception as e:
            log.warning(f"asked fact {sym}: {e}")
            out = {"status": "error", "symbol": sym, "error": "The search failed."}
            continue
        text = (getattr(resp, "text", None) or "").strip()
        cand = (getattr(resp, "candidates", None) or [None])[0]
        got = read_reply(text)
        if got:
            out = {"status": "ok", "symbol": sym, **got,
                   "sources": _sources(getattr(cand, "grounding_metadata", None))}
            break
        out = {"status": "no_data", "symbol": sym, "error": "No page states a time."}
    if out["status"] in ("ok", "no_data"):
        now = time.monotonic()
        for k, (ts, _) in list(_CACHE.items()):
            if now - ts >= CACHE_TTL_S:
                _CACHE.pop(k, None)
        stamp = now if out["status"] == "ok" else now - (CACHE_TTL_S - MISS_TTL_S)
        _CACHE[sym] = (stamp, out)
    return {**out, "kind": kind}


def _site(fact: dict) -> str:
    """Who to credit: the publisher the search named when a returned link
    is from it, else the first link's site, else the name as given."""
    named = fact.get("source") or ""
    sites = [str(s.get("title") or "") for s in fact.get("sources") or [] if s.get("title")]
    if not sites:
        return named
    key = re.sub(r"[^a-z0-9]", "", named.lower())
    for site in sites:
        first = re.sub(r"[^a-z0-9]", "", site.lower().split(".")[0])
        if key and first and (first in key or key in first):
            return named
    return sites[0]


def checked(fact: dict | None, report_days: list[tuple[str, str]],
            today: date | None = None) -> dict | None:
    """The searched times that hold up, or None.

    `report_days` is every (date, session) the earnings feeds carry for the
    stock: the coming report, one out or due, the last one. The reply's
    date must be within a day of one of them. With no date from any feed
    the reply stands only when the search returned links and the date is
    near today. A release time on the wrong side of the session is dropped
    ("after market close" and 1:05 PM is a zone the reply got wrong)."""
    if not isinstance(fact, dict) or fact.get("status") != "ok":
        return None
    try:
        day = date.fromisoformat(str(fact.get("date"))[:10])
    except ValueError:
        return None
    today = today or datetime.now(timezone.utc).astimezone(_NY).date()
    session = None
    for iso, sess in report_days:
        try:
            d = date.fromisoformat(str(iso)[:10])
        except ValueError:
            continue
        if abs((d - day).days) <= 1:
            session = sess or ""
            break
    if session is None:
        known = any(str(iso or "").strip() for iso, _ in report_days)
        if known or not fact.get("sources") or not (-7 <= (day - today).days <= 60):
            return None
        session = ""
    release, call = fact.get("release"), fact.get("call")
    release = tuple(release) if release else None
    call = tuple(call) if call else None
    after, before = "after" in session.lower(), "before" in session.lower()
    if release and ((after and release < (16, 0)) or (before and release > (9, 30))):
        release = None
    if call and after and call < (16, 0):
        call = None
    if release and call and call < release:
        call = None
    if not release and not call:
        return None
    return {"date": day.isoformat(), "release": release, "call": call, "site": _site(fact)}


def not_found(symbol: str) -> str:
    return f"→ No announced clock time was found for **{symbol}**'s release or its call."


def no_search(symbol: str) -> str:
    return f"→ The search for **{symbol}**'s announced report time did not come back."


def lines(fact: dict | None, report_days: list[tuple[str, str]], symbol: str,
          today: date | None = None) -> tuple[list[str], bool]:
    """(the posted line for a clock-time question, whether a time was
    found). `fact` is the prefetch result ({"status": "timeout"} when it
    did not come back)."""
    ok = checked(fact, report_days, today)
    if not ok:
        failed = not isinstance(fact, dict) or fact.get("status") in ("timeout", "error")
        return [no_search(symbol) if failed else not_found(symbol)], False
    d = date.fromisoformat(ok["date"])
    when = f"{d:%a} {d:%b} {d.day}"
    src = f" ({ok['site']})" if ok["site"] else ""
    call_asked = (fact or {}).get("kind") == "call"
    if ok["release"] and ok["call"]:
        parts = [f"results at **{_clock(ok['release'])}**", f"earnings call at **{_clock(ok['call'])}**"]
        if call_asked:
            parts.reverse()
        return [f"→ **{symbol}** {', '.join(parts)}, {when}{src}."], True
    if ok["call"]:
        return [f"→ **{symbol}** earnings call at **{_clock(ok['call'])}**, {when}{src}. "
                "No release time was found."], True
    return [f"→ **{symbol}** results at **{_clock(ok['release'])}**, {when}{src}."
            + (" No call time was found." if call_asked else "")], True


def unposted_clock(part: str, posted: str) -> bool:
    """The arrow gives a clock time the posted lines do not carry."""
    def _norm(s: str) -> str:
        s = re.sub(r"[\s.]", "", s.lower())
        return re.sub(r":00(?=[ap]m)", "", s)
    have = {_norm(m.group(0)) for m in CLOCK_IN_TEXT_RE.finditer(posted or "")}
    return any(_norm(m.group(0)) not in have for m in CLOCK_IN_TEXT_RE.finditer(part or ""))

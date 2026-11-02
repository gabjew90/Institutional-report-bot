"""A chat recap reads the whole window, in code (2026-11-02, owner).

"summarize the chat for the last 6 hours" got three generic bullets and the
room called it a shit recap. The model had asked chat search for the window,
which returned at most 200 of about 1,200 messages across every channel, cut
them to 12,000 characters, and said nothing about what it had left out.

This prefetch reads every message in the window for the channel asked about,
by channel_id, drops chart commands, counts in code who spoke and which tickers
came up (by author_id), and summarises long stretches in chunks with the same
cheap model. The model writes the recap from those notes, and phase 9 ends the
answer with a line built here saying what was read. A chunk the model would not
summarise is named as a gap, never filled in.

Spend is recorded as `ask_recap`: about a cent for a busy six hours.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

DEFAULT_HOURS = 6.0
MAX_HOURS = 48.0
DIRECT_CHARS = 12_000       # short enough to hand to the writer as it is
MAX_CHUNKS = 8
MAX_CHUNK_CHARS = 30_000
CHUNK_TIMEOUT_S = 16.0      # under the prefetch deadline bot.py gives this tool
CONCURRENCY = 4
MAX_LINE_CHARS = 300
TOP_TICKERS = 8

_RECAP_WORD_RE = re.compile(r"\b(?:summar(?:y|ize|ise)|recap|tl;?dr)\b", re.I)
_CATCH_UP_RE = re.compile(r"\b(?:catch\s+me\s+up|what(?:'?d|\s+did)\s+i\s+miss)\b", re.I)
_CHAT_WORD_RE = re.compile(r"\b(?:chat|convo|conversation|room|channel|discussion|in\s+here|y'?all)\b", re.I)
_WINDOW_RE = re.compile(
    r"\b(?:last|past)\s+(?:(\d{1,3}|an?|few|couple(?:\s+of)?)\s*)?(min(?:ute)?s?|h(?:ou)?rs?|days?)\b", re.I)
_TODAY_RE = re.compile(r"\b(?:today|this\s+morning|this\s+afternoon|so\s+far)\b", re.I)
# Someone's own words are chat search's job: "summarize Owen's messages"
_ONE_PERSON_RE = re.compile(r"\b\w+'s\s+(?:messages?|posts?|chat)\b|\bwhat\s+\w+\s+said\b", re.I)
# Another subject is not a chat recap: "summary of the market today", "recap
# today's games", "summarize the call today"
_OTHER_SUBJECT_RE = re.compile(
    r"\b(?:market|stocks?|earnings|pulse|cpi|fed|news|futures|games?|matchups?|league|calls?|article"
    r"|report|speech|meeting|episode|fight|show|podcast|interview)\b", re.I)
# "what did i miss on NVDA": the rest of the sentence names the subject
_CATCH_UP_ON_RE = re.compile(r"^\s+(?:on|about|with|from|in)\b(?!\s+here\b)", re.I)
_CHART_RE = re.compile(r"^\s*fc\s+\$?[a-z]{1,5}\b(?:\s+\w+)?\s*$", re.I)
_MENTION_RE = re.compile(r"<@!?\d+>")
_CHANNEL_REF_RE = re.compile(r"<#(\d+)>")
_CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,5})\b")
_CAPITALS_RE = re.compile(r"(?<![\w$])([A-Z]{2,5})\b")


def asks(own: str) -> bool:
    """The asker wants the room's conversation summarised."""
    text = own or ""
    if _ONE_PERSON_RE.search(text):
        return False
    if _CHAT_WORD_RE.search(text) and (_CATCH_UP_RE.search(text) or _RECAP_WORD_RE.search(text)):
        return True
    catch_up = _CATCH_UP_RE.search(text)
    if catch_up:
        return not _CATCH_UP_ON_RE.match(text[catch_up.end():])
    if not _RECAP_WORD_RE.search(text):
        return False
    return bool((_WINDOW_RE.search(text) or _TODAY_RE.search(text)) and not _OTHER_SUBJECT_RE.search(text))


def window_hours(own: str, now: datetime | None = None) -> float:
    """How far back the asker means, in hours, capped. Default six."""
    text = own or ""
    m = _WINDOW_RE.search(text)
    if m:
        qty, unit = (m.group(1) or "1").lower(), m.group(2).lower()
        n = {"a": 1.0, "an": 1.0, "few": 3.0}.get(qty, 2.0 if qty.startswith("couple") else None)
        if n is None:
            n = float(qty)
        hours = n / 60 if unit.startswith("min") else n * 24 if unit.startswith("day") else n
        return max(0.25, min(hours, MAX_HOURS))
    if _TODAY_RE.search(text):
        from zoneinfo import ZoneInfo
        ny = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
        return max(0.25, min(ny.hour + ny.minute / 60, MAX_HOURS))
    return DEFAULT_HOURS


def named_channel(own: str) -> int | None:
    m = _CHANNEL_REF_RE.search(own or "")
    return int(m.group(1)) if m else None


def _et(iso: str) -> str:
    from zoneinfo import ZoneInfo
    dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo("America/New_York")).strftime("%I:%M %p").lstrip("0")


def _clean_channel(name: str) -> str:
    return re.sub(r"[^\w-]", "", name or "").strip("-_") or "this channel"


def _symbols(text: str) -> set[str]:
    """Every symbol a line writes: a cashtag, or capitals something is listed
    under that are not an English word ("AAPL or NVDA?", not "I AM OUT"). The
    router's extract_tickers picks one subject for a question, which is a
    different job: it returns nothing for "AAPL or NVDA?"."""
    from discord_bot.ask_router import listed
    from discord_bot.listed_symbols import english_words
    found = {t.upper() for t in _CASHTAG_RE.findall(text)}
    if not text.isupper():
        words = english_words()
        found |= {t for t in _CAPITALS_RE.findall(text) if listed(t) and t.lower() not in words}
    return found


def prepare(rows: list[dict]) -> dict:
    """Lines to read and what code can count. People are author_ids, each shown
    under the latest name they posted with."""
    names: dict[int, str] = {}
    for r in rows:
        names[r["author_id"]] = r.get("author_display") or r.get("author_username") or "?"
    lines, spoke, tickers, ticker_people = [], Counter(), Counter(), {}
    for r in rows:
        text = (r.get("content") or "").strip()
        if _CHART_RE.match(text):
            continue
        ocr = (r.get("image_ocr_text") or "").strip()
        if not text and not ocr:
            continue
        aid = r["author_id"]
        spoke[aid] += 1
        for t in _symbols(text):
            tickers[t] += 1
            ticker_people.setdefault(t, set()).add(aid)
        body = _MENTION_RE.sub("@someone", text) or f"[image: {' '.join(ocr.split())[:120]}]"
        lines.append({"at": r["posted_at"],
                      "text": f"{_et(r['posted_at'])} {names[aid]}: {' '.join(body.split())[:MAX_LINE_CHARS]}"})
    return {"lines": lines, "people": len(spoke),
            "top_talkers": [{"name": names[a], "messages": n} for a, n in spoke.most_common(5)],
            "top_tickers": [{"ticker": t, "messages": n, "people": len(ticker_people[t])}
                            for t, n in tickers.most_common(TOP_TICKERS)]}


def chunks(lines: list[dict]) -> list[list[dict]]:
    """Consecutive stretches of the window, at most MAX_CHUNKS of them."""
    total = sum(len(x["text"]) + 1 for x in lines)
    size = min(MAX_CHUNK_CHARS, max(DIRECT_CHARS, total // MAX_CHUNKS + 1))
    out: list[list[dict]] = [[]]
    used = 0
    for x in lines:
        if used and used + len(x["text"]) + 1 > size and len(out) < MAX_CHUNKS:
            out.append([])
            used = 0
        out[-1].append(x)
        used += len(x["text"]) + 1
    return out if out[0] else []


_client = None


def _get_client():
    global _client
    if _client is None:
        from config import settings
        from ai_analysis.usage_ledger import make_client
        key = settings.google_ask_api_key or settings.google_api_key
        if not key:
            return None
        _client = make_client("ask_recap", api_key=key)
    return _client


PROMPT = """These are messages from a Discord channel of options and crypto traders, {start} to {end} Eastern. Each line is the time, the member's name and what they wrote.

Write at most 8 plain lines on what the room talked about, most discussed first: tickers and trades members said they took or closed (name the member and use only numbers they wrote), news they reacted to, arguments and who took which side, running jokes. Use only what is written here. No preamble, no comment on the language.

{text}"""


async def _summarise(part: list[dict]) -> str:
    """Notes on one stretch, '' when the model returned nothing. Slurs are masked
    first: this text goes to the model only to be summarised, and their density
    is what trips the unconfigurable filter."""
    from google.genai import types
    from config import settings
    from discord_bot.bot import _mask_slur_tokens
    client = _get_client()
    if client is None:
        return ""
    prompt = PROMPT.format(start=_et(part[0]["at"]), end=_et(part[-1]["at"]),
                           text=_mask_slur_tokens("\n".join(x["text"] for x in part)))
    config = types.GenerateContentConfig(
        temperature=0.2, max_output_tokens=700,
        thinking_config=types.ThinkingConfig(thinking_level="MINIMAL"),
        safety_settings=[types.SafetySetting(category=c, threshold=types.HarmBlockThreshold.BLOCK_NONE)
                         for c in (types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                                   types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                                   types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                                   types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT)])
    resp = await asyncio.wait_for(client.aio.models.generate_content(
        model=settings.ask_gemini_model or settings.gemini_model, contents=prompt, config=config),
        CHUNK_TIMEOUT_S)
    try:
        return (resp.text or "").strip()
    except Exception:
        return ""


async def _section(part: list[dict], sem: asyncio.Semaphore) -> dict:
    span = {"from": _et(part[0]["at"]), "to": _et(part[-1]["at"]), "messages": len(part)}
    async with sem:
        try:
            notes = await _summarise(part)
        except Exception as e:
            log.warning(f"chat recap: a stretch failed ({span['from']}-{span['to']}): {type(e).__name__}: {e}")
            notes = ""
    return {**span, "notes": notes}


def coverage_line(result: dict) -> str:
    """The answer's last line, built here: what was actually read."""
    line = (f"→ Read **{result['read']:,}** messages from **{result['people']}** people in "
            f"#{result['channel']}, {result['start']} to {result['end']} ET.")
    gaps = result.get("gaps") or []
    if gaps:
        line += " Could not read " + ", ".join(f"{g['from']} to {g['to']}" for g in gaps) + "."
    return line


async def _execute_chat_recap(args: dict) -> dict:
    """Every message in the window for one channel, reduced to notes and counts."""
    import db
    channel_id = args.get("channel_id")
    if not channel_id:
        return {"status": "error", "error": "no channel"}
    hours = max(0.25, min(float(args.get("hours") or DEFAULT_HOURS), MAX_HOURS))
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    rows = await asyncio.to_thread(db.chat_window, int(channel_id),
                                   start.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S"))
    prep = await asyncio.to_thread(prepare, rows)
    lines = prep["lines"]
    if not lines:
        return {"status": "empty", "hours": hours,
                "note": "No messages are stored for this channel in this window. Say so, and invent nothing."}
    base = {"status": "ok", "channel": _clean_channel(rows[-1].get("channel_name") or ""), "hours": hours,
            "start": _et(start.isoformat()), "end": _et(end.isoformat()), "people": prep["people"],
            "top_talkers": prep["top_talkers"], "top_tickers": prep["top_tickers"]}
    if sum(len(x["text"]) + 1 for x in lines) <= DIRECT_CHARS:
        from discord_bot.bot import _mask_slur_tokens
        return {**base, "read": len(lines), "gaps": [],
                "messages": _mask_slur_tokens("\n".join(x["text"] for x in lines))}
    sem = asyncio.Semaphore(CONCURRENCY)
    sections = await asyncio.gather(*(_section(p, sem) for p in chunks(lines)))
    read = [s for s in sections if s["notes"]]
    if not read:
        return {**base, "status": "error", "error": "no stretch of the chat could be summarised", "read": 0}
    return {**base, "read": sum(s["messages"] for s in read),
            "gaps": [{"from": s["from"], "to": s["to"]} for s in sections if not s["notes"]],
            "sections": read}

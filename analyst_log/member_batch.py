"""Batch trade extraction for member posts in the alert channels.

WHY (2026-09-26). Every member message in an alert channel was its own
Gemini call: ~1,450 tokens, ~1,350 of them the same instructions, for a
message of 20-100 tokens. Member trades feed the ledger and the trader
ranks, which do not need to be live (owner: "the trade ledger doesn't need
to be urgent for non callers"). Official callers stay on the live path,
because their posts are re-announced as they happen.

What a batch gives the model that the single-message path never had:
  - each message's own post date, so a missing expiry defaults to the day
    it was posted and M/D years are inferred from that day, not from
    whenever the call happens to run;
  - the author's own earlier posts, so "sold half" after "NVDA 190c"
    resolves even when it is not a Discord reply;
  - the reply parent with its author, as before.

A message may only borrow a contract from its reply parent or from the
same author's earlier posts. Messages are grouped by author inside a batch
so one member's contract is never pinned to another member's "sold".

The output lists trades only; a message not listed is not a trade.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from config import settings

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
MAX_MSGS = 30            # messages per call
MAX_CHARS = 9000         # rendered message text per call
CONTEXT_PER_AUTHOR = 3   # earlier posts shown per author, context only
_TEXT_CAP = 400

_PROMPT = """\
You read posts from members of a trading chat's alerts channel and pull
out the ones that record an options or stock trade: an open, add, trim or
close of a specific contract.

What counts:
  "Ndx 29000c @7.00"            open NDX call, strike 29000, price 7.00
  "Sold all 5/22 435c @3.66"    close call 435, expiry 5/22, price 3.66
  "MSFT 430c 5/20 @3.65 BTO"    open MSFT call 430, expiry 5/20
  "Took half PLTR 137c 5/29"    trim PLTR call 137, expiry 5/29
  "Out of mstr 132c"            close MSTR call 132
Not trades:
  - opinions and ideas ("China is a buy here", "SNOW can be next week 370")
  - questions, including about someone else's trade ("you bought 5 SPX
    10000c?", "why did you make me full port")
  - wishes, plans and resting orders not yet filled ("need meta 770",
    "sell orders at 26 for my 960s", "i want $5 for my 145c")
  - reactions and gain updates with no action word ("😭", ".", "240%",
    "these are 8 now!"), even when replying to a trade
  - hype ("slam") with no contract, and anything too sparse to name a
    ticker AND a strike
The author must be the one trading, and the message itself must carry
the action.

A trade needs a ticker and a strike. A follow-up that states an action
("sold", "took half", "out", "added a second") may take them from:
  1. the post it replies to (shown under the message), or
  2. the SAME author's own earlier post, in EARLIER POSTS or earlier in
     this list, when the follow-up plainly refers to it.
Never take a contract from a different author's post unless the message
replies to that post. Never guess a ticker or strike.

Expiry: an explicit M/D wins, with the year inferred from the message's
own date (on or after that date: same year; up to 14 days before: same
year; more than 14 days before: next year). With no expiry: an open or
add uses the message's own date (same-day contract); a trim or close
uses null.

Action: sold / sold all / exit / out / cut / closed -> close. took half /
took some off / trimmed / scaled -> trim. added / more / doubled -> add.
BTO / bought / in at / re-entering / reload / back in / slam -> open. A new
contract with no action word -> open.

Shorthand: rut RUT, ndx NDX, spx SPX, spy SPY, qqq QQQ, chyna FXI. A
number followed by c or p is a call or put strike ("94c", "207.5p").
Price is the number after @, else null.

Return JSON {{"trades": [...]}} with one entry per trade message:
  {{"id": "<message id as shown, e.g. m3>", "ticker": "<UPPERCASE>",
   "contract_type": "call" | "put" | "stock", "strike": <number>,
   "expiry": "YYYY-MM-DD" or null, "action": "open"|"add"|"trim"|"close",
   "price": <number or null>, "summary": "<one line>",
   "confidence": "high"|"medium"|"low"}}
List only trades. An empty list is a normal answer.

{context}MESSAGES:
{messages}
"""

_client = None
_client_lock = threading.Lock()


def _get_client():
    global _client
    with _client_lock:
        if _client is None:
            from google import genai
            _client = genai.Client(api_key=settings.google_api_key)
        return _client


class _Refused(Exception):
    pass


def _et(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        from datetime import timezone
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_ET)


def _clip(text: str) -> str:
    return " ".join((text or "").split())[:_TEXT_CAP]


def build_chunks(msgs: list[dict]) -> list[list[dict]]:
    """Split one channel's messages (time-ordered) into model calls. Each
    author's messages stay together and in order; an author is never
    split across calls unless they alone exceed a call's size."""
    by_author: dict = {}
    for m in msgs:
        by_author.setdefault(m["author_id"], []).append(m)
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    cur_chars = 0
    for author_msgs in by_author.values():
        size = sum(len(_clip(m["content"])) for m in author_msgs)
        if cur and (len(cur) + len(author_msgs) > MAX_MSGS
                    or cur_chars + size > MAX_CHARS):
            chunks.append(cur)
            cur, cur_chars = [], 0
        for m in author_msgs:
            if len(cur) >= MAX_MSGS or cur_chars > MAX_CHARS:
                chunks.append(cur)
                cur, cur_chars = [], 0
            cur.append(m)
            cur_chars += len(_clip(m["content"]))
    if cur:
        chunks.append(cur)
    return chunks


def render(chunk: list[dict], context: dict | None = None) -> tuple[str, dict]:
    """Prompt text for one call and the short-id -> message map.

    Message dicts: id, author_id, author, posted_at, content, and when the
    message is a reply: parent_content, parent_author. `context` maps
    author_id to that author's earlier posts [{posted_at, content}]."""
    ids: dict[str, dict] = {}
    ctx_lines = []
    authors_here = {m["author_id"] for m in chunk}
    for aid, posts in (context or {}).items():
        if aid not in authors_here or not posts:
            continue
        name = next((m["author"] for m in chunk if m["author_id"] == aid), "?")
        for p in posts[-CONTEXT_PER_AUTHOR:]:
            et = _et(p["posted_at"])
            stamp = et.strftime("%Y-%m-%d %H:%M") if et else "?"
            ctx_lines.append(f"  {name} [{stamp}]: \"{_clip(p['content'])}\"")
    context_block = ""
    if ctx_lines:
        context_block = ("EARLIER POSTS (context only, never a trade on their "
                         "own):\n" + "\n".join(ctx_lines) + "\n\n")
    lines = []
    for i, m in enumerate(chunk, 1):
        sid = f"m{i}"
        ids[sid] = m
        et = _et(m["posted_at"])
        stamp = et.strftime("%Y-%m-%d %H:%M ET") if et else "?"
        lines.append(f"[{sid}] {stamp} · {m['author']}: \"{_clip(m['content'])}\"")
        if m.get("parent_content"):
            lines.append(f"      replying to {m.get('parent_author') or '?'}: "
                         f"\"{_clip(m['parent_content'])}\"")
    return _PROMPT.format(context=context_block, messages="\n".join(lines)), ids


def _call(prompt: str, model: str) -> list[dict]:
    from google.genai import types
    cfg = types.GenerateContentConfig(
        temperature=0.0,
        max_output_tokens=4000,
        response_mime_type="application/json",
        safety_settings=[
            types.SafetySetting(category=c,
                                threshold=types.HarmBlockThreshold.BLOCK_NONE)
            for c in (types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                      types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                      types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                      types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT)
        ],
    )
    resp = _get_client().models.generate_content(
        model=model, contents=prompt, config=cfg)
    try:
        text = resp.text or ""
    except Exception:
        text = ""
    if not text.strip():
        raise _Refused("empty response")
    try:
        data = json.loads(text)
    except Exception as e:
        raise _Refused(f"unparseable: {e}")
    trades = data.get("trades") if isinstance(data, dict) else data
    return [t for t in (trades or []) if isinstance(t, dict)]


def to_extracted(t: dict) -> dict:
    """A batch trade in the single-message extractor's shape, so the
    watcher's integrity check, expiry resolution and close metrics apply
    unchanged."""
    return {
        "is_trade_screenshot": True,
        "screenshot_type": "caption_only",
        "ticker": (t.get("ticker") or "").upper().lstrip("$") or None,
        "contract_type": (t.get("contract_type") or "").lower() or None,
        "strike": t.get("strike"),
        "expiry": t.get("expiry"),
        "action": (t.get("action") or "").lower() or None,
        "action_source": "caption",
        "gain_pct": None,
        "price": t.get("price"),
        "caption_summary": t.get("summary") or "",
        "confidence": t.get("confidence") or "medium",
        "notes": "member batch",
    }


_WORD_RE = re.compile(r"[A-Za-z]{2,}")
_MENTION_RE = re.compile(r"<@!?\d+>|<#\d+>|https?://\S+")


def _grounded(t: dict, m: dict, context: dict | None) -> bool:
    """Code checks on a model trade (2026-09-26 replay). The model filled
    the ticker with the poster's name ("BK", "SAM", "LIGMA") or a number,
    read reactions ("😭", "120%") and questions as trades, and inherited a
    parent's contract onto them. A trade must:
      - come from a message with a word of its own (not only emoji,
        numbers, mentions or links) and no question mark;
      - name a ticker that is letters and appears in the message, its
        reply parent, or the author's own earlier posts;
      - for an option, name a strike that appears in one of those."""
    text = m.get("content") or ""
    bare = _MENTION_RE.sub(" ", text)
    if "?" in bare or not _WORD_RE.search(bare):
        return False
    sources = [text, m.get("parent_content") or ""]
    sources += [p.get("content") or "" for p in (context or {}).get(m["author_id"], [])]
    sources += [x.get("content") or "" for x in m.get("_same_author_earlier", [])]
    hay = " ".join(sources).lower()
    ticker = (t.get("ticker") or "").upper().lstrip("$")
    if not re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", ticker):
        return False
    # a member's name is not a ticker ("Yes" to "Sam are you in 800
    # Friday" came back as SAM)
    names = {n.lower() for n in (m.get("author"), m.get("parent_author")) if n}
    names |= {w for n in list(names) for w in re.findall(r"[a-z]{2,}", n)}
    if ticker.lower() in names or ticker.lower() in _member_aliases():
        return False
    aliases = {ticker.lower()} | {k for k, v in _ALIASES.items() if v == ticker}
    named = any(re.search(rf"(?<![a-z]){re.escape(a)}(?![a-z])", hay) for a in aliases)
    # index options are traded by strike alone ("Sold my 7725c at 5.6");
    # the strike check below still has to hold
    if not named and ticker not in _INDEX_BY_STRIKE:
        return False
    if (t.get("contract_type") or "").lower() in ("call", "put"):
        strike = t.get("strike")
        try:
            sf = float(strike)
        except (TypeError, ValueError):
            return False
        forms = {f"{sf:g}", str(int(sf)) if sf == int(sf) else f"{sf:g}"}
        if not any(re.search(rf"(?<![\d.]){re.escape(f)}(?![\d])", hay) for f in forms):
            return False
    return True


# shorthand the prompt teaches, so a grounded check accepts it
_ALIASES = {"chyna": "FXI", "micron": "MU", "apple": "AAPL", "google": "GOOGL",
            "meta": "META", "nvidia": "NVDA", "tesla": "TSLA", "coreweave": "CRWV",
            "costco": "COST", "sandisk": "SNDK", "palantir": "PLTR"}
_INDEX_BY_STRIKE = {"SPX", "SPXW", "NDX", "NDXP", "XSP", "RUT"}


def _member_aliases() -> set:
    """What the room calls its members (db.member_aliases), so "kyle" or
    "ligma" is never read as a ticker. Empty when the map is not built."""
    try:
        import db
        return {k.lower() for k in (db.member_aliases() or {})}
    except Exception:
        return set()


def extract_chunk(chunk: list[dict], context: dict | None = None,
                  model: str | None = None) -> dict:
    """{discord message id: extracted} for the trades in one chunk.
    A refused chunk is split until the refusing message is alone, and
    that message is treated as not a trade. Transport errors propagate
    so the caller can leave the chunk for the next run."""
    model = model or settings.gemini_model
    prompt, ids = render(chunk, context)
    try:
        trades = _call(prompt, model)
    except _Refused as e:
        if len(chunk) == 1:
            log.info(f"member batch: message {chunk[0]['id']} refused ({e})")
            return {}
        mid = len(chunk) // 2
        out = extract_chunk(chunk[:mid], context, model)
        out.update(extract_chunk(chunk[mid:], context, model))
        return out
    # a follow-up may borrow from the same author's earlier message in
    # this chunk, so those count as grounding sources too
    earlier: dict = {}
    for m in chunk:
        m["_same_author_earlier"] = list(earlier.get(m["author_id"], []))
        earlier.setdefault(m["author_id"], []).append(m)
    out = {}
    for t in trades:
        m = ids.get(str(t.get("id") or "").strip())
        if m is None:
            continue
        if not _grounded(t, m, context):
            log.debug(f"member batch: ungrounded trade dropped {t} for {m.get('content')!r}")
            continue
        out[m["id"]] = to_extracted(t)
    return out


# --- the job -------------------------------------------------------------

MAX_PER_RUN = 2000        # messages per channel per run (catch-up bound)
_FIRST_RUN_LOOKBACK_H = 1  # a channel with no saved position starts here


def member_channels() -> list[str]:
    """Alert channels read in member mode: the eager-OCR channels that are
    not an official caller's own channel (those stay live)."""
    return sorted(ch for ch in settings.resolve_chat_eager_ocr_channels()
                  if not settings.caller_by_channel(ch))


def run_channel(channel: str, now: datetime | None = None) -> dict:
    """Read one channel from its saved position. Every model call for the
    window must succeed before the position moves; a failed call leaves
    the whole window for the next run (rows already written are not
    duplicated: the trade table ignores a repeated message id)."""
    import db
    from datetime import timedelta, timezone
    from analyst_log.watcher import could_be_trade_caption, record_caption_extraction

    now = now or datetime.now(timezone.utc)
    saved = db.member_batch_watermark(channel)
    after = int(saved) if saved else db.member_batch_start_row(
        channel, (now - timedelta(hours=_FIRST_RUN_LOOKBACK_H)).isoformat())
    rows = db.member_batch_messages(channel, after, MAX_PER_RUN)
    if not rows:
        return {"channel": channel, "messages": 0}
    last_row = rows[-1]["row_id"]
    msgs = [r for r in rows
            if could_be_trade_caption(r["content"] or "", bool(r["reply_parent_id"]))]
    for m in msgs:
        m["author"] = m.get("author") or "?"
    first = min((m["posted_at"] for m in msgs), default=rows[0]["posted_at"])
    since = (datetime.fromisoformat(first.replace("Z", "+00:00"))
             - timedelta(hours=24)).isoformat()
    context = {aid: db.member_batch_context(channel, aid, first, since,
                                            CONTEXT_PER_AUTHOR)
               for aid in {m["author_id"] for m in msgs}}
    trades, calls = {}, 0
    for chunk in build_chunks(msgs):
        try:
            trades.update(extract_chunk(chunk, context))
            calls += 1
        except Exception as e:
            log.warning(f"member batch: {channel} call failed, window kept "
                        f"for the next run: {e}")
            return {"channel": channel, "messages": len(rows), "failed": True}
    written = 0
    by_id = {m["id"]: m for m in msgs}
    for mid, extracted in trades.items():
        m = by_id[mid]
        try:
            if record_caption_extraction(
                    discord_message_id=mid, author_name=m["author"],
                    author_id=m["author_id"], posted_at=m["posted_at"],
                    caption=m["content"], extracted=extracted,
                    canonical_caller=None, tracking_mode="member",
                    write_non_trades=False):
                written += 1
        except Exception as e:
            log.warning(f"member batch: row for {mid} not written, window "
                        f"kept for the next run: {e}")
            return {"channel": channel, "messages": len(rows), "failed": True}
    db.set_member_batch_watermark(channel, str(last_row))
    return {"channel": channel, "messages": len(rows), "to_model": len(msgs),
            "calls": calls, "trades": written}


def run() -> list[dict]:
    if not settings.member_trade_batch_enabled:
        return []
    return [run_channel(ch) for ch in member_channels()]

"""Deterministic checks on a member profile before it is saved.

The 2026-10-18 audit read 37 rebuilt profiles. Identity and quotes were
right (440 of 450 quotes verbatim), and the failures were in what the
writer put AROUND the quotes:

  M1  a copypasta several people posted was written up as each poster's
      life ("missed his kids growing up", "step away from trading").
  M2  the framing named places, people, jobs and numbers the member never
      wrote: a surf trip to "Costa Rica" (his link was wacosurf.com), a
      "brother at Merrill Lynch", "union and Deloitte work history" (union
      is a snowboard-binding brand), "24% ... Druckenmiller".
  M5  invented phrases in single quotes passed as anchors.
  M8  room-reaction claims ("the room constantly accuses him of lying")
      got past the lint.

Each check drops the bullet that fails it; nothing is rewritten. The
trades section is written from the log (scripts/profile_trades.py).

2026-10-30 audit (run 7, design note 4): the writer was shown each line's
reply parent and the messages before it and still wrote a joke about
Grand as kloh's life, cemini23's remarks about another trader's orange
juice trades as his own, and two-to-five-month-old messages as "recent".
So every quote is now traced to its source message here
(`member_messages`, `find_source`), and the bullet is judged by that
message: its date, whether it answers someone about someone else, and how
many messages carry it. Kept bullets in the takes and life sections end
with the source's date, and who it replied to, so /ask sees them too.
"""
from __future__ import annotations

import re

import db

# Bullets in these sections are trade data, checked against the trade log.
_SKIP_SECTIONS = ("Recent trades", "Points", "Trading record", "Trader")

_DQ_RE = re.compile(r"[\"“]([^\"“”\n]{2,300})[\"”]")
_SQ_RE = re.compile(r"(?<![A-Za-z])['‘]([^'‘’\n]{6,200})['’](?![A-Za-z])")
_TITLE_RE = re.compile(r"\b([A-Z][a-z]{2,})(?:\s+([A-Z][a-z]{2,}))?\b")
_NUMBER_RE = re.compile(r"(?<![\w.])(\$?\d[\d,]*(?:\.\d+)?\s?(?:%|k|K|m|M|bn|B)?)(?![\w.])")
_STOP_TITLE = {
    "the", "and", "but", "his", "her", "him", "she", "they", "their", "when", "after",
    "before", "while", "with", "from", "this", "that", "these", "those", "who", "what",
    "where", "why", "how", "also", "still", "often", "frequently", "regularly", "claims",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "jan", "feb", "mar", "apr",
    "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "voice", "takes",
    "recent", "personal", "life", "personality", "style", "omnibeta", "omniwiz",
    "discord", "room", "chat", "bot", "insufficient", "signal", "trades", "trade",
    "calls", "puts", "call", "put", "long", "short", "bull", "bear", "fed", "god",
    "lol", "lmao", "bro", "dude", "christmas", "thanksgiving", "halloween", "easter",
    "nfl", "nba", "mlb", "nhl", "ufc", "boasts", "brags", "posts", "says", "said",
    "retarded", "jokes", "joked", "plans", "planning", "currently", "recently",
}


_FIRST_PERSON_RE = re.compile(
    r"\b(?:i|i'm|im|i've|ive|i'd|i'll|ill|me|my|mine|myself|we|we're|our|ours)\b", re.I)
_SECOND_PERSON_RE = re.compile(r"\b(?:you|your|you're|youre|yours|ur)\b", re.I)
# Framing that rules on whether a claim was true: the writer has no data
# for that and called SpaceX being public "incorrect" (run 7 M2, M14).
_VERDICT_RE = re.compile(
    r"\b(?:incorrect(?:ly)?|factually|untrue|(?:not|isn't|wasn't|isnt|wasnt) true"
    r"|(?:is|was|are|were) (?:false|wrong)|false (?:claim|statement|premise)"
    r"|(?:failed|fails) to materiali[sz]e|never materiali[sz]ed|didn't materiali[sz]e"
    r"|debunked|(?:which|that) (?:never|didn't) happen(?:ed)?)\b", re.I)
# A frequency the evidence must show more than once (run 7 M17).
_FREQ_RE = re.compile(
    r"\s*\b(?:constantly|regularly|frequently|routinely|habitually|repeatedly|perpetually"
    r"|religiously|always)\b", re.I)
# The code-written tail on a kept bullet; removed before a line is checked.
_TAG_RE = re.compile(r"\s*\(said [^()]*\)\s*$")
_TAG_SECTIONS = ("retarded takes", "recent personal life")
_CTX_WINDOW_S = 600


def _norm(s: str) -> str:
    s = (s or "").lower().replace("’", "'").replace("‘", "'")
    s = re.sub(r"[^\w\s$%'.]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


RECENT_DAYS = 60   # what a "Recent ..." section may draw on (2026-10-26 audit M6)


def _like_words(text: str) -> str:
    words = [w.replace("%", "").replace("_", "") for w in text.split()]
    return "%" + "%".join(w for w in words if w) + "%"


def member_messages(user_id: int, limit: int = 5000) -> list[dict]:
    """The member's messages, newest first, each with its normalised text."""
    rows = db.get_connection().execute(
        "SELECT id, channel_id, posted_at, content, image_ocr_text, reply_parent_id "
        "FROM chat_messages WHERE author_id = ? ORDER BY posted_at DESC LIMIT ?",
        (int(user_id), int(limit))).fetchall()
    return [{"id": r[0], "channel_id": r[1], "posted_at": r[2] or "", "content": r[3] or "",
             "reply_parent_id": r[5], "norm": _norm(f"{r[3] or ''} {r[4] or ''}")} for r in rows]


def find_source(phrase: str, msgs: list[dict]) -> tuple[dict | None, int]:
    """(the newest message carrying the quote, how many messages carry it)."""
    p = _norm(phrase)
    if not p:
        return None, 0
    hits = [m for m in msgs if p in m["norm"]]
    return (hits[0] if hits else None), len(hits)


def _context(msg: dict, user_id: int) -> tuple[str | None, list[str]]:
    """(who the message replied to, if someone else; the text it answered:
    the reply parent and up to two messages by others just before it in
    its channel, within ten minutes)."""
    from datetime import datetime
    conn = db.get_connection()
    replied_to, texts = None, []
    if msg.get("reply_parent_id"):
        r = conn.execute(
            "SELECT author_id, COALESCE(author_display, author_username), content "
            "FROM chat_messages WHERE discord_message_id = ?", (msg["reply_parent_id"],)).fetchone()
        if r and r[0] != user_id:
            replied_to, texts = r[1], [r[2] or ""]
    try:
        t = datetime.fromisoformat(msg["posted_at"].replace("Z", "+00:00"))
    except ValueError:
        return replied_to, texts
    for a_id, at, content in conn.execute(
            "SELECT author_id, posted_at, content FROM chat_messages WHERE channel_id = ? AND id < ? "
            "ORDER BY id DESC LIMIT 2", (msg["channel_id"], msg["id"])).fetchall():
        if a_id == user_id:
            break               # their own earlier line: they started this
        try:
            gap = (t - datetime.fromisoformat((at or "").replace("Z", "+00:00"))).total_seconds()
        except (ValueError, TypeError):
            break
        if gap > _CTX_WINDOW_S:
            break
        texts.append(content or "")
    return replied_to, texts


def _other_names(user_id: int) -> set[str]:
    """Names other members go by, without this member's own."""
    names: dict[str, set] = {}
    try:
        for alias, uid in db.member_aliases().items():
            names.setdefault(alias.lower(), set()).add(uid)
    except Exception:
        pass
    try:
        for uid, u, d in db.get_connection().execute(
                "SELECT user_id, username, display_name FROM user_profiles").fetchall():
            for v in (u, d):
                for tok in re.findall(r"[A-Za-z]{4,}", v or ""):
                    names.setdefault(tok.lower(), set()).add(uid)
    except Exception:
        pass
    return {n for n, uids in names.items() if user_id not in uids}


def about_someone_else(msg: dict, user_id: int, others: set[str]) -> bool:
    """A line that answers a message naming another member, or speaks to
    the person it answers, and says nothing in the first person, is about
    that other person: kloh's "Got goth chick pregnant" came right after
    "why did grand leave" (run 7 M1), and a cat, an orange juice trade and
    a shared login were other members' (M5, M6)."""
    text = msg.get("content") or ""
    if _FIRST_PERSON_RE.search(text):
        return False
    replied_to, ctx = _context(msg, user_id)
    if not ctx and not replied_to:
        return False
    if _SECOND_PERSON_RE.search(text):
        return True
    words = set(re.findall(r"[a-z]{3,}", " ".join(ctx).lower()))
    return bool(words & others)


def strip_tag(line: str) -> str:
    return _TAG_RE.sub("", line)


def _tag(msg: dict, user_id: int) -> str:
    from datetime import date
    try:
        d = date.fromisoformat(msg["posted_at"][:10])
    except ValueError:
        return ""
    replied_to, _ = _context(msg, user_id)
    to = f", replying to {replied_to}" if replied_to else ""
    return f" (said {d:%b} {d.day}{to})"


_PASTA_MIN_CHARS = 120   # a copypasta is a long block, not a catchphrase


def is_copypasta(phrase: str, user_id: int, days: int = 120) -> bool:
    """True when the member's message carrying `phrase` is a long block that
    another member also posted. Room catchphrases ("fuck it we ball", "are
    we winning yet") are short and stay the member's own voice; the
    10/14 "my kids went from toddlers to teenagers" block was posted by
    three people and is nobody's life story."""
    probe = phrase.strip()[:80]
    if len(probe) < 12:
        return False
    conn = db.get_connection()
    # word by word, so a double space or a line break in the message does
    # not hide it (run 7 M3: an all-caps meme got past an exact LIKE)
    rows = conn.execute(
        "SELECT content FROM chat_messages WHERE author_id = ? AND content LIKE ? COLLATE NOCASE "
        "AND posted_at >= datetime('now', ?) LIMIT 3",
        (int(user_id), _like_words(probe), f"-{int(days)} day")).fetchall()
    for (content,) in rows:
        if len(content or "") < _PASTA_MIN_CHARS:
            continue
        # A long all-caps block is a meme pasted in, even with one poster:
        # EFDHD's crypto meme built five bullets (2026-10-26 audit M2).
        letters = [c for c in content if c.isalpha()]
        if len(content) >= 200 and letters and sum(c.isupper() for c in letters) > 0.7 * len(letters):
            return True
        head = " ".join(content.split()[:10])
        n = conn.execute(
            "SELECT COUNT(DISTINCT author_id) FROM chat_messages WHERE content LIKE ? COLLATE NOCASE "
            "AND posted_at >= datetime('now', ?)", (_like_words(head), f"-{int(days)} day")).fetchone()
        if n and int(n[0] or 0) >= 2:
            return True
    return False


def _known_names() -> set[str]:
    names: set[str] = set()
    try:
        names |= {s.lower() for s in db.member_aliases()}
    except Exception:
        pass
    try:
        for r in db.get_connection().execute(
                "SELECT username, display_name FROM user_profiles").fetchall():
            for v in (r[0], r[1]):
                for tok in re.findall(r"[A-Za-z]{3,}", v or ""):
                    names.add(tok.lower())
    except Exception:
        pass
    return names


def _outside_quotes(line: str) -> str:
    return _SQ_RE.sub(" ", _DQ_RE.sub(" ", line))


def _written(word: str, haystack: str) -> bool:
    """The member wrote the word, or a short form of it ("cali" for
    California, "aus" for Australia, 3+ letters at a word start)."""
    lw = word.lower()
    if re.search(r"\b" + re.escape(lw), haystack):
        return True
    return any(re.search(r"\b" + re.escape(lw[:k]) + r"\b", haystack)
               for k in range(3, min(len(lw), 6)))


def _unit_form(token: str, after: str) -> str | None:
    """A pattern for a one-digit figure with its unit, as the member would
    write it: "$4k" -> 4k / 4,000 / 4 grand; "4" before "AM" -> 4am / 4 a.m.
    None when the figure has no unit."""
    d = re.sub(r"[^\d.]", "", token).strip(".")
    if not d:
        return None
    if re.match(r"\s*(?:a\.?m\.?|p\.?m\.?)\b", after, re.I):
        # a time needs its am/pm: "at 4" is not "4 AM" (run 7 M11)
        return r"(?<![\d.])" + re.escape(d) + r"(?::\d\d)?\s*(?:a\.?m|p\.?m)\b"
    if re.search(r"[kK]$", token.strip()):
        return (r"(?<![\d.])" + re.escape(d) + r"\s*(?:k\b|grand\b|thousand\b)|(?<![\d.])"
                + re.escape(d) + r",?000\b")
    if re.search(r"(?:m|M|bn|B)$", token.strip()):
        return r"(?<![\d.])" + re.escape(d) + r"\s*(?:m\b|mil|million|b\b|bn\b|billion)"
    return None


_ACRONYM_RE = re.compile(r"(?<![$\w])[A-Z]{2,5}(?![\w'])")
_STOP_ACRONYMS = {
    "AI", "CEO", "CFO", "ETF", "IPO", "ATH", "ATL", "YOLO", "DTE", "ODTE", "OTM", "ITM", "ATM",
    "EOD", "AH", "PM", "AM", "ET", "PT", "EST", "PST", "US", "USA", "UK", "EU", "LOL", "LMAO",
    "OK", "NFL", "NBA", "MLB", "NHL", "UFC", "PNL", "PL", "OCR", "GIF", "TV", "DM", "DMS", "ID",
    "IRL", "FOMO", "FUD", "IV", "OI", "EPS", "GDP", "CPI", "PCE", "FOMC", "FED", "SEC", "IRS",
    "DD", "TA", "VIX", "SPX", "SPY", "QQQ", "NDX", "RUT", "BTC", "ETH", "ER", "YTD", "WTF",
    "OMG", "IMO", "TBH", "NGL", "FR", "BRB", "RIP", "MAGA", "GOP", "DEI", "CNBC", "AF", "OP",
    "LLC", "OS", "HR", "PR", "NY", "LA", "SF", "DC", "MOASS", "WSB", "GG", "NPC", "POV", "FAQ",
}


def unsupported_terms(line: str, haystack: str, known: set[str]) -> list[str]:
    """Proper names in the writer's bracketed framing, and figures anywhere
    outside quotes, that the member never wrote. Names in the bullet's own
    head are left alone: there they paraphrase the quote beside them ("New
    Hampshire" for "NH", "North Korean" for "KJU"), and a dry run over the
    38 live profiles dropped those wrongly. A name at a sentence or clause
    start is skipped, as are member names."""
    text = _outside_quotes(line)
    framing = " ".join(re.findall(r"\[([^\]]*)\]", text))
    missing: list[str] = []
    for m in _TITLE_RE.finditer(framing):
        words = [w for w in m.groups() if w]
        before = framing[:m.start()].rstrip()
        if not before or before[-1] in "-.:;!?([—–+…\"“":
            words = words[1:]           # sentence or clause start
        for w in words:
            lw = w.lower()
            if lw in _STOP_TITLE or lw in known or _written(w, haystack):
                continue
            missing.append(w)
    for m in _ACRONYM_RE.finditer(text):
        # "works in IT", "NYC ski trips": none of it in his chat (run 7 M10)
        a = m.group(0)
        if a not in _STOP_ACRONYMS and a.lower() not in known and not re.search(
                r"\b" + re.escape(a.lower()) + r"\b", haystack):
            missing.append(a)
    for m in _NUMBER_RE.finditer(text):
        digits = re.sub(r"[^\d.]", "", m.group(1)).strip(".")
        # A figure with a unit is checked in the member's own form of it:
        # "$4k jacket", "wakes at 4 AM" were invented (2026-10-26 audit M8),
        # and "$16k" matched a stray 16 (run 7 M11).
        unit = _unit_form(m.group(1), text[m.end():m.end() + 6])
        if unit:
            if not re.search(unit, haystack.replace(",", "")):
                missing.append(m.group(1).strip())
            continue
        if len(digits.replace(".", "")) < 2:
            continue                    # "1", "5": too common to judge alone
        if re.fullmatch(r"(?:19|20)\d\d", digits):
            continue                    # a year is often arithmetic on the quote
        if not re.search(r"(?<![\d.])" + re.escape(digits) + r"(?!\d)", haystack.replace(",", "")):
            missing.append(m.group(1).strip())
    return missing


# The room's beliefs or reactions: the writer only sees this member's own
# messages, so these can never carry a receipt. 2026-10-26 audit M7 added
# the verbs that got through ("the room treats", "assumes", the room's
# "go-to"). Read outside quotes: the member's own words about the room stay.
_ROOM_CLAIM_RE = re.compile(
    r"\b(?:the room|the chat|the group|everyone in (?:here|the room|chat))(?:'s)?(?:\s+\w+){0,2}\s+"
    r"(?:accuses?|accused|knows?|knew|learned|ignores?|ignored|believes?|believed|treats?|treated"
    r"|assumes?|assumed|jokes?|joked|laughs?|laughed|mocks?|mocked|roasts?|roasted|clowns?|clowned"
    r"|expects?|expected|considers?|considered|sees|saw|views?|viewed|reacts?|reacted|respects?"
    r"|tolerates?|calls?|called|go-?to|favou?rite|punching\s+bag|running\s+joke|consensus"
    r"|standard\s+response|collective\s+(?:eye-?roll|groan|sigh|response)|has\s+learned)\b"
    r"|\bnobody\s+(?:cares|believes|listens)\b", re.I)


def _sections(profile: str) -> list[tuple[str, str]]:
    """(section name, line) for each line, section from the last **Header.**"""
    out, cur = [], ""
    for line in (profile or "").split("\n"):
        m = re.match(r"\s*\*\*([^*]+?)\.?\*\*", line)
        if m:
            cur = m.group(1).strip().rstrip(".")
        out.append((cur, line))
    return out


def apply(profile: str, user_id: int, verified_quotes: list[str] | None = None
          ) -> tuple[str, list[str]]:
    """The profile with failing bullets removed, and what was removed and why."""
    if not profile:
        return profile, []
    from datetime import datetime, timedelta, timezone
    msgs = member_messages(user_id)
    haystack = " \n ".join(m["norm"] for m in msgs)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d")
    known = _known_names()
    others: set[str] | None = None      # read once, on the first life bullet
    verified = {_norm(q) for q in (verified_quotes or [])}
    pasta_cache: dict[str, bool] = {}
    used: set[str] = set()
    keep, dropped = [], []
    for section, line in _sections(profile):
        if not line.lstrip().startswith("-") or any(s.lower() in section.lower() for s in _SKIP_SECTIONS):
            keep.append(line)
            continue
        line = strip_tag(line)              # re-derived below from the source
        sec = section.lower()
        reason = ""
        dq = [q for q in _DQ_RE.findall(line) if len(q.split()) >= 4]
        # every quote of two words or more is traced to its message
        sources = {q: find_source(q, msgs) for q in _DQ_RE.findall(line) if len(q.split()) >= 2}
        found = [(q, m, n) for q, (m, n) in sources.items() if m is not None]
        if "voice" in sec and _DQ_RE.findall(line) and not sources and "://" not in line:
            reason = "a one-word voice line"                         # run 7 M19
        if not reason and found and all(_norm(q) in used for q, _, _ in found):
            reason = f"repeats a quote used above: {found[0][0][:50]}"  # run 7 M18
        if not reason and "recent" in sec and found and all(
                m["posted_at"][:10] < cutoff for _, m, _ in found):
            # Recent life from messages two to five months old read as current
            # (2026-10-26 audit M6); run 7 M4 found the old four-word floor
            # let most of them through, so every traced quote counts now.
            reason = f"older than {RECENT_DAYS} days: {found[0][0][:50]}"
        if not reason and "personal life" in sec and found:
            if others is None:
                others = _other_names(user_id)
            if all(about_someone_else(m, user_id, others) for _, m, _ in found):
                reason = f"answers someone about someone else: {found[0][0][:50]}"
        if not reason and _VERDICT_RE.search(_outside_quotes(line)):
            reason = "rules on whether a claim was true"            # run 7 M2, M14
        # every quote of two words or more is checked against pastas: the
        # 10/14 kids pasta came back as a three-word quote (2026-10-26 M2)
        for q in [q for q in _DQ_RE.findall(line) if len(q.split()) >= 2 and not reason]:
            if q not in pasta_cache:
                pasta_cache[q] = is_copypasta(q, user_id)
            if pasta_cache[q]:
                reason = f"copypasta: {q[:50]}"
                break
        if not reason:
            sq = [q for q in _SQ_RE.findall(line) if len(q.split()) >= 2
                  and _norm(q) not in haystack]
            has_real_anchor = any(_norm(q) in verified or _norm(q) in haystack for q in dq)
            if sq and not has_real_anchor:
                reason = f"invented single-quoted anchor: {sq[0][:50]}"
        if not reason and re.search(r"(?:tenor\.com|giphy\.com|\.gif\b)", " ".join(_DQ_RE.findall(line)), re.I):
            reason = "a GIF link is not a quote"         # 2026-10-26 audit M10
        if not reason and _ROOM_CLAIM_RE.search(_outside_quotes(line)):
            reason = "room-reaction claim"
        if not reason and haystack:
            miss = unsupported_terms(line, haystack, known)
            if miss:
                reason = "not in their messages: " + ", ".join(miss[:4])
        if reason:
            dropped.append(f"{reason} | {line.strip()[:100]}")
            continue
        used |= {_norm(q) for q, _, _ in found}
        if found and sum(n for _, _, n in found) <= 1:
            # "constantly" from one message (run 7 M17): the word goes
            parts = re.split(r"([\"“][^\"“”\n]*[\"”])", line)
            line = "".join(p if i % 2 else _FREQ_RE.sub("", p) for i, p in enumerate(parts))
        if found and any(s in sec for s in _TAG_SECTIONS):
            newest = max((m for _, m, _ in found), key=lambda m: m["posted_at"])
            line = line.rstrip() + _tag(newest, user_id)
        keep.append(line)
    return "\n".join(keep), dropped


_NO_RECEIPTS_RE = re.compile(r"[^.!?\n]*\bno (?:documented )?receipts?\b[^.!?\n]*[.!?]?\s*", re.I)
_WIN_LINE_RE = re.compile(r"(?:gain|closed|exit)[^\n]{0,40}\+\d", re.I)


def fix_receipts_claim(rationale: str | None, profile: str) -> str | None:
    """Drop a "no documented receipts" sentence from the trader rationale
    when the profile's Recent trades list a winning close (audit M7: the
    two blocks used different windows and contradicted each other)."""
    if not rationale or not _NO_RECEIPTS_RE.search(rationale):
        return rationale
    trades = "\n".join(l for s, l in _sections(profile) if "recent trades" in s.lower())
    if not _WIN_LINE_RE.search(trades) and not re.search(r"\+\d+(?:\.\d+)?%", trades):
        return rationale
    return re.sub(r"\s{2,}", " ", _NO_RECEIPTS_RE.sub(" ", rationale)).strip() or rationale

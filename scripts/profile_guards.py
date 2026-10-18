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
trades section is grounded separately (scripts/profile_trades.py).
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


def _norm(s: str) -> str:
    s = (s or "").lower().replace("’", "'").replace("‘", "'")
    s = re.sub(r"[^\w\s$%'.]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def member_haystack(user_id: int, limit: int = 5000) -> str:
    """Everything the member wrote, with their screenshots' text, normalised."""
    rows = db.get_connection().execute(
        "SELECT content, image_ocr_text FROM chat_messages WHERE author_id = ? "
        "ORDER BY posted_at DESC LIMIT ?", (int(user_id), int(limit))).fetchall()
    return _norm(" \n ".join(f"{r[0] or ''} {r[1] or ''}" for r in rows))


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
    rows = conn.execute(
        "SELECT content FROM chat_messages WHERE author_id = ? AND content LIKE ? COLLATE NOCASE "
        "AND posted_at >= datetime('now', ?) LIMIT 3",
        (int(user_id), f"%{probe}%", f"-{int(days)} day")).fetchall()
    for (content,) in rows:
        if len(content or "") < _PASTA_MIN_CHARS:
            continue
        head = content.strip()[:60]
        n = conn.execute(
            "SELECT COUNT(DISTINCT author_id) FROM chat_messages WHERE content LIKE ? COLLATE NOCASE "
            "AND posted_at >= datetime('now', ?)", (f"{head}%", f"-{int(days)} day")).fetchone()
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
    for m in _NUMBER_RE.finditer(text):
        digits = re.sub(r"[^\d.]", "", m.group(1)).strip(".")
        if len(digits.replace(".", "")) < 2:
            continue                    # "1", "5": too common to judge
        if re.fullmatch(r"(?:19|20)\d\d", digits):
            continue                    # a year is often arithmetic on the quote
        if not re.search(r"(?<![\d.])" + re.escape(digits) + r"(?!\d)", haystack.replace(",", "")):
            missing.append(m.group(1).strip())
    return missing


_ROOM_CLAIM_RE = re.compile(
    r"\bthe room(?:'s)?(?:\s+\w+){0,2}\s+(?:accuses?|accused|knows?|knew|learned|ignores?|ignored"
    r"|standard\s+response|collective\s+(?:eye-?roll|groan|sigh|response)|has\s+learned|believes)\b"
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
    haystack = member_haystack(user_id)
    known = _known_names()
    verified = {_norm(q) for q in (verified_quotes or [])}
    pasta_cache: dict[str, bool] = {}
    keep, dropped = [], []
    for section, line in _sections(profile):
        if not line.lstrip().startswith("-") or any(s.lower() in section.lower() for s in _SKIP_SECTIONS):
            keep.append(line)
            continue
        reason = ""
        dq = [q for q in _DQ_RE.findall(line) if len(q.split()) >= 4]
        for q in dq:
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
        if not reason and _ROOM_CLAIM_RE.search(line):
            reason = "room-reaction claim"
        if not reason and haystack:
            miss = unsupported_terms(line, haystack, known)
            if miss:
                reason = "not in their messages: " + ", ".join(miss[:4])
        if reason:
            dropped.append(f"{reason} | {line.strip()[:100]}")
        else:
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

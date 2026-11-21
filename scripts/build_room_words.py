"""Rebuild assets/room_words.txt: listed symbols the room types as words.

The router takes a lowercase listed symbol as a ticker in a stock question
("how has rddt performed"), unless it is an English word
(assets/english_words.txt) or a word of the room's own: "tho", "ppl",
"ngl" and "cuz" are all listed symbols. The room's words are measured from
its chat, not kept by hand: a token typed in lowercase at least 5 times by
at least 2 members, and in capitals or as a cashtag less than a tenth as
often, is a word. "qqq" (1,228 lowercase, 250 capitals) stays a ticker;
"tho" (661, 3) is a word.

Runs on the worker against the live database, read-only:

    railway ssh "/opt/venv/bin/python scripts/build_room_words.py --since 2026-07-01" > assets/room_words.txt
"""
import argparse
import collections
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MIN_USES = 5
MIN_AUTHORS = 2
MAX_CAPS_RATIO = 0.1


def room_words(rows, listed, english) -> list[str]:
    """`rows`: (author_id, content) pairs. `listed(sym)` -> bool."""
    low, caps, authors = collections.Counter(), collections.Counter(), collections.defaultdict(set)
    for aid, txt in rows:
        if not txt:
            continue
        for m in re.finditer(r"(?<![$A-Za-z])([a-z]{3,5})(?![A-Za-z])", txt):
            low[m.group(1)] += 1
            authors[m.group(1)].add(aid)
        for m in re.finditer(r"\$?\b([A-Z]{3,5})\b", txt):
            caps[m.group(1)] += 1
    out = []
    for w, n in low.items():
        if (n >= MIN_USES and len(authors[w]) >= MIN_AUTHORS and w not in english
                and caps.get(w.upper(), 0) < MAX_CAPS_RATIO * n and listed(w.upper())):
            out.append(w)
    return sorted(out)


TICKER_MIN_LOW = 20        # rows kept in room_tickers.txt: a word typed this often,
TICKER_MIN_EVIDENCE = 3    # or typed as a ticker this often


def room_tickers(rows, listed, english) -> list[tuple[str, int, int]]:
    """(word, times typed in lowercase, times typed as a ticker) for listed
    symbols that are English words. Ticker evidence is a cashtag or the
    room's chart command ("fc amd"): capitals alone are also shouting
    ("NOW", "GO"). 2026-11-21: "has" is typed 1,570 times and never as a
    ticker, "mu" 331 times against 672 as a ticker.

        railway ssh "/opt/venv/bin/python scripts/build_room_words.py --since 2026-07-01 --tickers"
    """
    low, ev = collections.Counter(), collections.Counter()
    fc = re.compile(r"^\s*fc\s+([A-Za-z]{2,5})\b", re.I)
    for _aid, txt in rows:
        if not txt:
            continue
        m = fc.match(txt)
        if m:
            ev[m.group(1).lower()] += 1
            continue
        for m in re.finditer(r"(?<![$A-Za-z'])([a-z]{2,5})(?![A-Za-z'])", txt):
            low[m.group(1)] += 1
        for m in re.finditer(r"\$([A-Za-z]{2,5})\b", txt):
            ev[m.group(1).lower()] += 1
    return sorted((w, low[w], ev[w]) for w in set(low) | set(ev)
                  if w in english and listed(w.upper())
                  and (low[w] >= TICKER_MIN_LOW or ev[w] >= TICKER_MIN_EVIDENCE))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True, help="ISO date, e.g. 2026-07-01")
    ap.add_argument("--db", default="/data/reports.db")
    ap.add_argument("--tickers", action="store_true",
                    help="print assets/room_tickers.txt rows: English-word symbols with their counts")
    a = ap.parse_args()
    from discord_bot import listed_symbols as L
    c = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True, timeout=5)
    rows = c.execute("SELECT author_id, content FROM chat_messages WHERE posted_at >= ?", (a.since,))
    if a.tickers:
        for w, lo, ev in room_tickers(rows, lambda s: bool(L.is_listed(s)), L.english_words()):
            sys.stdout.write(f"{w} {lo} {ev}\n")
        return 0
    words = room_words(rows, lambda s: bool(L.is_listed(s)), L.english_words())
    sys.stdout.write("\n".join(words) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

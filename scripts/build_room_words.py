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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True, help="ISO date, e.g. 2026-07-01")
    ap.add_argument("--db", default="/data/reports.db")
    a = ap.parse_args()
    from discord_bot import listed_symbols as L
    c = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True, timeout=5)
    rows = c.execute("SELECT author_id, content FROM chat_messages WHERE posted_at >= ?", (a.since,))
    words = room_words(rows, lambda s: bool(L.is_listed(s)), L.english_words())
    sys.stdout.write("\n".join(words) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

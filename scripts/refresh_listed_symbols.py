"""Rebuild assets/listed_symbols.txt from Nasdaq's symbol directory.

The worker refreshes its own copy next to the database every day
(discord_bot/listed_symbols.py). The repository snapshot is what a fresh
process, a test run or a local session starts with; rebuild it when it is
more than a few months old.

    py -3.12 scripts/refresh_listed_symbols.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from discord_bot import listed_symbols as L  # noqa: E402


def main() -> int:
    rows = L.fetch()
    if len(rows) < L.MIN_ROWS:
        print(f"only {len(rows)} rows, not writing")
        return 1
    with open(L.SNAPSHOT, "w", encoding="utf-8", newline="\n") as f:
        f.write(L.serialize(rows))
    print(f"wrote {len(rows)} symbols to {L.SNAPSHOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

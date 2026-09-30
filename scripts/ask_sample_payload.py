"""Dump the /ask prefetch payloads for a ticker-opinion question as JSON,
for a fixture run of scripts/ask_fixture_run.py.

Owner review tool (2026-09-30). Run on the worker against a READ-ONLY
database connection:

  /opt/venv/bin/python scripts/ask_sample_payload.py MU

Prints one JSON object: {"lookup_research": ..., "lookup_market_price":
..., "lookup_earnings_date": ...}, the same payloads the router's
TICKER_OPINION prefetch would inject. Writes nothing.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    sym = argv[0].upper()
    import db
    from config import settings
    ro = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True, timeout=5)
    ro.row_factory = sqlite3.Row
    db.get_connection = lambda: ro
    from discord_bot.ask_tools import _execute_earnings_date, _execute_market_price
    from discord_bot.research_tool import _execute_research

    async def go():
        return {
            "lookup_research": await _execute_research({"symbol": sym, "days": 14}),
            "lookup_market_price": await _execute_market_price({"symbols": [sym]}),
            "lookup_earnings_date": await _execute_earnings_date({"symbol": sym}),
        }
    print(json.dumps(asyncio.run(go()), default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

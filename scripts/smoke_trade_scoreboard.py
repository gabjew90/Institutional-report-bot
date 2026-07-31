"""Smoke: the trade scoreboard reports HONEST win rates.

2026-07-30: a caller-performance chart shipped win rates of 96.2% /
100% / 100% / 91%. The model had computed
    wins / COUNT(gain_pct IS NOT NULL)
but gain_pct exists only where someone POSTED a close, and members
screenshot winners while silently abandoning losers — so the
denominator is winners by construction. Real ledger: Sam showed 100%
on 8 documented wins while 44 of his trades were opened and never
closed (15% once those count as losses); abe 96.6% -> 37.8%.

query_data's description already warned about the bias and the model
wrote the naive query anyway, so the honest math now lives in SQL as
the `trade_scoreboard` view, with column names that carry the caveat.
"""

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE pdf_files (id INTEGER PRIMARY KEY, file_name TEXT,
            dropbox_path TEXT, dropbox_modified_at TEXT,
            downloaded_at TEXT, status TEXT);
        CREATE TABLE pdf_analyses (id INTEGER PRIMARY KEY,
            pdf_file_id INTEGER, triage_json TEXT, analysis_json TEXT,
            priority TEXT, pages_analyzed INT, total_pages INT,
            input_tokens_used INT, output_tokens_used INT,
            model_used TEXT, analysis_duration_seconds REAL,
            created_at TEXT);
        CREATE TABLE analyst_trades (id INTEGER PRIMARY KEY,
            author TEXT, caller TEXT, is_trade INT, gain_pct REAL,
            ticker TEXT, posted_at TEXT);
    """)
    # "Sam": 8 documented wins, 0 posted losses, 44 never closed.
    rows = [("Sam", None, 1, 50.0)] * 8 + [("Sam", None, 1, None)] * 44
    c.executemany(
        "INSERT INTO analyst_trades (author, caller, is_trade, gain_pct) "
        "VALUES (?,?,?,?)", rows)
    c.commit()
    return path, c


def test_view_exposes_both_rates():
    import db as dbmod
    path, c = _db()
    try:
        dbmod._migrate_pdf_query_surface(c)
        r = c.execute(
            "SELECT logged_trades, documented_wins, never_closed, "
            "win_rate_BIASED_documented_only, "
            "win_rate_honest_ghosts_as_losses FROM trade_scoreboard"
        ).fetchone()
        logged, wins, ghosts, biased, honest = r
        assert logged == 52 and wins == 8 and ghosts == 44, r
        assert biased == 100.0, f"biased rate should be the fake 100%: {r}"
        assert 14.0 < honest < 16.0, (
            f"honest rate must count ghosts as losses (~15.4%): {r}"
        )
    finally:
        c.close(); os.remove(path)
    _ok("view exposes the fake 100% AND the honest ~15% side by side")


def test_column_names_carry_the_caveat():
    import db as dbmod
    path, c = _db()
    try:
        dbmod._migrate_pdf_query_surface(c)
        cols = [d[0] for d in c.execute(
            "SELECT * FROM trade_scoreboard LIMIT 1").description]
        assert "win_rate_BIASED_documented_only" in cols, cols
        assert "win_rate_honest_ghosts_as_losses" in cols, cols
        assert "avg_gain_on_wins_only" in cols, (
            "the average must say it's wins-only — it is not an "
            "expectancy"
        )
    finally:
        c.close(); os.remove(path)
    _ok("column names make the bias impossible to quote unknowingly")


def test_tool_points_at_the_view():
    import inspect
    import discord_bot.bot as bot
    decl = inspect.getsource(bot._build_query_data_tool)
    assert "trade_scoreboard" in decl, (
        "query_data must route win-rate questions to the view"
    )
    assert "win_rate_honest_ghosts_as_losses" in decl, (
        "tool must tell the model which rate to quote"
    )
    _ok("query_data routes performance questions to trade_scoreboard")


if __name__ == "__main__":
    print("=== trade scoreboard smoke ===")
    test_view_exposes_both_rates()
    test_column_names_carry_the_caveat()
    test_tool_points_at_the_view()
    print("\nALL TRADE SCOREBOARD SMOKE TESTS PASS")

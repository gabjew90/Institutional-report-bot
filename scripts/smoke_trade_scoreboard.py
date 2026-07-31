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
            author TEXT, author_id INTEGER, caller TEXT, is_trade INT,
            gain_pct REAL, ticker TEXT, action TEXT, posted_at TEXT);
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


def test_renames_do_not_split_a_trader():
    """2026-07-30, same day as the view shipped: grouping on the
    display name split ONE trader into three.

    This room renames constantly. Discord author_id 423994649317736448
    posts as 'BK', 'M&AK' and 'bearishkyle'; 1192771108332650496 posts
    as 'abe', 'abugs bunny', 'abullish_xyz', 'abearish' and more. The
    name-grouped view reported BK as three separate traders with 81 /
    73 / 21 trades instead of one with 184, so every per-trader number
    was understated. author_id is the stable key.
    """
    import db as dbmod
    path, c = _db()
    try:
        c.executemany(
            "INSERT INTO analyst_trades (author, author_id, is_trade, "
            "gain_pct, posted_at) VALUES (?,?,?,?,?)",
            # one human, three display names, 6 trades, 2 wins
            [("BK", 423994649317736448, 1, 10.0, "2026-05-01"),
             ("BK", 423994649317736448, 1, None, "2026-05-02"),
             ("bearishkyle", 423994649317736448, 1, 20.0, "2026-06-01"),
             ("bearishkyle", 423994649317736448, 1, None, "2026-06-02"),
             ("M&AK", 423994649317736448, 1, None, "2026-07-01"),
             ("M&AK", 423994649317736448, 1, -5.0, "2026-07-02")])
        c.commit()
        dbmod._migrate_pdf_query_surface(c)
        rows = c.execute(
            "SELECT trader, logged_trades, documented_wins "
            "FROM trade_scoreboard WHERE trader_key = ?",
            ("423994649317736448",)).fetchall()
        assert len(rows) == 1, (
            f"one human must be one row, got {len(rows)}: {rows}"
        )
        trader, logged, wins = rows[0]
        assert logged == 6 and wins == 2, (
            f"all three display names must roll up: {rows[0]}"
        )
        assert trader == "M&AK", (
            f"display name should be the most recent one, got {trader!r}"
        )
    finally:
        c.close(); os.remove(path)
    _ok("renames roll up to one trader via author_id")


def test_missing_author_id_still_counted():
    """885 of 887 prod trades carry author_id; the stragglers must not
    vanish from the board."""
    import db as dbmod
    path, c = _db()
    try:
        dbmod._migrate_pdf_query_surface(c)
        # the fixture's 52 "Sam" rows have author_id NULL
        r = c.execute(
            "SELECT trader_key, logged_trades FROM trade_scoreboard"
        ).fetchall()
        assert r == [("Sam", 52)], (
            f"rows without author_id must fall back to the name: {r}"
        )
    finally:
        c.close(); os.remove(path)
    _ok("trades lacking author_id fall back to the name, not dropped")


def test_unscored_closes_are_not_called_never_closed():
    """2026-07-30: `never_closed` was SUM(gain_pct IS NULL), which
    lumps two different things together.

    179 of the room's 431 closes carry no percentage — the member wrote
    "sold DELL way too early smh" or "Im taking all 7:31 113c" and the
    extractor had no number to parse. Those are CLOSED positions that
    happen to be unscored. Counting them as "never closed" says the
    trade is still open, which is simply false.

    (Price-scoring them was the obvious repair and it does not work:
    only 4 of 96 priced closes can be matched to a prior open that also
    carries a price, because opens rarely record one. So the buckets get
    told apart honestly instead of being guessed at.)
    """
    import db as dbmod
    path, c = _db()
    try:
        c.executemany(
            "INSERT INTO analyst_trades (author, author_id, is_trade, "
            "action, gain_pct, posted_at) VALUES (?,?,1,?,?,?)",
            [("z", 900, "open",  None, "2026-06-01"),   # truly open
             ("z", 900, "open",  None, "2026-06-02"),   # truly open
             ("z", 900, "close", None, "2026-06-03"),   # closed, unscored
             ("z", 900, "close", 12.0, "2026-06-04")])  # closed, scored
        c.commit()
        dbmod._migrate_pdf_query_surface(c)
        r = c.execute(
            "SELECT logged_trades, documented_wins, closed_unscored, "
            "never_closed FROM trade_scoreboard WHERE trader_key='900'"
        ).fetchone()
        logged, wins, unscored, ghosts = r
        assert logged == 4 and wins == 1, r
        assert unscored == 1, (
            f"the numberless close must land in its own bucket: {r}"
        )
        assert ghosts == 2, (
            f"never_closed must count ONLY positions with no close "
            f"posted at all, got {ghosts} (the unscored close leaked "
            f"in): {r}"
        )
    finally:
        c.close(); os.remove(path)
    _ok("unscored closes separated from genuinely open positions")


def test_three_rates_bracket_the_truth():
    """Neither existing rate is honest on its own.

    win_rate_BIASED_documented_only divides by scored closes, so it only
    ever sees exits the member chose to post with a number attached —
    that is how 96-100% happens. win_rate_honest_ghosts_as_losses
    divides by EVERY row, which calls a position announced this morning
    a loss. The truth is bracketed by them, not equal to either.

    The middle rate divides by positions that are actually closed
    (scored + unscored), so a posted-but-numberless exit counts against
    you while a trade still running does not.

    Ghosts stay visible in never_closed — excluded from this rate, not
    hidden. For options most ghosts are probably expirations, i.e. real
    losses, so this rate is generous. Quote it WITH never_closed.
    """
    import db as dbmod
    path, c = _db()
    try:
        c.executemany(
            "INSERT INTO analyst_trades (author, author_id, is_trade, "
            "action, gain_pct, posted_at) VALUES (?,?,1,?,?,?)",
            [("z", 901, "open",  None, "2026-06-01"),   # still running
             ("z", 901, "open",  None, "2026-06-02"),   # still running
             ("z", 901, "close", None, "2026-06-03"),   # closed, unscored
             ("z", 901, "close", 12.0, "2026-06-04")])  # closed, won
        c.commit()
        dbmod._migrate_pdf_query_surface(c)
        biased, closed_only, harsh = c.execute(
            "SELECT win_rate_BIASED_documented_only, "
            "win_rate_closed_positions_only, "
            "win_rate_honest_ghosts_as_losses "
            "FROM trade_scoreboard WHERE trader_key='901'").fetchone()
        assert biased == 100.0, f"1 win / 1 scored close: {biased}"
        assert closed_only == 50.0, (
            f"1 win / 2 closed positions — the unscored close must "
            f"count against, got {closed_only}"
        )
        assert harsh == 25.0, f"1 win / 4 rows: {harsh}"
        assert biased > closed_only > harsh, (
            f"the three rates must bracket: {biased} > {closed_only} > "
            f"{harsh}"
        )
    finally:
        c.close(); os.remove(path)
    _ok("three rates bracket the truth; open positions aren't losses")


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
    test_renames_do_not_split_a_trader()
    test_missing_author_id_still_counted()
    test_unscored_closes_are_not_called_never_closed()
    test_three_rates_bracket_the_truth()
    test_tool_points_at_the_view()
    print("\nALL TRADE SCOREBOARD SMOKE TESTS PASS")

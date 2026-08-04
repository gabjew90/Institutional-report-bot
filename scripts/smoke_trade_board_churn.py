"""Smoke: the TRADE BOARD owns its churn and its lineage.

2026-08-04 review of the week's pulses: the SMH/SOXX complex flipped
direction FIVE times in seven sessions (puts -> short SOXX -> long SMH
spreads -> short SMH -> long SMH), and 08-04's FLIP never mentioned it
was the third reversal in a week. Each flip read fine alone; a follower
got chopped. Separately, 08-04 claimed "held since Jul 29: Long $MSFT,
$GOOGL" — the Jul 29 entry was MSFT calls on dealer-hedging mechanics
and GOOGL had never been on any board: the held-since label silently
inherited a different trade's lineage.

Contract:
- db.count_recent_reversals(instrument, today) counts direction changes
  across that instrument's lean history in a rolling window.
- A FLIP that is the 2nd+ reversal in the window renders the count on
  the board — the reader sees the chop, not just today's conviction.
- A re-affirmed lean whose instrument set CHANGED keeps its date but
  says so, instead of letting new tickers inherit the old lineage.
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _db():
    import db as dbmod
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row  # matches production get_connection
    dbmod._init_schema(conn)
    orig = dbmod.get_connection
    dbmod.get_connection = lambda: conn
    return dbmod, conn, orig


def test_reversal_count():
    dbmod, conn, orig = _db()
    try:
        # SMH week: long (07-27) -> short (07-28) -> long (07-30)
        # -> short (08-03) -> long (08-04): 4 reversals.
        seq = [("2026-07-27", "long"), ("2026-07-28", "short"),
               ("2026-07-30", "long"), ("2026-08-03", "short"),
               ("2026-08-04", "long")]
        for d, direction in seq:
            conn.execute(
                "INSERT INTO pulse_leans (instrument, direction, "
                "first_seen_date, last_seen_date, context_snippet, status) "
                "VALUES ('SMH', ?, ?, ?, 'x', 'superseded')",
                (direction, d, d))
        conn.commit()
        n = dbmod.count_recent_reversals("SMH", "2026-08-04")
        assert n == 4, f"expected 4 direction changes, got {n}"
        assert dbmod.count_recent_reversals("GLD", "2026-08-04") == 0
    finally:
        dbmod.get_connection = orig
        conn.close()
    _ok("count_recent_reversals counts direction changes in the window")


def test_flip_renders_reversal_count():
    from report.pulse_sections import render_trade_board
    rows = [{
        "instrument": "SMH", "direction": "long",
        "first_seen_date": "2026-08-04", "last_seen_date": "2026-08-04",
        "context_snippet": "Long $SMH — staged into weakness",
    }]
    md = render_trade_board(
        rows, "2026-08-04", flips={"SMH"}, hc_calls=[],
        prev_board_date="2026-08-03",
        reversal_counts={"SMH": 4},
    )
    assert "FLIP" in md, md
    assert "4th reversal" in md, (
        f"a FLIP that is the 4th direction change in the window must "
        f"say so on the board:\n{md}"
    )
    # A first flip stays clean — no scare annotation.
    md1 = render_trade_board(
        rows, "2026-08-04", flips={"SMH"}, hc_calls=[],
        prev_board_date="2026-08-03",
        reversal_counts={"SMH": 1},
    )
    assert "reversal" not in md1, f"first flip must not be annotated:\n{md1}"
    _ok("repeat flips carry their reversal count; first flips stay clean")


def test_instrument_widening_breaks_silent_lineage():
    dbmod, conn, orig = _db()
    try:
        # Jul 29: MSFT-only lean.
        dbmod.upsert_pulse_leans("2026-07-29", [{
            "instrument": "MSFT", "direction": "long",
            "context": "Long $MSFT — dealer hedging above 390",
        }])
        # Aug 4: same primary, but now claims $GOOGL too.
        dbmod.upsert_pulse_leans("2026-08-04", [{
            "instrument": "MSFT", "direction": "long",
            "context": "Long $MSFT, $GOOGL — self-funded capex",
        }])
        row = conn.execute(
            "SELECT first_seen_date, context_snippet FROM pulse_leans "
            "WHERE instrument='MSFT' AND status='live'").fetchone()
        assert row[0] == "2026-07-29", "lineage date itself is kept"
        assert "instruments updated" in row[1], (
            f"a widened instrument set must be visible — $GOOGL "
            f"inherited 'held since Jul 29' silently: {row[1]!r}"
        )
        # Re-affirming with the SAME set must NOT accumulate markers.
        dbmod.upsert_pulse_leans("2026-08-05", [{
            "instrument": "MSFT", "direction": "long",
            "context": "Long $MSFT, $GOOGL — self-funded capex",
        }])
        row2 = conn.execute(
            "SELECT context_snippet FROM pulse_leans "
            "WHERE instrument='MSFT' AND status='live'").fetchone()
        assert row2[0].count("instruments updated") <= 1, row2[0]
    finally:
        dbmod.get_connection = orig
        conn.close()
    _ok("widened instrument sets are marked, not silently inherited")


def test_hc_calls_require_a_rating_or_pt():
    """2026-08-04 review: the "high-conviction single-name calls"
    subsection mixed real ratings with post-hoc RECAPS — TME's "$META —
    Earnings miss ... driving a sharp sell-off" and JPM's "$RBLX —
    Bookings miss, poor Q3 guidance" are descriptions of what already
    happened, not calls anyone can act on. A real call carries a rating
    (Buy/OW/UW) or a price target; recaps carry neither."""
    import json as _json
    from report.pulse_sections import extract_state_from_ctx
    ctx = {"theme_map": {}, "analyses_json": _json.dumps([
        {"source": "Goldman Sachs", "market_movers": [
            {"ticker": "MSFT", "conviction": "high", "action": "Buy",
             "rating": "Buy", "price_target": "$640",
             "rationale": "Azure acceleration"},
        ]},
        {"source": "The Market Ear", "market_movers": [
            {"ticker": "META", "conviction": "high", "action": "",
             "rating": "", "price_target": "",
             "rationale": "Earnings miss and rising capex are driving "
                          "a sharp sell-off in the stock."},
        ]},
        {"source": "JPMorgan", "market_movers": [
            {"ticker": "SIMO", "conviction": "high", "action": "",
             "rating": "", "price_target": "PT up by $90",
             "rationale": "Strong results, PCIe opportunity"},
        ]},
    ])}
    calls = extract_state_from_ctx(ctx)["hc_calls"]
    tickers = {c["ticker"] for c in calls}
    assert "MSFT" in tickers, f"rated call must survive: {tickers}"
    assert "SIMO" in tickers, f"PT-carrying call must survive: {tickers}"
    assert "META" not in tickers, (
        f"a recap with no rating and no PT is not a call: {tickers}"
    )
    _ok("HC subsection keeps rated/PT calls, drops post-hoc recaps")


if __name__ == "__main__":
    print("=== trade-board churn + lineage smoke ===")
    test_reversal_count()
    test_flip_renders_reversal_count()
    test_instrument_widening_breaks_silent_lineage()
    test_hc_calls_require_a_rating_or_pt()
    print("\nALL TRADE-BOARD CHURN SMOKE TESTS PASS")

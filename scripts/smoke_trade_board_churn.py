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
    # 2026-08-12 (owner decision): the board carries today's calls with
    # no status labels, so the churn annotation no longer renders.
    # count_recent_reversals is unchanged and still covered above — the
    # churn signal remains queryable for anything that wants it.
    assert "FLIP" not in md, f"FLIP label should be retired:\n{md}"
    assert "reversal" not in md, f"churn annotation should be retired:\n{md}"
    assert "$SMH" in md, f"the lean itself must still render:\n{md}"
    # The reversal_counts argument is now inert: same output either way.
    md1 = render_trade_board(
        rows, "2026-08-04", flips={"SMH"}, hc_calls=[],
        prev_board_date="2026-08-03",
        reversal_counts={"SMH": 1},
    )
    assert md1 == md, "reversal_counts must no longer change the render"
    _ok("churn annotation retired from the board; counts still computed")


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


def test_na_rating_is_not_a_rating():
    """2026-08-05: the SPCX inaugural-earnings miss. Gemini extracts
    literal 'N/A' strings for rating/pt on recap-shaped movers; 'N/A'
    is truthy, so the rating-or-PT recap filter passed three junk
    entries ("GS $MSFT — Blowout earnings and a +15% surge", no rating,
    no PT) into the capped HC list."""
    import json as _json
    from report.pulse_sections import extract_state_from_ctx
    ctx = {"theme_map": {}, "analyses_json": _json.dumps([
        {"source": "Goldman Sachs", "market_movers": [
            {"ticker": "MSFT", "conviction": "high",
             "action": "positive_catalyst_watch", "rating": "N/A",
             "price_target": "N/A", "rationale": "Blowout earnings"},
        ]},
    ])}
    calls = extract_state_from_ctx(ctx)["hc_calls"]
    assert not calls, f"'N/A' must count as no rating/PT: {calls}"
    _ok("'N/A' rating/pt entries are recaps and get dropped")


def test_hc_cap_ranks_by_action_not_arrival_order():
    """2026-08-05: GS published a dedicated HIGH note on SpaceX's FIRST
    EVER earnings (Buy, PT $220) — analyzed 3h before the pulse — and
    the board cut it: calls[:6] sliced in extraction order and SPCX
    arrived 13th. A PT change / upgrade must outrank catalyst-watch
    reiterations regardless of when the PDF happened to be analyzed."""
    import json as _json
    from report.pulse_sections import extract_state_from_ctx, _HC_SUBSECTION_MAX
    movers = []
    # 6 low-signal catalyst-watch entries arrive FIRST...
    for i in range(_HC_SUBSECTION_MAX):
        movers.append({"ticker": f"AAA{chr(66 + i)}", "conviction": "high",
                       "action": "positive_catalyst_watch",
                       "rating": "Overweight", "price_target": "",
                       "rationale": "strong results expected"})
    analyses = [{"source": f"Bank{i}", "market_movers": [m]}
                for i, m in enumerate(movers)]
    # ...the marquee PT-change call arrives LAST.
    analyses.append({"source": "Goldman Sachs", "market_movers": [
        {"ticker": "SPCX", "conviction": "high",
         "action": "price_target_change", "rating": "Buy",
         "price_target": "$220",
         "rationale": "Strong inaugural earnings beat"},
    ]})
    ctx = {"theme_map": {}, "analyses_json": _json.dumps(analyses)}
    calls = extract_state_from_ctx(ctx)["hc_calls"]
    top = [c["ticker"] for c in calls[:_HC_SUBSECTION_MAX]]
    assert "SPCX" in top, (
        f"a rating+PT change must survive the cap over catalyst-watch "
        f"reiterations; top-{_HC_SUBSECTION_MAX} = {top}"
    )
    _ok("HC cap keeps rating/PT actions over catalyst-watch arrivals")


if __name__ == "__main__":
    print("=== trade-board churn + lineage smoke ===")
    test_reversal_count()
    test_flip_renders_reversal_count()
    test_instrument_widening_breaks_silent_lineage()
    test_hc_calls_require_a_rating_or_pt()
    test_na_rating_is_not_a_rating()
    test_hc_cap_ranks_by_action_not_arrival_order()
    print("\nALL TRADE-BOARD CHURN SMOKE TESTS PASS")

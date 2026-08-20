"""Smoke: the TRADE BOARD carries only trades a desk explicitly called.

2026-08-12 owner decision. The board had become a status ledger: a
"held since <date>" line for every live position, "off board since"
exit rows with move scoring, FLIP and RE-ENTRY labels, and a six-line
legend defining all of it reprinted verbatim every pulse. That day's
board was eight rows of repeats and exits, zero new calls, under a
legend longer than the content.

Then a second decision the same day: the pulse's OWN synthesized leans
came off the board too. "Let's not make up longs, let's only do it when
explicitly called." What remains is one list of calls a named desk
published — rating, usually a price target, their reasoning. No legend,
no status prefixes, no house leans.

That also killed a duplication: 2026-08-11 carried both "Long $AMAT"
(ours, synthesized) and "Bank of America $AMAT · Buy, PT $720" (theirs)
as though they were two separate calls.

This file replaces the rendering assertions in smoke_board_dropped,
smoke_thesis_flip and smoke_trade_board_churn. The DETECTION those files
cover (lineage, thesis flips, reversal counts, prev_seen_date) is
untouched and still unit-tested there — it just no longer renders.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _row(inst, first, last, prev=None, ctx=None, direction="long"):
    return {"instrument": inst, "direction": direction,
            "first_seen_date": first, "last_seen_date": last,
            "prev_seen_date": prev,
            "context_snippet": ctx or f"Long ${inst} · thesis"}


HC = [{"source": "Deutsche Bank", "ticker": "DASH", "action": "reiterate",
       "pt": "$260", "rating": "Buy",
       "rationale": "Strong execution and accelerating flywheel."}]


def _board(rows, hc=None, today="2026-08-12", prev="2026-08-11"):
    from report.pulse_sections import render_trade_board
    return render_trade_board(board_rows=rows, today=today, hc_calls=hc,
                              prev_board_date=prev)


def test_house_leans_never_render():
    """The core of the second decision. A synthesized lean is the pulse's
    own view assembled across the corpus, not a call anyone published."""
    md = _board([
        _row("MU", "2026-08-12", "2026-08-12", ctx="Long $MU · new today"),
        _row("BNO", "2026-08-06", "2026-08-12", prev="2026-08-11",
             ctx="Long $BNO · carried from Aug 6"),
    ], hc=HC)
    if "$MU" in md:
        _fail(f"a house lean rendered — we do not make up longs:\n{md}")
    if "$BNO" in md:
        _fail(f"a carried house lean rendered:\n{md}")
    if "$DASH" not in md:
        _fail(f"the explicitly-called desk trade is missing:\n{md}")
    _ok("house leans never render; the desk call does")


def test_the_amat_duplication_is_gone():
    """2026-08-11 carried 'Long $AMAT' (ours) and 'Bank of America $AMAT ·
    Buy, PT $720' (theirs) as if they were two calls."""
    md = _board(
        [_row("AMAT", "2026-08-11", "2026-08-11",
              ctx="Long $AMAT · into Thursday print")],
        hc=[{"source": "Bank of America", "ticker": "AMAT",
             "action": "reiterate", "rating": "Buy", "pt": "$720",
             "rationale": "Constructive into earnings."}],
        today="2026-08-11", prev="2026-08-10")
    bullets = [l for l in md.splitlines() if l.startswith("- ")]
    if len(bullets) != 1:
        _fail(f"$AMAT should appear once, as BofA's call: {bullets}")
    if "Bank of America" not in bullets[0]:
        _fail(f"the surviving row is not the desk call: {bullets[0]}")
    _ok("no more house-lean/desk-call duplication on one ticker")


def test_no_status_labels():
    md = _board([_row("MU", "2026-08-12", "2026-08-12")], hc=HC)
    for label in ("NEW", "FLIP", "RE-ENTRY", "held since", "off board"):
        if label in md:
            _fail(f"status label {label!r} still renders:\n{md}")
    _ok("no NEW / FLIP / RE-ENTRY / held / off-board labels")


def test_no_legend():
    md = _board([_row("MU", "2026-08-12", "2026-08-12")], hc=HC)
    for phrase in ("first flagged today", "reverses a view",
                   "Leans this pulse is making",
                   "is scored where price data exists"):
        if phrase in md:
            _fail(f"legend text survives: {phrase!r}")
    _ok("the repeated legend is gone")


def test_desk_calls_have_no_separate_header():
    md = _board([], hc=HC)
    if "High-conviction single-name calls" in md:
        _fail(f"desk calls still render under their own header:\n{md}")
    bullets = [l for l in md.splitlines() if l.startswith("- ")]
    if len(bullets) != 1 or "$DASH" not in bullets[0]:
        _fail(f"expected one desk call in one list:\n{md}")
    _ok("desk calls render as one plain list, no sub-header")


def test_rating_legend_survives():
    """Short, conditional, and it exists because OW/UW once shipped with
    no decode (2026-07-15). Not the boilerplate that was cut."""
    md = _board([], hc=HC)
    if "PT = price target" not in md:
        _fail(f"rating decode was lost:\n{md}")
    _ok("the conditional rating decode still renders")


def test_no_desk_calls_renders_nothing():
    """No desk called anything today, so there is no board. Say nothing
    rather than fall back to house views to fill it."""
    md = _board([_row("BNO", "2026-08-06", "2026-08-12", prev="2026-08-11"),
                 _row("MU", "2026-08-12", "2026-08-12")], hc=[])
    if md.strip():
        _fail(f"board rendered with no explicit desk calls:\n{md}")
    _ok("no desk calls renders empty; house leans do not fill the gap")


def test_desk_calls_alone_render():
    md = _board([], hc=HC)
    if "$DASH" not in md or "TRADE BOARD" not in md:
        _fail(f"desk calls alone did not render:\n{md}")
    _ok("desk calls are the whole board")


def test_tracking_is_unchanged():
    """The rewrite is presentation-only. Lineage must still be recorded
    so re-enabling any label family is a rendering change."""
    import sqlite3
    import db as dbmod
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    dbmod._init_schema(conn)
    dbmod._conn = conn
    lean = [{"instrument": "MU", "direction": "long", "context": "Long $MU"}]
    dbmod.upsert_pulse_leans("2026-08-07", lean)
    dbmod.upsert_pulse_leans("2026-08-12", lean)
    r = conn.execute(
        "SELECT first_seen_date, prev_seen_date, last_seen_date "
        "FROM pulse_leans WHERE instrument='MU'").fetchone()
    if (r["first_seen_date"], r["prev_seen_date"], r["last_seen_date"]) != (
            "2026-08-07", "2026-08-07", "2026-08-12"):
        _fail(f"lineage tracking changed: {dict(r)}")
    _ok("pulse_leans still records full lineage behind the scenes")


def test_board_voice_sanitation():
    """2026-08-20: published boards carried semicolons in rationales and
    a 'The Market Ear' source line. Board text is restored verbatim
    after lint/SCRUB, so the renderer is the only enforcement point:
    banned-publication sources are dropped, semicolons and em-dashes in
    rationale/rating strings are rewritten."""
    hc = [
        {"source": "The Market Ear", "ticker": "META", "action": "",
         "pt": "", "rating": "OW",
         "rationale": "Relay of Morgan Stanley's top pick."},
        {"source": "Goldman Sachs", "ticker": "EOAN", "action": "",
         "pt": "", "rating": "Buy",
         "rationale": "Key GS long; weakness — a buying opportunity."},
        {"source": "UBS", "ticker": "DTEGY", "action": "",
         "pt": "", "rating": "Most Preferred",
         "rationale": "Best-in-class operator."},
    ]
    md = _board([], hc=hc)
    if "Market Ear" in md:
        _fail(f"banned publication survived on the board: {md}")
    if "$META" in md:
        _fail("banned-source call itself must be dropped, not re-attributed")
    if ";" in md or "—" in md:
        _fail(f"semicolon/em-dash survived board sanitation: {md}")
    if "Most Preferr " in md or "Most Preferr·" in md:
        _fail(f"mid-word rating stub on board: {md}")
    if "$EOAN" not in md or "$DTEGY" not in md:
        _fail(f"legit calls were over-filtered: {md}")
    _ok("board: banned sources dropped, semicolons/em-dashes rewritten")


if __name__ == "__main__":
    test_house_leans_never_render()
    test_the_amat_duplication_is_gone()
    test_no_status_labels()
    test_no_legend()
    test_desk_calls_have_no_separate_header()
    test_rating_legend_survives()
    test_no_desk_calls_renders_nothing()
    test_desk_calls_alone_render()
    test_tracking_is_unchanged()
    test_board_voice_sanitation()
    print("\nAll new-calls-only board smoke tests passed.")

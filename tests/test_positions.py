"""The trade log read as positions (analyst_log/positions.py).

2026-11-11 audit, run 11: Abe's 13 winning closes and 6 unclosed opens were
reported to the room as "13 wins / 11 losses". The rows below are his
caller log for the 30 days to 2026-11-11 14:19 UTC (contract, action,
price, gain and time only).
"""
import pathlib
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import db  # noqa: E402,F401
from analyst_log import positions as P  # noqa: E402

TODAY = date(2026, 11, 11)

# (posted_at, ticker, type, strike, expiry, action, price, gain_pct)
ABE = [
    ("2026-10-15T13:32", "PLTR", "put", 200, "2026-10-16", "close", None, 400.0),
    ("2026-10-15T13:33", "TSM", "put", 400, "2026-10-16", "close", None, 66.67),
    ("2026-10-15T15:05", "PLTR", "call", 200, "2026-10-16", "close", None, 72.27),
    ("2026-10-21T19:19", "GLW", "call", 140, "2026-10-30", "open", 4.73, None),
    ("2026-10-22T15:06", "LULU", "call", 105, "2026-10-30", "open", 1.07, None),
    ("2026-10-26T15:05", "NBIS", "call", 170, "2026-11-06", "open", 8.1, None),
    ("2026-10-26T19:57", "AMD", "call", 570, "2026-10-30", "open", 2.85, None),
    ("2026-10-27T13:37", "AMD", "call", 570, "2026-10-30", "close", 6.0, 110.53),
    ("2026-10-28T14:58", "AVGO", "call", 375, "2026-11-06", "open", 1.615, None),
    ("2026-10-28T18:03", "LULU", "call", 105, "2026-10-30", "close", 2.08, 94.39),
    ("2026-10-28T18:28", "ORCL", "call", 140, "2026-10-30", "close", None, 157.96),
    ("2026-10-28T18:41", "ORCL", "call", 140, None, "close", None, 384.08),
    ("2026-10-28T18:41", "NBIS", "call", 170, "2026-11-06", "close", 16.75, 106.79),
    ("2026-10-29T13:34", "ORCL", "call", 150, "2026-11-06", "open", 2.245, None),
    ("2026-10-29T14:39", "MSTR", "call", 150, "2026-10-30", "open", 1.54, None),
    ("2026-10-30T19:10", "TSLA", "call", 410, "2026-11-06", "open", 5.7, None),
    ("2026-10-30T19:25", "SPY", "call", 800, "2026-12-18", "open", 2.02, None),
    ("2026-11-02T14:32", "TSLA", "call", 410, "2026-11-06", "close", 10.0, 75.44),
    ("2026-11-02T14:36", "PLTR", "put", 150, "2026-11-06", "open", 1.68, None),
    ("2026-11-02T18:21", "FSLR", "call", 215, "2026-11-20", "open", 1.79, None),
    ("2026-11-03T13:17", "TSLA", "call", 410, "2026-11-04", "open", 4.63, None),
    ("2026-11-04T14:33", "FSLR", "call", 215, "2026-11-20", "close", 8.05, 349.72),
    ("2026-11-05T15:32", "PLTR", "call", 160, "2026-11-06", "open", 2.16, None),
    ("2026-11-05T16:20", "TSM", "call", 400, "2026-11-20", "open", 3.13, None),
    ("2026-11-06T14:32", "TSM", "call", 400, "2026-11-20", "trim", 6.9, 120.45),
    ("2026-11-06T14:49", "PLTR", "call", 160, "2026-11-06", "close", 4.05, 87.5),
    ("2026-11-09T14:58", "TSM", "call", 400, "2026-11-20", "close", 9.4, 200.32),
    ("2026-11-09T15:04", "CRWD", "call", 240, "2026-11-20", "open", 4.375, None),
    ("2026-11-09T15:09", "META", "call", 680, "2026-11-18", "open", 4.33, None),
    ("2026-11-09T20:16", "DASH", "call", 200, "2026-11-20", "open", 1.56, None),
    ("2026-11-09T20:21", "GOOGL", "call", 300, "2026-11-18", "open", 2.315, None),
    ("2026-11-09T20:51", "MRNA", "put", 260, "2026-11-20", "open", 13.4, None),
    ("2026-11-10T18:04", "MRNA", "put", 260, "2026-11-20", "close", 18.0, 34.33),
]
COLS = ("posted_at", "ticker", "contract_type", "strike", "expiry", "action", "price", "gain_pct")


def _rows(src=ABE):
    return [dict(zip(COLS, r), id=i + 1) for i, r in enumerate(src)]


def test_a_closed_winner_is_not_also_an_unclosed_open():
    pos = P.build(_rows(), TODAY)
    unclosed = sorted((p["ticker"], p["strike"], p["expiry"]) for p in pos
                      if p["status"] == "expired_no_exit")
    assert unclosed == [
        ("AVGO", 375.0, "2026-11-06"), ("GLW", 140.0, "2026-10-30"),
        ("MSTR", 150.0, "2026-10-30"), ("ORCL", 150.0, "2026-11-06"),
        ("PLTR", 150.0, "2026-11-06"), ("TSLA", 410.0, "2026-11-04"),
    ]
    by = {(p["ticker"], p["strike"], (p["contract_type"] or "")[:1]): p for p in pos
          if p["status"] == "closed"}
    for key in [("LULU", 105.0, "c"), ("NBIS", 170.0, "c"), ("AMD", 570.0, "c"), ("PLTR", 160.0, "c")]:
        assert by[key]["entry"] is not None and by[key]["exits"]
    still_open = sorted(p["ticker"] for p in pos if p["status"] == "open")
    assert still_open == ["CRWD", "DASH", "GOOGL", "META", "SPY"]


def test_a_reopened_contract_is_two_positions():
    """TSLA 410C: opened, closed Nov 2 for a win, opened again Nov 3 with a
    Nov 4 expiry and never closed. One win and one unclosed open."""
    tsla = [p for p in P.build(_rows(), TODAY) if p["ticker"] == "TSLA"]
    assert [p["status"] for p in tsla] == ["closed", "expired_no_exit"]


def test_trims_and_the_close_are_one_position():
    tsm = [p for p in P.build(_rows(), TODAY) if p["key"] == ("TSM", 400.0, "c")]
    assert len(tsm) == 1 and [e["action"] for e in tsm[0]["exits"]] == ["trim", "close"]
    assert P.dollars_per_contract(tsm[0], tsm[0]["exits"][-1], 200.32) == 627.0


def test_dollars_come_from_the_logged_prices():
    pos = {p["key"]: p for p in P.build(_rows(), TODAY)}
    fslr = pos[("FSLR", 215.0, "c")]
    # 1.79 to 8.05: the 11-11 answer said +$2,273 for two contracts, from
    # an assumed $3.25 entry. The log gives $626 a contract.
    assert P.dollars_per_contract(fslr, fslr["exits"][-1], 349.72) == 626.0
    # no entry logged: no dollar figure, however large the gain
    pltr = pos[("PLTR", 200.0, "p")]
    assert P.dollars_per_contract(pltr, pltr["exits"][-1], 400.0) is None


def test_one_post_read_twice_is_one_event_and_stays_in_the_position():
    """Two screenshots of one MCD close in one message (2026-11-04) were
    stored as two +1,497% closes."""
    rows = [dict(id=1, ticker="MCD", contract_type="call", strike=280, expiry="2026-11-20",
                 action="close", price=None, gain_pct=1497.44, posted_at="2026-11-04T19:54:01"),
            dict(id=2, ticker="MCD", contract_type="call", strike=280, expiry="2026-11-20",
                 action="close", price=None, gain_pct=1497.44, posted_at="2026-11-04T19:54:01")]
    pos = P.build(rows, TODAY)
    assert len(pos) == 1 and len(pos[0]["exits"]) == 1 and len(pos[0]["rows"]) == 2
    assert len(P.distinct_trades(rows)) == 1
    assert set(P.position_of(pos)) == {1, 2}


def test_only_opens_of_unclosed_positions_carry_the_expired_mark():
    rows = _rows()
    ids = P.should_be_marked_expired(rows, TODAY)
    marked = sorted((r["ticker"], r["strike"], r["action"]) for r in rows if r["id"] in ids)
    assert marked == [("AVGO", 375, "open"), ("GLW", 140, "open"), ("MSTR", 150, "open"),
                      ("ORCL", 150, "open"), ("PLTR", 150, "open"), ("TSLA", 410, "open")]
    # the TSLA row marked is the Nov 3 re-entry, not the Oct 30 open that closed
    assert [r["posted_at"] for r in rows if r["id"] in ids and r["ticker"] == "TSLA"] == ["2026-11-03T13:17"]


# ---- the readers, against a database --------------------------------------

CALLER = "postest"


def _insert(conn, src, shift_days: int):
    """Abe's rows as caller `postest`, moved so the last one is today."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(analyst_trades)")}
    delta = timedelta(days=shift_days)
    for i, r in enumerate(_rows(src)):
        r.pop("id")
        r["posted_at"] = (datetime.fromisoformat(r["posted_at"]) + delta).isoformat()
        if r["expiry"]:
            r["expiry"] = (date.fromisoformat(r["expiry"]) + delta).isoformat()
        r.update(discord_message_id=940000000 + i, discord_attachment_id=0, author=CALLER,
                 author_id=940001, is_trade=1, tracking_mode="caller", caller=CALLER)
        r = {k: v for k, v in r.items() if k in cols}
        conn.execute(f"INSERT INTO analyst_trades ({','.join(r)}) VALUES ({','.join('?' * len(r))})",
                     list(r.values()))
    conn.commit()


def _fresh():
    conn = db.get_connection()
    conn.execute("DELETE FROM analyst_trades WHERE caller = ?", (CALLER,))
    conn.commit()
    return conn


def test_the_tally_the_sweep_and_the_trade_block_agree():
    conn = _fresh()
    shift = (datetime.utcnow().date() - TODAY).days
    _insert(conn, ABE, shift)
    try:
        wl = db.compute_caller_win_loss_summary(days=30, caller=CALLER)
        assert (wl["wins"], wl["losses_documented"], wl["losses_silent_expiry"]) == (13, 0, 6)
        assert wl["decided"] == 19 and wl["win_rate_pct"] == 68.4 and wl["avg_win_pct"] == 164.6
        fslr = next(t for t in wl["win_trades"] if t["ticker"] == "FSLR")
        assert (fslr["entry_price"], fslr["exit_price"], fslr["usd_per_contract"]) == (1.79, 8.05, 626.0)
        assert sorted(t["ticker"] for t in wl["silent_expiry_trades"]) == [
            "AVGO", "GLW", "MSTR", "ORCL", "PLTR", "TSLA"]

        # the old sweep's marks: every row past its expiry
        conn.execute("UPDATE analyst_trades SET inferred_status = 'expired_unknown' "
                     "WHERE caller = ? AND expiry IS NOT NULL AND date(expiry) < date('now')", (CALLER,))
        conn.commit()
        db.mark_expired_analyst_positions()
        marked = conn.execute(
            "SELECT ticker, action FROM analyst_trades WHERE caller = ? "
            "AND inferred_status = 'expired_unknown' ORDER BY ticker", (CALLER,)).fetchall()
        assert [tuple(r) for r in marked] == [
            ("AVGO", "open"), ("GLW", "open"), ("MSTR", "open"),
            ("ORCL", "open"), ("PLTR", "open"), ("TSLA", "open")]
        # a second run changes nothing
        assert db.mark_expired_analyst_positions() == []

        text = db.format_analyst_trades_for_context(hours=24 * 30, limit=80, caller=CALLER,
                                                    display="Abe", kind="all")
        assert "**13W / 6L** (documented: 0L, silent expiry: 6L)" in text
        lines = text.split("\n")
        opens = [ln for ln in lines if " open PLTR 160C" in ln or " open LULU 105C" in ln]
        assert len(opens) == 2 and not any("no close alert" in ln for ln in opens)
        assert any(" open GLW 140C" in ln and "[expired — no close alert]" in ln for ln in lines)
        assert any("FSLR 215C" in ln and "entry 1.79, exit 8.05, +$626 per contract" in ln for ln in lines)
        assert any("PLTR 200P" in ln and "entry price not logged" in ln for ln in lines)
        assert any("GLW 140C" in ln and "$473 per contract at risk" in ln for ln in lines)
    finally:
        _fresh()


def test_the_purge_leaves_rows_the_tally_can_still_count():
    conn = _fresh()
    shift = (datetime.utcnow().date() - TODAY).days
    _insert(conn, ABE, shift)
    try:
        db.mark_expired_analyst_positions()
        before = conn.execute("SELECT COUNT(*) FROM analyst_trades WHERE caller = ?", (CALLER,)).fetchone()[0]
        # GLW expired 12 days before "today" in the fixture; with a 7-day
        # retention the old purge would have deleted it
        db.purge_old_expired_analyst_trades(days_after_expiry=7)
        after = conn.execute("SELECT COUNT(*) FROM analyst_trades WHERE caller = ?", (CALLER,)).fetchone()[0]
        assert after == before
    finally:
        _fresh()


def test_the_member_record_counts_trades_only_and_each_once():
    conn = db.get_connection()
    conn.execute("DELETE FROM analyst_trades WHERE author_id = 940002")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(analyst_trades)")}
    base = dict(author_id=940002, action="close", posted_at="2099-01-01 00:00:00", author="t",
                tracking_mode="member", contract_type="call", strike=280.0)
    rows = [dict(base, ticker="MCD", gain_pct=100.0, is_trade=1, discord_attachment_id=1),
            dict(base, ticker="MCD", gain_pct=100.0, is_trade=1, discord_attachment_id=2),
            dict(base, ticker="QQQ", gain_pct=50.0, is_trade=1, discord_attachment_id=3),
            # marked as not a trade by the 11-06 cleanup; still carries its gain
            dict(base, ticker="UNCL", gain_pct=71900.0, is_trade=0, discord_attachment_id=4)]
    try:
        for i, r in enumerate(rows):
            r["discord_message_id"] = 940002000 + (0 if i < 2 else i)
            r = {k: v for k, v in r.items() if k in cols}
            conn.execute(f"INSERT INTO analyst_trades ({','.join(r)}) VALUES ({','.join('?' * len(r))})",
                         list(r.values()))
        conn.commit()
        s = db.member_ledger_summary(940002, days=21, points={})
        assert s["avg_gain_pct"] == 75.0
        st = db.member_close_stats(940002, days=30)
        assert (st["closes_counted"], st["wins"], st["avg_gain_pct"]) == (2, 2, 75.0)
    finally:
        conn.execute("DELETE FROM analyst_trades WHERE author_id = 940002")
        conn.commit()


def test_a_same_day_reentry_at_the_same_price_is_a_new_position():
    base = dict(ticker="SPX", contract_type="call", strike=7500, expiry="2026-11-05")
    rows = [dict(base, id=1, action="open", price=None, gain_pct=None, posted_at="2026-11-05T14:00"),
            dict(base, id=2, action="close", price=None, gain_pct=50.0, posted_at="2026-11-05T15:00"),
            dict(base, id=3, action="open", price=None, gain_pct=None, posted_at="2026-11-05T16:00")]
    pos = P.build(rows, TODAY)
    assert [p["status"] for p in pos] == ["closed", "expired_no_exit"]
    assert P.should_be_marked_expired(rows, TODAY) == {3}


def test_a_close_inside_the_window_finds_an_open_from_before_it():
    """The entry price of a trade opened 40 days ago and closed last week
    is still logged: the tally builds from rows older than its window."""
    conn = _fresh()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(analyst_trades)")}
    now = datetime.utcnow()
    base = dict(ticker="NVDA", contract_type="call", strike=200.0,
                expiry=(now + timedelta(days=20)).date().isoformat(), author=CALLER,
                author_id=940001, is_trade=1, tracking_mode="caller", caller=CALLER,
                discord_attachment_id=0)
    rows = [dict(base, action="open", price=2.0, gain_pct=None, discord_message_id=940000901,
                 posted_at=(now - timedelta(days=40)).isoformat()),
            dict(base, action="close", price=5.0, gain_pct=150.0, discord_message_id=940000902,
                 posted_at=(now - timedelta(days=5)).isoformat())]
    try:
        for r in rows:
            r = {k: v for k, v in r.items() if k in cols}
            conn.execute(f"INSERT INTO analyst_trades ({','.join(r)}) VALUES ({','.join('?' * len(r))})",
                         list(r.values()))
        conn.commit()
        wl = db.compute_caller_win_loss_summary(days=30, caller=CALLER)
        assert wl["wins"] == 1 and wl["losses_silent_expiry"] == 0
        assert wl["win_trades"][0]["entry_price"] == 2.0
        assert wl["win_trades"][0]["usd_per_contract"] == 300.0
    finally:
        _fresh()

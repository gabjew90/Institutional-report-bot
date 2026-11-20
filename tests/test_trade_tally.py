"""The trade log read as a record, once (analyst_log/tally.py), and the
record questions answered in code (discord_bot/trade_tally.py).

2026-11-19, audit run 13: one trader was given "4W / 6L, 40%" from the
caller tally and "45W/15L" on his profile line on a day his rows held 60
winning closes and 1 losing; "the win rate of the member channel" was
answered with the two callers; "returns on puts this month" with row
counts from SQL the model wrote. Fixture rows are invented: contract,
action, price, gain and time only.
"""
import asyncio
import pathlib
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import db  # noqa: E402
from analyst_log import tally as T  # noqa: E402
from discord_bot import trade_tally as TT  # noqa: E402

TODAY = date(2026, 11, 19)
SINCE = "2026-10-20T00:00:00"
ALERTS, MAIN = 9500001, 9500002
COLS = ("posted_at", "ticker", "contract_type", "strike", "expiry", "action", "price", "gain_pct",
        "channel_id")
# One trader. Alerts posted in his alert channel, most exits in the main chat.
KYLE = [
    ("2026-11-03T15:00", "NVDA", "call", 200, "2026-11-07", "open", 2.0, None, ALERTS),
    ("2026-11-04T15:00", "NVDA", "call", 200, "2026-11-07", "close", 3.0, 50.0, MAIN),     # exit posted elsewhere
    ("2026-11-05T15:00", "TLT", "call", 82, "2026-11-06", "open", 0.13, None, ALERTS),     # never closed
    ("2026-11-06T15:00", "AMD", "put", 240, "2026-11-07", "close", None, 80.0, MAIN),      # result, no entry
    ("2026-11-07T15:00", "MU", "call", 730, "2026-11-14", "close", 7.3, None, MAIN),       # exit, no result
    ("2026-11-10T15:00", "SPY", "put", 660, "2026-11-12", "open", 1.0, None, MAIN),
    ("2026-11-11T15:00", "SPY", "put", 660, "2026-11-12", "close", 0.7, -30.0, MAIN),
    ("2026-11-17T15:00", "META", "call", 700, "2026-12-19", "open", 5.0, None, ALERTS),    # still open
    ("2026-11-12T15:00", "QQQ", None, None, None, "close", None, 2009.0, MAIN),            # dollars read as a percent
    ("2026-10-01T15:00", "IBM", "call", 300, "2026-10-03", "open", 1.0, None, ALERTS),     # before the window
]


def _rows(src=KYLE, author=7):
    return [dict(zip(COLS, r), id=i + 1, author_id=author) for i, r in enumerate(src)]


def _in(channel):
    return lambda p: any(r.get("channel_id") == channel for r in p["rows"])


def test_one_count_of_wins_losses_expiries_and_opens():
    t = T.build(_rows(), since=SINCE, today=TODAY)
    assert (t["wins"], t["losses"], t["expired_no_exit"], t["open"], t["opened"]) == (2, 1, 1, 1, 4)
    # a close with no gain is neither a win nor a loss; +2,009% on shares is not a result
    assert (t["flat"], t["discarded"], t["closes_without_entry"]) == (1, 1, 1)
    assert t["decided"] == 4 and t["win_rate_pct"] == 50.0
    assert (t["avg_win_pct"], t["avg_loss_pct"], t["avg_close_pct"]) == (65.0, -30.0, 33.3)
    assert t["by_kind"]["put"]["wins"] == 1 and t["by_kind"]["call"]["expired_no_exit"] == 1
    # the open posted before the window is not this window's expiry
    assert [s["ticker"] for s in t["silent_expiry_trades"]] == ["TLT"]


def test_an_exit_posted_in_another_channel_closes_the_alert():
    """The alerts' record: NVDA was closed in the main chat, so it is a
    win and not an alert that expired with no exit."""
    t = T.build(_rows(), since=SINCE, today=TODAY, in_scope=_in(ALERTS))
    assert (t["wins"], t["losses"], t["expired_no_exit"], t["open"], t["opened"]) == (1, 0, 1, 1, 3)
    # read from the alert channel's rows alone, as the tally was until
    # 2026-11-20, the same alert is an open that expired
    alone = T.build([r for r in _rows() if r["channel_id"] == ALERTS], since=SINCE, today=TODAY)
    assert (alone["wins"], alone["expired_no_exit"]) == (0, 2)


def test_filters_and_the_window():
    assert T.build(_rows(), since=SINCE, today=TODAY, contract_type="put")["wins"] == 1
    t = T.build(_rows(), since=SINCE, today=TODAY, ticker="spy")
    assert (t["wins"], t["losses"], t["opened"]) == (0, 1, 1)
    # a close is counted by the day it was posted
    late = T.build(_rows(), since="2026-11-05T00:00:00", today=TODAY)
    assert late["wins"] == 1 and late["opened"] == 3
    until = T.build(_rows(), since=SINCE, until="2026-11-05T00:00:00", today=TODAY)
    assert (until["wins"], until["opened"]) == (1, 1)


def test_each_trader_is_built_alone_and_counted_once():
    other = [("2026-11-04T16:00", "NVDA", "call", 200, "2026-11-07", "close", None, -40.0, MAIN)]
    t = T.build(_rows() + [dict(r, id=100 + r["id"]) for r in _rows(other, author=8)],
                since=SINCE, today=TODAY)
    # trader 8's NVDA close is his own result, not a second exit of trader 7's alert
    assert (t["wins"], t["losses"]) == (2, 2)
    assert t["by_trader"][8] == {"wins": 0, "losses": 1, "expired_no_exit": 0, "open": 0,
                                 "opened": 0, "win_rate_pct": 0.0}
    # a trader with nothing inside the window is not one of the traders
    old = [("2026-09-01T15:00", "IBM", "call", 300, "2026-09-05", "close", None, 10.0, MAIN)]
    t = T.build(_rows() + [dict(r, id=200) for r in _rows(old, author=9)], since=SINCE, today=TODAY)
    assert set(t["by_trader"]) == {7}


def test_the_entry_price_is_the_first_open_that_carries_one():
    """A caller's alert and his mention of it in the main chat are one
    position. The mention can sort first, and it has no price."""
    from analyst_log import positions as P
    base = dict(ticker="MU", contract_type="call", strike=740, expiry="2026-11-14", gain_pct=None)
    rows = [dict(base, id=1, action="open", price=None, posted_at="2026-11-10T16:02:01", channel_id=MAIN),
            dict(base, id=2, action="open", price=5.32, posted_at="2026-11-10T16:02:40", channel_id=ALERTS),
            dict(base, id=3, action="add", price=9.0, posted_at="2026-11-10T18:00:00", channel_id=ALERTS),
            dict(base, id=4, action="close", price=7.32, gain_pct=37.6, posted_at="2026-11-11T15:00:00",
                 channel_id=MAIN)]
    (p,) = P.build(rows, TODAY)
    assert P.entry_price(p) == 5.32
    assert P.dollars_per_contract(p, p["exits"][-1], 37.6) == 200.0
    # an add at a later price is never the entry
    assert P.entry_price(P.build([rows[0], rows[2]], TODAY)[0]) is None


# ---- the question ------------------------------------------------------------

def test_the_two_questions_of_2026_11_19_are_read_in_code():
    a = TT.parse("do a calculation of the win rate of the member channel is the past month")
    assert a["channel_id"] == 1408508350202908845 and a["window"] == {"kind": "last", "n": 1, "unit": "m"}
    b = TT.parse("what is the percent of returns on puts this discord has called out this month")
    assert b["room"] and b["contract_type"] == "put" and b["window"] == {"kind": "this_month"}
    assert b["channel_id"] is None and not b["self"]


def test_who_what_and_when_come_from_the_askers_words():
    assert TT.parse("whats my win rate")["self"]
    assert TT.parse("what's bk's win rate")["names"] == ["bk"]
    assert TT.parse("kyle alerts win rate last 2 weeks")["channel_id"] == 1420507457708621824
    assert TT.parse("what's the win rate in this channel today")["this_channel"]
    assert TT.parse("my win rate on mu calls")["contract_type"] == "call"
    # "Abe's calls" are his alerts of any kind
    assert TT.parse("win rate for abe's calls")["contract_type"] is None
    assert TT.parse("whats the discords win rate on $NVDA ytd")["ticker"] == "NVDA"
    assert TT.parse("whats the discords win rate on $NVDA ytd")["window"] == {"kind": "all"}
    assert TT.parse("who has the better W/L abe or BK")["pair_names"] == ["abe", "bk"]


def test_what_is_not_a_record_question_goes_to_the_model():
    for q in ("what win rate do I need at 2 to 1",          # arithmetic
              "roast my win rate",                          # banter
              "my win rate is better than yours",           # a boast
              "NVDA's record high was when",                # a stock
              "what's the SPY backtest win rate after a 2% drop",
              "abe is on a heater this week and i think his win rate has to be over ninety by now",
              "any injury news"):
        assert TT.parse(q) is None, q
    assert TT.parse("what's kyle's record in fantasy") is None
    # a record of some of his trades only (asked 2026-10-06): the model's
    assert TT.parse('what is Abe\'s record for the past 90 days for the options he says are "slam" '
                    'or "slammed"') is None
    # "put it in a chart" is not the option
    assert TT.parse("what's bk's win rate, put it in a chart")["contract_type"] is None
    # in the league channel a record is the league's unless trades are named
    import channel_config
    assert TT.parse("what's my record", channel_config.FANTASY_CHANNEL_ID) is None
    assert TT.parse("what's my record on puts", channel_config.FANTASY_CHANNEL_ID)["self"]


# ---- against a database ------------------------------------------------------

A, B = 950001, 950002
CH_ALERTS, CH_MAIN = 950000001, 950000002


def _clean(conn):
    conn.execute("DELETE FROM analyst_trades WHERE author_id IN (?, ?)", (A, B))
    conn.execute("DELETE FROM chat_messages WHERE author_id IN (?, ?)", (A, B))
    conn.commit()


def _load(conn):
    """KYLE's rows as member A, moved so the fixture's "today" is today,
    plus one losing close by member B in the alert channel."""
    _clean(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(analyst_trades)")}
    shift = timedelta(days=(datetime.utcnow().date() - TODAY).days)
    chan = {ALERTS: CH_ALERTS, MAIN: CH_MAIN}
    src = [(A, r) for r in KYLE] + [
        (B, ("2026-11-12T15:00", "AMD", "call", 250, "2026-11-14", "close", None, -55.0, ALERTS))]
    for i, (who, r) in enumerate(src):
        row = dict(zip(COLS, r))
        cid = chan[row.pop("channel_id")]
        row["posted_at"] = (datetime.fromisoformat(row["posted_at"]) + shift).isoformat()
        if row["expiry"]:
            row["expiry"] = (date.fromisoformat(row["expiry"]) + shift).isoformat()
        mid = 950000100 + i
        caller_row = who == A and cid == CH_ALERTS
        row.update(discord_message_id=mid, discord_attachment_id=0, author="tallytest",
                   author_id=who, is_trade=1, tracking_mode="caller" if caller_row else "member",
                   caller="tallytest" if caller_row else None)
        row = {k: v for k, v in row.items() if k in cols}
        conn.execute(f"INSERT INTO analyst_trades ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                     list(row.values()))
        conn.execute(
            "INSERT INTO chat_messages (discord_message_id, channel_id, channel_name, author_id, "
            "author_username, author_display, content, posted_at) VALUES (?,?,?,?,?,?,?,?)",
            (mid, cid, "🧪-tally-alerts-🧪" if cid == CH_ALERTS else "🧪-tally-main-🧪", who,
             "tallya" if who == A else "tallyb", "TallyA" if who == A else "TallyB", "", row["posted_at"]))
    conn.commit()


def test_every_reader_gives_the_same_record():
    conn = db.get_connection()
    _load(conn)
    try:
        # the member, in every channel
        m = db.trade_tally(author_ids=[A], days=30)
        assert (m["wins"], m["losses"], m["expired_no_exit"], m["open"]) == (2, 1, 1, 1)
        # the caller's alerts: the exit posted in the main chat counts
        wl = db.compute_caller_win_loss_summary(days=30, caller="tallytest")
        assert (wl["wins"], wl["losses_documented"], wl["losses_silent_expiry"]) == (1, 0, 1)
        assert wl["everywhere"]["wins"] == 2 and wl["everywhere"]["losses"] == 1
        text = db.format_analyst_trades_for_context(hours=24 * 30, limit=80, caller="tallytest",
                                                    display="Kyle", kind="tally")
        assert "**1W / 1L** (documented: 0L, silent expiry: 1L)" in text
        assert "In every channel he posts in, alerts included: 2 winning closes, 1 losing" in text
        # the record the model is handed for a member
        st = db.member_close_stats(A, days=30)
        assert (st["wins"], st["losses"], st["expired_with_no_exit_posted"], st["still_open"]) == (2, 1, 1, 1)
        assert st["win_rate_pct_of_closes"] == 66.7
        assert st["win_rate_pct_counting_expiries_as_losses"] == 50.0
        assert db.member_close_stats(A, days=30, contract_type="put")["wins"] == 1
        # the profile line: a close with no result logged is not a loss
        led = db.member_ledger_summary(A, days=30)
        assert (led["wins"], led["losses"], led["expired_no_exit"]) == (2, 1, 1)
        # the channel: both members' positions with a row posted there
        ch = db.trade_tally(channel_id=CH_ALERTS, days=30)
        assert (ch["wins"], ch["losses"], ch["expired_no_exit"]) == (1, 1, 1)
        assert set(ch["by_trader"]) == {A, B}
    finally:
        _clean(conn)


def test_the_answer_is_posted_as_counted():
    conn = db.get_connection()
    _load(conn)
    try:
        spec = TT.parse("whats my win rate")
        out = TT.lines(TT.run(spec, A, CH_MAIN))
        assert out[0] == ("→ **You**, in the last 30 days: win rate **67%** of the 3 closes with a result "
                          "(**2** wins, **1** loss), or **50%** when the 1 position that expired with no "
                          "exit posted is counted as a loss")
        assert out[1] == ("→ **4** positions opened, **1** still open. 1 more close had no result logged. "
                          "1 of the closes with a result had no entry logged. 1 close left out: the logged "
                          "gain cannot be a result")
        assert out[2] == "→ Average winning close **+65%**, average losing close **-30%**"
        assert out[3] == "→ Calls: 1 win, 0 losses, 1 expired with no exit. Puts: 1 win, 1 loss"
        # puts only: no split line, and the filter is named
        puts = TT.lines(TT.run(TT.parse("whats my win rate on puts"), A, CH_MAIN))
        assert puts[0].startswith("→ **You**, puts, in the last 30 days: win rate **50%** of the 2 closes")
        assert not any(ln.startswith("→ Calls:") for ln in puts)
        # a channel, by its mention: its members, most active first
        chan = TT.lines(TT.run(TT.parse(f"win rate of <#{CH_ALERTS}> in the last 3 weeks?"), B, CH_MAIN))
        assert chan[0].startswith(
            "→ **#tally-alerts**, 2 members, in the last 3 weeks: win rate **50%** of the 2 closes")
        assert chan[-1] == "→ Most active: **TallyA** 1W 0L 1 expired · **TallyB** 0W 1L"
        # a member with nothing logged
        none = TT.lines(TT.run(TT.parse(f"<@{B}> win rate on puts?"), A, CH_MAIN))
        assert none == ["→ No trades logged for **TallyB**, puts, in the last 30 days"]
        # nobody and nothing named: the model takes it
        assert TT.run(TT.parse("what's his win rate"), None, CH_MAIN) is None
        assert TT.answer(TT.parse("whats the win rate of the room?"), A, CH_MAIN).startswith("→ **The room**")
    finally:
        _clean(conn)


def test_the_tool_returns_the_same_lines_for_a_channel():
    from discord_bot.ask_tools import _execute_trade_log
    conn = db.get_connection()
    _load(conn)
    try:
        res = asyncio.run(_execute_trade_log({"scope": "channel", "channel": str(CH_ALERTS), "days": 30}))
        assert res["status"] == "ok"
        assert res["record_lines"][0].startswith("→ **#tally-alerts**, 2 members, in the last 30 days: win rate")
        miss = asyncio.run(_execute_trade_log({"scope": "channel", "channel": "the moon"}))
        assert miss["status"] == "not_found"
        # the two anchors still need exactly one
        assert asyncio.run(_execute_trade_log({}))["status"] == "error"
    finally:
        _clean(conn)


def test_a_window_past_the_log_is_cut_to_what_is_kept():
    conn = db.get_connection()
    _load(conn)
    try:
        t = db.trade_tally(author_ids=[A], days=400)
        assert t["window_capped"] and t["days"] == db.TALLY_MAX_DAYS
        out = TT.lines(TT.run(TT.parse("what's my win rate ytd"), A, CH_MAIN))
        assert "in the last 45 days, as far back as the trade log is kept in full" in out[0]
    finally:
        _clean(conn)


def test_a_rank_carries_the_record_and_not_the_writers_paragraph():
    """Audit run 13, M2 and M3: the stored paragraph called the member with
    the best record a "tilt-prone gambler", and four members with no slur
    on record were said to "drop slurs". Neither paragraph is served."""
    from discord_bot import rank_evidence
    conn = db.get_connection()
    _load(conn)
    conn.execute("DELETE FROM user_profiles WHERE user_id = ?", (A,))
    conn.execute(
        "INSERT INTO user_profiles (user_id, username, display_name, profile_text, trader_score, "
        "trader_rationale, racism_rationale) VALUES (?, 'tallya', 'TallyA', 'x', 61, "
        "'A tilt-prone gambler in the average-joe bracket.', 'Drops slurs with nonchalance.')", (A,))
    conn.commit()
    try:
        res = db.lookup_user_ranks(username="tallya")
        user = res["users"][0]
        assert "trader_rationale" not in user and "racism_rationale" not in user
        ev = user["trader_evidence"]
        # 21 days: the same count the record answer posts
        t = db.trade_tally(author_ids=[A], days=21)
        assert (ev["wins"], ev["losses"], ev["ghosted"]) == (t["wins"], t["losses"], t["expired_no_exit"])
        foot = rank_evidence.footer("his trader rank", [res])
        assert f"{t['wins']}W/{t['losses']}L documented in 21d" in foot
        if t["expired_no_exit"]:
            assert "opened and expired with no exit posted" in foot
        top = db.lookup_user_ranks(metric="trader", top_n=50)
        assert all("trader_rationale" not in u for u in top["users"])
        snap = db.export_user_profiles_markdown()
        assert "tilt-prone gambler" not in snap and "Drops slurs" not in snap
        assert f"_trades: documented 21d: {t['wins']}W/{t['losses']}L" in snap
    finally:
        conn.execute("DELETE FROM user_profiles WHERE user_id = ?", (A,))
        conn.commit()
        _clean(conn)

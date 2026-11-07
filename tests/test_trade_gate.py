"""One check before a trade row is stored (analyst_log/trade_gate.py), and
one line per contract in the profile (scripts/profile_trades.py).

Cases are the run-8 audit's (2026-11-05): a chart command stored as COIN
shares (M10), a price remark stored as an open (M10), a nickname read as a
ticker (M11), an unlisted option and a +71900% gain with no open (M12),
repeated exit lines, one trade under two types and a missing year (M13).
"""
import itertools
from datetime import date

import db
from analyst_log import trade_gate as T
from discord_bot import listed_symbols
from scripts import profile_trades as P

_ids = itertools.count(990_000_000)


def setup_module(_):
    listed_symbols.use({"COIN": ("Coinbase", False), "NBIS": ("Nebius", False),
                        "NVDA": ("NVIDIA", False), "FSLR": ("First Solar", False)})


def teardown_module(_):
    listed_symbols.use(None)
    conn = db.get_connection()
    conn.execute("DELETE FROM chat_messages WHERE author_id = 77")
    conn.execute("DELETE FROM analyst_trades WHERE author = 'sam' AND ticker = 'UNCL'")
    conn.commit()


def _gate(ticker, text, ct=None, action="open", image=False, aliases=frozenset()):
    return T.reject_reason(ticker=ticker, contract_type=ct, action=action, text=text,
                           from_image=image, aliases=set(aliases))


def test_a_chart_command_is_not_a_trade():
    assert _gate("COIN", "Fc coin 5") == "chart command"


def test_a_bare_price_remark_is_not_an_open():
    assert "price remark" in _gate("NBIS", "NBIS at 400")
    assert _gate("NBIS", "bought NBIS at 400") == ""
    assert _gate("NBIS", "NBIS 400c 11/21") == ""
    # real entries the first version rejected (2026-11-06 dry run)
    for text in ("Slammed nbis at 185 w margin", "Another 40 at 16.8", "Full port 273 googl", "2x NBIS, 4x AMD"):
        assert _gate("NBIS", text) == "", text


def test_a_members_nickname_is_not_a_ticker():
    assert "member's name" in _gate("WIZ", "Doubling down with Wiz here", aliases={"wiz"})
    assert _gate("WIZ", "Doubling down with Wiz here", aliases={"wiz"}).startswith("WIZ")


def test_an_unlisted_option_is_rejected_and_index_roots_pass():
    T._coin_cache["UNCL"] = True       # a coin of that name exists; still no options on it
    assert "no listed options" in _gate("UNCL", "", ct="call", action="close", image=True)
    assert _gate("NDXP", "", ct="call", action="close", image=True) == ""
    T._coin_cache["MRN"] = False
    assert "not a listed symbol" in _gate("MRN", "bought MRN shares")
    assert _gate("XMR", "longed XMR at 587") == ""


def test_an_impossible_option_gain_without_an_open_is_blanked():
    assert T.gain_or_none(71900, "call", has_open=False) is None
    assert T.gain_or_none(71900, "call", has_open=True) == 71900
    assert T.gain_or_none(250, "call", has_open=False) == 250


def test_a_rejected_screenshot_row_is_kept_as_a_non_trade():
    mid = next(_ids)
    db.record_analyst_trade(
        discord_message_id=mid, discord_attachment_id=1, author="sam", posted_at="2026-11-04T15:00:00",
        image_url="http://x/y.png", caption="", is_trade=True, gemini_json={}, ticker="UNCL",
        contract_type="call", strike=167.5, action="close", gain_pct=71900, author_id=1, tracking_mode="member")
    row = db.get_connection().execute(
        "SELECT is_trade, gemini_json FROM analyst_trades WHERE discord_message_id = ?", (mid,)).fetchone()
    assert row[0] == 0 and "rejected_by_gate" in row[1]


def _row(action, day, ct="call", gain=None, strike=28325, ticker="NDXP", expiry="2026-11-21"):
    return {"ticker": ticker, "contract_type": ct, "strike": strike, "expiry": expiry,
            "action": action, "gain_pct": gain, "posted_at": f"2026-11-{day:02d}T15:00:00"}


def test_trims_and_the_close_are_one_line_and_a_repeat_is_read_once():
    rows = [_row("open", 3), _row("trim", 4, gain=50), _row("trim", 4, gain=50), _row("close", 5, gain=80)]
    lines = P.section_lines(rows, today=date(2026, 11, 6))
    assert lines == ["- NDXP 28325C exp 11/21: opened Nov 3, exited in 2 parts by Nov 5, last at +80.0%"]


def test_shares_under_two_types_are_one_position():
    rows = [_row("open", 3, ct="stock", strike=None, ticker="FSLR", expiry=None),
            _row("close", 5, ct="unclear", strike=None, ticker="FSLR", expiry=None, gain=12)]
    lines = P.section_lines(rows, today=date(2026, 11, 6))
    assert len(lines) == 1 and "closed Nov 5, +12.0%" in lines[0]


def test_an_expiry_in_another_year_shows_the_year():
    rows = [_row("open", 3, ticker="CBRG", strike=2, expiry="2027-03-01")]
    assert "exp 03/01/27" in P.section_lines(rows, today=date(2026, 11, 6))[0]


def test_an_exit_with_no_open_and_an_impossible_gain_shows_no_gain():
    rows = [_row("close", 4, ticker="NVDA", strike=167.5, gain=71900)]
    assert "gain not recorded" in P.section_lines(rows, today=date(2026, 11, 6))[0]


def test_the_profile_leaves_out_rows_the_check_rejects():
    mid = next(_ids)
    conn = db.get_connection()
    conn.execute("INSERT INTO chat_messages (discord_message_id, channel_id, channel_name, author_id, "
                 "author_username, author_display, content, posted_at) "
                 "VALUES (?, 1, 'alerts', 77, 'bigz', 'Big Z', 'Fc coin 5', datetime('now'))", (mid,))
    conn.commit()
    rows = [{"ticker": "COIN", "contract_type": "stock", "action": "open", "discord_message_id": mid,
             "image_url": None, "discord_attachment_id": -1, "caption": None},
            {"ticker": "NVDA", "contract_type": "call", "action": "open", "discord_message_id": None,
             "image_url": "http://x", "discord_attachment_id": 5, "caption": ""}]
    assert [r["ticker"] for r in P._gated(rows)] == ["NVDA"]


def test_an_fc_line_under_a_remark_is_a_chart_command_and_a_real_trade_beside_one_is_not():
    assert _gate("COIN", "Surprised coin isn't down worse \n\nFc coin 5") == "chart command"
    assert _gate("NVDA", "bought nvda 200c\nfc nvda") == ""


def test_the_write_path_never_looks_a_coin_up():
    T._coin_cache.pop("ZZZQ", None)
    assert T.is_coin("ZZZQ", lookup=False) is True
    assert _gate("ZZZQ", "bought zzzq") == ""      # unknown, not looked up: kept

"""trader_evidence slices the ledger's tickers, which arrive as a set.

2026-10-18 audit A5: every lookup_user_ranks(metric="trader") call raised
"TypeError: 'set' object is not subscriptable", and the answer fell back to
an all-time query that ranked the wrong member first.
"""
import pathlib
import sys
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import db  # noqa: E402,F401  (loads the facade before its parts)
from db_parts import chat  # noqa: E402


def test_tickers_from_a_set_are_sorted_and_capped():
    led = {"wins": 3, "losses": 1, "avg_gain_pct": 42.4,
           "tickers": {"TSLA", "AAPL", "NVDA", "MU", "AMD", "SPY", "QQQ"}}
    with patch("db.compute_member_points", return_value={"points": 5}), \
         patch("db.member_ledger_summary", return_value=led):
        ev = chat.trader_evidence(1)
    assert ev["tickers"] == ["AAPL", "AMD", "MU", "NVDA", "QQQ", "SPY"]
    assert ev["wins"] == 3 and ev["avg_gain_pct_on_closes"] == 42


def test_no_tickers_is_an_empty_list():
    with patch("db.compute_member_points", return_value={}), \
         patch("db.member_ledger_summary", return_value={}):
        assert chat.trader_evidence(1)["tickers"] == []


def test_a_group_line_is_labelled_not_the_stocks_own_2026_10_18():
    """Audit A23: Goldman's "IPPs (CEG, NRG, TLN, VST) trade at ~7x EBITDA"
    was given as VST's own valuation."""
    import json
    import db
    conn = db.get_connection()
    pid = db.insert_pdf_file("/Current/2026/October/Oct 12/GS/ipp.pdf", "ipp.pdf", "")
    analysis = {"source": "Goldman Sachs", "title": "IPPs", "key_insights": [
        "IPPs (CEG, NRG, TLN, VST) trade at ~7x EBITDA with 12% FCF yield on 2027 estimates",
        "VST raised its buyback"]}
    db.insert_analysis(pdf_file_id=pid, triage_json="{}", analysis_json=json.dumps(analysis),
                       priority="high", pages_analyzed=1, total_pages=1, input_tokens=0,
                       output_tokens=0, model="t", duration=0.0)
    aid = conn.execute("SELECT MAX(id) FROM pdf_analyses WHERE pdf_file_id = ?", (pid,)).fetchone()[0]
    conn.execute("DELETE FROM pdf_entities WHERE analysis_id = ?", (aid,))
    conn.execute("INSERT INTO pdf_entities (analysis_id, pdf_file_id, ticker, name) "
                 "VALUES (?, ?, 'VST', 'Vistra Corp')", (aid, pid))
    conn.commit()
    notes = db.research_for_ticker("VST", days=30)
    ins = next(n for n in notes if n["title"] == "IPPs")["insights"]
    assert ins[0].startswith("Group-wide line, not VST alone:")
    assert ins[1] == "VST raised its buyback"


def test_member_close_stats_drops_impossible_rows_2026_10_26():
    """Audit A6: a 50,900% row on ticker "unclear" made "+1,919.7% avg"."""
    import db
    conn = db.get_connection()
    conn.execute("DELETE FROM analyst_trades WHERE author_id = 930001")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(analyst_trades)")}
    base = {"author_id": 930001, "action": "close", "posted_at": "2099-01-01 00:00:00"}
    rows = [dict(base, ticker="NVDA", contract_type="call", gain_pct=50.0),
            dict(base, ticker="AMD", contract_type="call", gain_pct=-20.0),
            dict(base, ticker="unclear", contract_type="unclear", gain_pct=50900.0)]
    for i, r in enumerate(rows):
        r = {k: v for k, v in r.items() if k in cols}
        r.update({"discord_message_id": 930001000 + i, "discord_attachment_id": i,
                  "author": "tester", "is_trade": 1, "tracking_mode": "member"})
        r = {k: v for k, v in r.items() if k in cols}
        conn.execute(f"INSERT INTO analyst_trades ({','.join(r)}) VALUES ({','.join('?'*len(r))})", list(r.values()))
    conn.commit()
    s = db.member_close_stats(930001, days=30)
    assert s["closes_counted"] == 2 and s["closes_discarded_as_impossible"] == 1
    assert s["win_rate_pct"] == 50.0 and s["avg_gain_pct"] == 15.0
    assert db.member_close_stats(930001, days=30, contract_type="call")["wins"] == 1
    conn.execute("DELETE FROM analyst_trades WHERE author_id = 930001")
    conn.commit()

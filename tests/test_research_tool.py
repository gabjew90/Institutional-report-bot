"""The /ask research tool (2026-09-30): what the banks wrote about a
ticker, from the ingested PDF analyses."""
import asyncio
import json
import sqlite3
from unittest.mock import patch

import db
from discord_bot import research_tool as RT


def _conn():
    conn = sqlite3.connect(":memory:", check_same_thread=False)   # the executor reads in a thread
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE pdf_analyses (id INTEGER PRIMARY KEY AUTOINCREMENT, pdf_file_id INTEGER,
                                   analysis_json TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE pdf_entities (analysis_id INTEGER, pdf_file_id INTEGER, ticker TEXT, name TEXT, asset_class TEXT);
    """)
    return conn


def _add(conn, pdf_id, analysis, tickers, created_at="datetime('now')"):
    cur = conn.execute(
        f"INSERT INTO pdf_analyses (pdf_file_id, analysis_json, created_at) VALUES (?, ?, {created_at})",
        (pdf_id, json.dumps(analysis)))
    for tk, name in tickers:
        conn.execute("INSERT INTO pdf_entities VALUES (?, ?, ?, ?, 'stock')", (cur.lastrowid, pdf_id, tk, name))
    return cur.lastrowid


GS = {
    "source": "Goldman Sachs", "title": "Micron: raising estimates into the print", "report_type": "equity_research",
    "published_at": "2026-09-29T08:00:00",
    "market_movers": [{"ticker": "MU", "action": "reiterate", "rating": "Buy", "price_target": "$1,250",
                       "rationale": "HBM pricing holds through 2027", "conviction": "high"},
                      {"ticker": "AMD", "action": "reiterate", "rating": "Neutral", "price_target": "$300", "rationale": "x"}],
    "earnings_insights": ["Micron reports Wednesday after the close, consensus EPS $32.32."],
    "key_insights": ["Micron's HBM supply is sold out for 2027.", "Oil is a policy problem."],
    "trade_ideas": [{"description": "Long MU into earnings", "rationale": "beat and raise", "risk": "guide",
                     "time_horizon": "swing", "instruments": ["MU"]}],
    "key_data_points": [
        {"figure": "$56.3B", "metric": "MU F1Q27 revenue consensus", "context": "JPM expects the guide above it",
         "source_bank": "Goldman Sachs", "figure_status": "forecast"},
        {"figure": "$751B", "metric": "2026 hyperscaler capex", "context": "", "source_bank": "Goldman Sachs"},
    ],
}
MS = {"source": "Morgan Stanley", "title": "Semis weekly", "report_type": "sales_trading",
      "key_insights": ["Memory names are crowded longs, MU the most."], "market_movers": [], "earnings_insights": []}
OLD = {"source": "UBS", "title": "old", "key_insights": ["MU is cheap."], "market_movers": []}


def test_notes_are_reduced_to_the_parts_about_the_ticker():
    conn = _conn()
    _add(conn, 1, GS, [("MU", "Micron Technology"), ("AMD", "AMD")])
    _add(conn, 2, MS, [("MU", "Micron")])
    _add(conn, 3, OLD, [("MU", "Micron")], created_at="datetime('now', '-40 days')")
    with patch("db_parts.pdf._db.get_connection", return_value=conn):
        notes = db.research_for_ticker("mu", days=14)
    assert [n["source"] for n in notes] == ["Morgan Stanley", "Goldman Sachs"]   # newest first
    gs = notes[1]
    assert gs["calls"] == [{"action": "reiterate", "rating": "Buy", "price_target": "$1,250",
                            "rationale": "HBM pricing holds through 2027", "conviction": "high"}]
    assert gs["earnings"] == ["Micron reports Wednesday after the close, consensus EPS $32.32."]
    assert gs["insights"] == ["Micron's HBM supply is sold out for 2027."], "the oil line is not about MU"
    assert gs["trade_ideas"][0]["description"] == "Long MU into earnings"
    assert gs["published"] == "2026-09-29"
    assert gs["data_points"] == [{"figure": "$56.3B", "metric": "MU F1Q27 revenue consensus",
                                  "context": "JPM expects the guide above it", "figure_status": "forecast"}]


def test_a_short_ticker_matches_as_a_word_not_a_substring():
    conn = _conn()
    _add(conn, 1, {"source": "Citi", "market_movers": [],
                   "key_insights": ["Investors must wait.", "MU guides above.", "Micron's HBM share grows."]},
         [("MU", "Micron Technology")])
    with patch("db_parts.pdf._db.get_connection", return_value=conn):
        notes = db.research_for_ticker("MU")
    assert notes[0]["insights"] == ["MU guides above.", "Micron's HBM share grows."]


def test_a_superseded_analysis_and_a_note_with_nothing_about_the_name_are_skipped():
    conn = _conn()
    _add(conn, 1, {"source": "Citi", "key_insights": ["MU to the moon"], "market_movers": []}, [("MU", "Micron")])
    _add(conn, 1, {"source": "Citi", "key_insights": ["Rates are the story."], "market_movers": []}, [("MU", "Micron")])
    with patch("db_parts.pdf._db.get_connection", return_value=conn):
        assert db.research_for_ticker("MU") == []      # latest analysis of PDF 1 says nothing about MU
        assert db.research_for_ticker("") == []


def test_the_executor_reports_banks_and_no_data():
    conn = _conn()
    _add(conn, 1, GS, [("MU", "Micron Technology")])
    with patch("db_parts.pdf._db.get_connection", return_value=conn):
        ok = asyncio.run(RT._execute_research({"symbol": "mu", "days": "500"}))
        none = asyncio.run(RT._execute_research({"symbol": "ZZZZ"}))
    assert ok["status"] == "ok" and ok["banks"] == ["Goldman Sachs"] and ok["days"] == 60
    assert none["status"] == "no_data" and "invent" in none["error"]
    assert asyncio.run(RT._execute_research({}))["status"] == "error"


def test_the_injected_block_is_a_prose_digest_with_the_figures():
    from discord_bot import ask_router as R
    payload = {"status": "ok", "symbol": "MU", "days": 14, "banks": ["JPMorgan"], "notes": [{
        "source": "JPMorgan", "title": "Afternoon Briefing", "published": "2026-09-29", "report_type": "x",
        "calls": [{"action": "positive_catalyst_watch", "rating": "N/A", "price_target": "N/A",
                   "rationale": "Beat vs Street and raised guidance on HBM4 demand.", "conviction": "high"}],
        "earnings": ["MU F1Q27: Street consensus $56.3B revenue / 86.5% GM / $35.71 EPS; JPM expects a raise well above."],
        "insights": [], "trade_ideas": [], "risks": ["Higher yields."],
        "data_points": [{"figure": "$24B+", "metric": "Aug-Q FCF run-rate", "context": "cumulative near $200B through CY27"}],
    }]}
    block = R.inject_text(R.T_RESEARCH, payload)
    assert block.startswith("[INSTITUTIONAL RESEARCH")
    assert "JPMorgan (2026-09-29): Afternoon Briefing" in block
    assert "earnings: MU F1Q27: Street consensus $56.3B" in block
    assert "figure: $24B+ Aug-Q FCF run-rate (cumulative near $200B through CY27)" in block
    assert "call: positive_catalyst_watch · high conviction. Beat vs Street" in block
    assert "risk: Higher yields." in block and '"notes"' not in block
    # no_data keeps the JSON form, which carries the instruction text
    assert '"status": "no_data"' in R.inject_text(R.T_RESEARCH, {"status": "no_data", "error": "x"})


def test_the_tool_is_declared_and_routed():
    tool = RT._build_research_tool()
    assert tool.function_declarations[0].name == "lookup_research"
    from discord_bot import data_footer as F
    assert "bank research" in F.footer([{"tool": "lookup_research", "status": "ok"}])


def test_a_reported_move_is_not_the_desks_call():
    """2026-10-06: a note that only reported GS falling was credited with
    a "negative catalyst watch"."""
    conn = _conn()
    _add(conn, 1, {"source": "Edmond de Rothschild", "key_insights": [], "market_movers": [
        {"ticker": "GS", "action": "negative_catalyst_watch",
         "rationale": "GS fell after management's comments on trading"}]}, [("GS", "Goldman")])
    with patch("db_parts.pdf._db.get_connection", return_value=conn):
        note = db.research_for_ticker("GS")[0]
    assert note["calls"] == []
    assert note["insights"] == ["(reported) GS fell after management's comments on trading"]

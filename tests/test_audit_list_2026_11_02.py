"""The rest of audit run 7's list (2026-11-02): A4, A13, A14, A18, A19, A20,
F1-F3, M12, M16, C1, and the light pulse's feed-down note."""
import json
from datetime import date
from unittest.mock import patch

from discord_bot import answer_block, data_footer, desk_claims, fantasy_odds, news_tool, revenue_yoy, slate_week


# --- A4
GS = {"source": "Goldman Sachs", "insights": [
    "Robinhood margin usage hit local highs while retail search interest sits near cycle lows"]}
A4 = ("→ Goldman's desk points to Robinhood margin usage hitting local highs while retail search "
      "interest sits near cycle lows, leaving the stock hypersensitive to sudden volume dry-ups "
      "and after-hours sweeps.")


def test_a_conclusion_the_note_does_not_contain_is_cut_and_the_rest_stays():
    out, cut = desk_claims.strip_unsupported(A4, [GS])
    assert out == ("→ Goldman's desk points to Robinhood margin usage hitting local highs while "
                   "retail search interest sits near cycle lows.")
    assert len(cut) == 1 and cut[0].startswith("leaving")


def test_a_consequence_the_note_does_say_stays():
    note = {"source": "Goldman Sachs", "insights": [
        "leaves the stock hypersensitive to sudden volume dry-ups and after-hours sweeps"]}
    assert desk_claims.strip_unsupported(A4, [note]) == (A4, [])


def test_a_decimal_inside_the_clause_does_not_split_it():
    text = "→ Goldman flags margin usage, leaving hypersensitive sudden sales near $1.2B of exposure."
    out, cut = desk_claims.strip_unsupported(text, [GS])
    assert out == "→ Goldman flags margin usage." and cut


def test_a_sentence_naming_no_desk_or_another_bank_is_untouched():
    plain = "→ Margin is high, leaving the stock hypersensitive to sudden volume dry-ups."
    assert desk_claims.strip_unsupported(plain, [GS]) == (plain, [])
    ms = "→ Morgan Stanley likes it, leaving hypersensitive sudden volume sweeps ahead."
    jpm = {"source": "JP Morgan", "insights": ["nothing relevant"]}
    assert desk_claims.strip_unsupported(ms, [jpm]) == (ms, [])


# --- A13
SNDK = {"next_quarter": {"revenue": 10.72e9, "revenue_year_ago": 2.31e9},
        "revenue_reports": [{"quarter": "2026-06-30", "revenue": 8.9e9, "yoy_pct": 300.0}]}
A13 = "→ Q1 revenue hit **$11.03B** (up **+23% y/y**, beating the **$10.72B** consensus)"


def test_a_wrong_yoy_on_a_fresh_print_is_replaced_from_the_year_ago_figure():
    out, changed = revenue_yoy.correct(A13, SNDK, fresh_print=True)
    assert "+377% y/y" in out and "+23%" not in out and "beating the **$10.72B**" in out
    assert changed


def test_a_consensus_row_is_ignored_when_nothing_just_printed():
    assert revenue_yoy.correct(A13, SNDK, fresh_print=False) == (A13, [])


def test_a_reported_quarter_matches_on_its_own_revenue_and_close_figures_stand():
    ok = "→ Revenue was **$8.9B**, up **+298% y/y**"
    assert revenue_yoy.correct(ok, SNDK) == (ok, [])
    wrong = "→ Revenue was **$8.9B**, up **+12% y/y**"
    assert "+300% y/y" in revenue_yoy.correct(wrong, SNDK)[0]
    down = "→ Revenue was **$8.9B**, down 12% y/y"
    assert "up +300% y/y" in revenue_yoy.correct(down, SNDK)[0]


def test_a_percentage_owned_by_another_metric_is_left_alone():
    for text in ("→ Revenue was **$8.9B**, while EPS grew 12% y/y",
                 "→ Sales of $8.9B, and margin up 4% yoy",
                 "→ Revenue of $8.9B on $2B of orders, up 4% y/y"):
        assert revenue_yoy.correct(text, SNDK) == (text, [])


# --- A14
ROWS = {"status": "ok", "date": "2026-10-20",
        "before_open": [{"symbol": "GE", "name": "General Electric Company", "market_cap_musd": 323000}],
        "after_close": [{"symbol": "NFLX", "name": "Netflix, Inc.", "market_cap_musd": 291000},
                        {"symbol": "SPCX", "name": "SpaceX", "market_cap_musd": 2000000},
                        {"symbol": "TINY", "name": "Tiny Inc.", "market_cap_musd": 500}]}


def _day(question="", morning=False):
    out = {**ROWS, "week_outline": slate_week.week_outline([ROWS], per_session=12)}
    return answer_block.build("lookup_earnings_slate", out, date(2026, 10, 20),
                              morning_reported=morning, question=question)


def test_a_day_slate_is_rendered_in_code_with_its_date():
    lines = _day()["lines"]
    assert lines[0].startswith("→ **Tue Oct 20, before the open:** General Electric")
    assert "Netflix (NFLX)" in lines[1] and "SpaceX (SPCX)" in lines[1] and "TINY" not in lines[1]


def test_after_the_open_a_plain_question_skips_the_morning_but_one_about_it_keeps_it():
    assert len(_day(morning=True)["lines"]) == 1
    keep = _day("who reported this morning", morning=True)["lines"]
    assert len(keep) == 1 and "before the open" in keep[0]
    assert "after the close" in _day("who reports after the close today")["lines"][0]


def test_a_small_cap_day_lists_its_names_and_a_week_still_says_none():
    small = {"status": "ok", "date": "2026-10-20", "before_open": [],
             "after_close": [{"symbol": "TINY", "name": "Tiny Inc.", "market_cap_musd": 500}]}
    out = {**small, "week_outline": slate_week.week_outline([small])}
    assert "Tiny (TINY)" in answer_block.build(
        "lookup_earnings_slate", out, date(2026, 10, 20))["lines"][0]
    assert "nothing above" in answer_block.build(
        "lookup_earnings_slate", {**out, "dates": ["2026-10-20"]}, date(2026, 10, 20))["lines"][0]


# --- A18
SLS = {"title": "tikr.com"}


def test_a_publisher_with_no_link_behind_it_is_dropped():
    line = "2026-10-28: REGAL trial hit its 80th event (GlobeNewswire)"
    assert news_tool.checked_publishers([line], [SLS]) == ["2026-10-28: REGAL trial hit its 80th event"]
    ok = "2026-10-28: Valuation note (TIKR)"
    assert news_tool.checked_publishers([ok], [SLS]) == [ok]
    assert news_tool.checked_publishers([line], []) == [line], "no links, nothing to compare"
    bare = "2026-10-28: REGAL trial (Phase 3) hit its event"
    assert news_tool.checked_publishers([bare], [SLS]) == [bare]
    figure = "2026-10-28: Revenue beat consensus ($4.2B)"
    assert news_tool.checked_publishers([figure], [SLS]) == [figure]


# --- A19
def test_a_year_span_to_present_reads_to():
    from discord_bot import bot
    out, _ = bot._clean_voice_violations("→ Three stints (1996–99, 2009–21, and 2022–present) as prime minister")
    assert "2022 to present" in out and "1996–99" in out
    clause, _ = bot._clean_voice_violations("→ EPS $5 — now guided higher, and in 2026 – today's print")
    assert " to now" not in clause and " to today" not in clause


# --- A20
def test_the_models_data_line_goes_even_as_an_arrow():
    text = "→ Free cash flow **$136B**\n\n→ Data: company reports (Business Quant)"
    assert data_footer.strip_model_sources(text) == "→ Free cash flow **$136B**"
    assert data_footer.strip_model_data(text) == "→ Free cash flow **$136B**"
    block = "→ a\n\nSources:\n[1] [x.com](<https://x.com>)"
    assert data_footer.strip_model_data(block) == block, "a Sources block with no system footer stays"


# --- F1-F3
ASK = "how do these odds change if you’re in a 12-man league and top 8 teams make playoffs"
TABLE = 'Odds of making the fantasy playoffs:\n\n7\\-0: 98\\.4%\n6\\-1: 92\\.0%\n\n[BK]\n' + ASK


def test_the_format_question_is_computed_and_matches_a_simulation():
    spec = fantasy_odds.parse(ASK, TABLE)
    assert spec["teams"] == 12 and spec["spots"] == 8 and spec["games"] == 14
    assert spec["records"][0] == (7, 0) and spec["records"][-1] == (0, 7)
    assert abs(fantasy_odds.odds(12, 8, 14, 4, 3) - 0.82) < 0.04
    assert abs(fantasy_odds.odds(12, 8, 14, 3, 4) - 0.59) < 0.04
    assert abs(fantasy_odds.odds(12, 8, 14, 2, 5) - 0.33) < 0.04
    text = fantasy_odds.answer(spec)
    assert "**4-3:**" in text and "coin flip" in text


def test_the_odds_are_a_probability_that_sums_to_the_spots():
    """Across a full league the chances must add up to the playoff spots."""
    from math import comb
    total = sum(comb(14, w) / 2 ** 14 * fantasy_odds.odds(12, 8, 14, w, 14 - w) for w in range(15))
    assert abs(total * 12 - 8) < 1e-6


def test_other_questions_stay_with_the_model():
    assert fantasy_odds.parse("what are my playoff odds") is None
    assert fantasy_odds.parse("in a 12-man league who wins") is None
    assert fantasy_odds.parse("top 8 teams make playoffs, odds in a 8-team league") is None


def test_games_played_is_not_the_season_and_byes_are_not_spots():
    spec = fantasy_odds.parse("playoff odds at 4-3 after 7 games in a 12-team league, top 8 make it")
    assert spec["games"] == 14
    assert fantasy_odds.parse("playoff odds in a 15-game season 12-team league top 6")["games"] == 15
    assert fantasy_odds.parse("playoff odds in a 12-team league where the top 2 get a bye") is None


def test_a_named_record_is_answered_alone():
    spec = fantasy_odds.parse("odds of making playoffs at 5-3 in a 10-team league, top 4 make it")
    assert spec["records"] == [(5, 3)]


# --- M12, M16
def _guards(msgs):
    import scripts.profile_guards as G
    rows = [{"norm": G._norm(m), "posted_at": "2026-10-20T00:00:00", "id": i, "content": m}
            for i, m in enumerate(msgs)]
    return G, rows


def test_a_voice_quote_in_no_message_goes():
    G, rows = _guards(["real line from the member here"])
    profile = ("**Voice.**\n- \"I could have shot you once\"\n- \"real line from the member here\"")
    with patch.object(G, "member_messages", return_value=rows), patch.object(G, "_known_names", return_value=set()), \
            patch.object(G, "is_copypasta", return_value=False), patch.object(G, "_context", return_value=(None, [])):
        out, dropped = G.apply(profile, 1)
    assert "shot you" not in out and "real line" in out
    assert any("quote in no message" in d for d in dropped)


def test_a_rationale_detail_the_member_never_wrote_goes_and_ledger_figures_stay():
    G, rows = _guards(["i hit a three leg parlay"])
    text = "Hits a nine-leg parlay weekly. Closes average +150% on a three-leg parlay."
    with patch.object(G, "member_messages", return_value=rows), patch.object(G, "_known_names", return_value=set()):
        out, dropped = G.check_rationale(text, 1)
    assert out == "Closes average +150% on a three-leg parlay."
    assert "nine-leg" in dropped[0]
    with patch.object(G, "member_messages", return_value=rows), patch.object(G, "_known_names", return_value=set()):
        assert G.check_rationale("Hits a nine-leg parlay weekly.", 1)[0] == ""


def test_the_feed_down_note_obeys_the_voice_rules():
    import re
    from ai_analysis.voice_rules import compose_lint_patterns
    import scripts.omnipulse_body as O
    for rx, kind in compose_lint_patterns():
        assert not re.search(rx, O.FEED_DOWN_NOTE, re.I), (kind, rx)


# --- C1
def test_one_class_per_company_keeps_the_larger():
    from discord_bot import listed_symbols
    from report.calendar_data import one_class_per_company
    names = {"GOOGL": "Alphabet Inc.", "GOOG": "Alphabet Inc.", "FOXA": "Fox Corporation",
             "FOX": "Fox Corporation", "AAPL": "Apple Inc.", "NEW": ""}
    with patch.object(listed_symbols, "name", side_effect=lambda s: names.get(s, "")):
        assert one_class_per_company(["GOOGL", "AAPL", "GOOG", "FOXA", "FOX", "NEW"]) == \
            ["GOOGL", "AAPL", "FOXA", "NEW"]


# --- the light pulse
def test_the_light_note_says_the_feed_is_down_when_polls_are_failing(tmp_path):
    import scripts.omnipulse_body as O
    import scripts.omnipulse_streak as S
    import scripts.pulse_driver as PD
    d = PD.Driver(tmp_path)
    (tmp_path / "ctx.json").write_text(json.dumps(
        {"today": "2026-11-02", "theme_map": {}, "no_new_research": True,
         "research_feed_down": True}), encoding="utf-8")
    with patch.object(O, "ENABLED", True):
        assert d.gate_omnipulse("2026-11-02") == "LIGHT"
    body = (tmp_path / "omnipulse_body.md").read_text(encoding="utf-8")
    assert O.FEED_DOWN_NOTE in body and "No new bank research" not in body
    reason = (tmp_path / "light_reason.txt").read_text(encoding="utf-8")
    assert reason.lower().startswith(S.NEUTRAL_REASON)


def test_the_feed_flag_needs_two_failed_polls():
    from scheduler import jobs as J
    with patch.object(J, "_poll_failures", 1):
        assert J.dropbox_polls_failing() is False
    with patch.object(J, "_poll_failures", 2):
        assert J.dropbox_polls_failing() is True

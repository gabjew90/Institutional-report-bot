"""The result of a report, rendered in code (2026-11-13). The figures are
AMAT's from the morning after its 2026-11-12 report: Yahoo's earnings
dates and quarterly revenue, the calendar feed's reported revenue."""
from datetime import date

from discord_bot import print_block as P
from discord_bot import stock_outline as O
from discord_bot import ask_router as R
from report import ticker_snapshot as S

HIST = [{"date": "2027-02-11", "session": "after the close", "epsActual": None, "epsEstimate": 4.26},
        {"date": "2026-11-12", "session": "after the close", "epsActual": 4.16, "epsEstimate": 4.05,
         "surprisePercent": 0.026},
        {"date": "2026-08-13", "session": "after the close", "epsActual": 3.50, "epsEstimate": 3.39},
        {"date": "2026-05-14", "session": "after the close", "epsActual": 2.86, "epsEstimate": 2.69},
        {"date": "2026-02-12", "session": "after the close", "epsActual": 2.38, "epsEstimate": 2.21},
        {"date": "2025-11-13", "session": "after the close", "epsActual": 2.17, "epsEstimate": 2.09}]
QREV = [("2026-07-31", 9.115e9), ("2026-04-30", 7.910e9), ("2026-01-31", 7.012e9),
        ("2025-10-31", 6.800e9), ("2025-07-31", 7.302e9)]
TODAY = date(2026, 11, 13)
NEWS = {"status": "ok", "digest": (
    "- 2026-11-12: Applied Materials reported fourth-quarter fiscal 2026 revenue of $10.41 billion (up 53% "
    "year-over-year) beating the $10.30 billion estimate, and non-GAAP EPS of $4.16 (up 92% year-over-year) "
    "beating the $4.06 estimate; guided fiscal first-quarter revenue to $11.10 billion (plus or minus $500 "
    "million) and non-GAAP EPS to $4.35 (plus or minus $0.20) against estimates, with shares moving lower "
    "following the release (Investing.com)\n"
    "- 2026-11-12: HSBC maintained a Buy rating on Applied Materials and set a price target of $641")}
PRICE = {"status": "ok", "session": "REGULAR", "quotes": [
    {"symbol": "AMAT", "price": 384.16, "change_pct": -3.6}]}


def _fresh(feed=10.409e9, qrev=QREV, rev_est=None, eps_est=None):
    g = S.growth_block(rev_est or {}, eps_est or {}, HIST, qrev, today=TODAY, feed_revenue=feed)
    return R.fresh_print({"symbol": "AMAT", "growth": g}, today=TODAY), g


def test_the_last_report_carries_the_year_ago_eps_and_the_quarter_revenue():
    fresh, g = _fresh()
    last = g["last_report"]
    assert last["eps_year_ago"] == 2.17
    assert last["revenue"] == 10.409e9 and last["revenue_source"] == "calendar"
    assert last["revenue_year_ago"] == 6.800e9, "the October quarter a year before, not July"
    assert "eps_year_ago" not in g["eps_reports"][0], "the report rows keep their shape"
    assert fresh["revenue"] == 10.409e9


def test_yahoos_own_quarter_wins_once_it_is_listed():
    later = [("2026-10-31", 10.41e9)] + QREV
    _, g = _fresh(feed=9.9e9, qrev=later)
    assert g["last_report"]["revenue"] == 10.41e9 and g["last_report"]["revenue_source"] == "yahoo"
    assert g["last_report"]["revenue_year_ago"] == 6.800e9


def test_a_feed_figure_in_another_unit_or_with_no_feed_gives_no_revenue():
    assert "revenue" not in _fresh(feed=None)[1]["last_report"]
    # 1.4 trillion won against 9 billion dollars is not growth
    assert "revenue" not in _fresh(feed=1.4e12)[1]["last_report"]
    assert S.print_revenue("not a date", QREV, 1e9) == {}


def test_the_reported_quarters_consensus_is_kept_while_yahoo_still_lists_it():
    """Hours after a print '0q' is still the reported quarter: its revenue
    estimate and year-ago revenue come from the same row."""
    rev = {"0q": {"avg": 10.30e9, "yearAgoRevenue": 6.80e9, "growth": 0.51}}
    eps = {"0q": {"avg": 4.05, "yearAgoEps": 2.17, "growth": 0.87}}
    _, g = _fresh(rev_est=rev, eps_est=eps)
    assert "next_quarter" not in g and g["last_report"]["revenue_estimate"] == 10.30e9


def test_the_lines_are_the_price_the_eps_the_revenue_and_the_guidance():
    fresh, g = _fresh()
    block = P.build("AMAT", fresh, {"status": "ok", "growth": g}, PRICE, NEWS)
    assert block["lines"] == [
        "→ **AMAT** at **$384.16**, -3.6% today.",
        "→ Reported Nov 12 after the close: EPS **$4.16** beat the **$4.05** estimate by **2.6%** "
        "and rose **91.7%** from a year earlier.",
        "→ Revenue **$10.41B** beat the **$10.30B** estimate cited by Investing.com "
        "and rose **53.1%** from a year earlier.",
        "→ Guided fiscal first-quarter revenue to **$11.10 billion** (±$500 million) and non-GAAP EPS "
        "to **$4.35** (±$0.20) against estimates (Investing.com).",
    ]
    assert block["has_price"] and block["eps"] == 4.16 and block["price"] == 384.16


def test_without_a_quote_the_eps_line_names_the_stock_and_without_news_revenue_stands_alone():
    fresh, g = _fresh()
    block = P.build("AMAT", fresh, {"status": "ok", "growth": g})
    assert block["lines"] == [
        "→ **AMAT** reported Nov 12 after the close: EPS **$4.16** beat the **$4.05** estimate by "
        "**2.6%** and rose **91.7%** from a year earlier.",
        "→ Revenue **$10.41B**, up **53.1%** from a year earlier."]
    assert not block["has_price"]


def test_no_block_without_the_printed_eps_or_the_revenue():
    fresh, g = _fresh(feed=None)
    assert P.build("AMAT", fresh, {"growth": g}, PRICE, NEWS) is None
    assert P.build("AMAT", {"date": "2026-11-12", "actual": None, "estimate": 4.05, "revenue": 1e9}) is None
    assert P.build("AMAT", None) is None


def test_a_miss_a_match_and_a_loss_read_as_what_they_are():
    miss = {"date": "2026-10-29", "session": "before the open", "actual": -0.42, "estimate": -0.30,
            "eps_year_ago": -0.10}
    assert P.eps_line(miss, "RIVN") == (
        "→ **RIVN** reported Oct 29 before the open: EPS **-$0.42** missed the **-$0.30** estimate by "
        "**40.0%**, against **-$0.10** a year earlier.")
    assert "matched the **$1.00** estimate." in P.eps_line({"date": "2026-10-29", "actual": 1.0, "estimate": 1.0})
    down = P.revenue_line({"revenue": 9e8, "revenue_year_ago": 1e9, "revenue_estimate": 9.5e8})
    assert down == "→ Revenue **$900.0M** missed the **$950.0M** estimate by **5.3%** and fell **10.0%** from a year earlier."
    assert P.revenue_line({"revenue": 9e8}) == "→ Revenue **$900.0M**."


def test_a_news_estimate_must_sit_beside_an_estimate_word_and_the_feeds_revenue():
    rev = 10.409e9
    ok = ["- 2026-11-12: revenue of $10.41B beating the consensus estimate of $10.3B (MLQ.ai)"]
    assert P.news_revenue_estimate(ok, rev) == (10.3e9, "$10.3B", "MLQ.ai")
    assert P.news_revenue_estimate(["revenue of $10.41 billion versus $10.30 billion expected"], rev)[1] == "$10.30B"
    # the year-ago figure is not an estimate, even with one mentioned after it
    assert P.news_revenue_estimate(
        ["revenue of $10.41 billion, up from $9.80 billion a year ago, beating estimates"], rev) is None
    # another revenue figure than the feed's: the line is about something else
    assert P.news_revenue_estimate(["revenue of $7.6 billion beating the $7.4 billion estimate"], rev) is None
    # an EPS estimate has no unit and is never taken for the revenue's
    assert P.news_revenue_estimate(["revenue of $10.41 billion and EPS of $4.16 beating the $4.06 estimate"],
                                   rev) is None
    assert P.news_revenue_estimate(["EPS of $4.16"], rev) is None


def test_the_outline_asks_only_for_what_the_lines_do_not_carry():
    fresh, g = _fresh()
    results = {R.T_PRICE: PRICE, R.T_SNAPSHOT: {"status": "ok", "symbol": "AMAT", "growth": g},
               R.T_NEWS: NEWS,
               R.T_PRIMER: {"status": "ok", "primer": "DRIVERS: Semiconductor Systems drives revenue growth "
                                                      "on AI chip demand\nWATCHED: gross margin"}}
    block = P.build("AMAT", fresh, results[R.T_SNAPSHOT], PRICE, NEWS)
    text = O.build_outline(results, "ticker_opinion", "AMAT", fresh=fresh, posted=block)
    assert all(ln in text for ln in block["lines"])
    slots = [ln.split(". ", 1)[1].split(":")[0] for ln in text.splitlines() if ln[:2].rstrip(".").isdigit()]
    assert slots == ["DRIVER", "ANALYST ACTION"]
    assert "RESULT:" not in text and "GUIDANCE:" not in text and "PRICE:" not in text
    # nothing left to outline: still one arrow asked for, and the lines shown
    bare = O.build_outline({R.T_SNAPSHOT: results[R.T_SNAPSHOT]}, "ticker_opinion", "AMAT",
                           fresh=fresh, posted=block)
    assert "Write one arrow: the part of the business" in bare and block["lines"][1] in bare


def test_compose_posts_the_lines_first_and_drops_an_arrow_that_restates_them():
    fresh, g = _fresh()
    block = P.build("AMAT", fresh, {"status": "ok", "growth": g}, PRICE, NEWS)
    driver = "→ Semiconductor Systems drove the growth on demand for AI chip equipment"
    hsbc = "→ HSBC kept its Buy rating with a **$641** target"
    answer = ("→ **$AMAT** closed at **$384.16**, down 3.6%\n\n"
              "→ EPS of **$4.16** beat estimates\n\n" + driver + "\n\n" + hsbc)
    out, dropped = P.compose(answer, block)
    assert out == "\n\n".join(block["lines"] + [driver, hsbc]) and dropped == 2
    # the bot's own no-answer line is not an arrow to keep
    assert P.compose("→ No response came back (reason: MAX_TOKENS). Try again or rephrase.", block) == (
        "\n\n".join(block["lines"]), 0)


def test_a_figure_that_shares_digits_with_the_eps_is_not_the_eps():
    assert P._has_figure("EPS of **$1.40**.", 1.40) and P._has_figure("earned $1.4 a share", 1.40)
    assert not P._has_figure("AI orders reached $1.4B, up 1.4%", 1.40)
    assert not P._has_figure("a $14.05 target", 4.05) and P._has_figure("at $1,065.11 today", 1065.11)


def test_the_print_guard_stands_down_when_the_lines_are_posted():
    from discord_bot import bot
    fresh, _ = _fresh()
    meta = {"guards": [], "route_shape": "ticker_opinion", "route_prefetch": [], "fresh_print": fresh,
            "print_block": {"lines": ["x"]}}
    ans = "→ Semiconductor Systems drove **$7.6B** of it"
    assert bot._fresh_print_guard(ans, meta) == ans and meta["guards"] == []
    meta.pop("print_block")
    assert bot._fresh_print_guard(ans, meta) != ans


def test_a_year_ago_eps_on_another_share_count_is_not_used():
    """Yahoo's estimate table is on today's share count. When its year-ago
    EPS matches none of the older reports, the report dates carry pre-split
    figures and a growth rate from them would be the split, not the business."""
    assert S.year_ago_eps(HIST, "2026-11-12") == 2.17
    assert S.year_ago_eps(HIST, "2026-11-12", basis_check=2.38) == 2.17, "the February report's 2.38"
    assert S.year_ago_eps(HIST, "2026-11-12", basis_check=0.238) is None
    _, g = _fresh(eps_est={"0q": {"avg": 0.43, "yearAgoEps": 0.238, "growth": 0.8}})
    assert "eps_year_ago" not in g["last_report"]


def test_the_calendar_feed_is_read_only_for_a_fresh_report_yahoo_has_no_revenue_for(monkeypatch):
    from report import news_data
    calls = []

    def fake(sym, timeout=8.0):
        calls.append((sym, timeout))
        return {"last": {"date": "2026-11-12", "revenue_actual": 10409000000}}
    monkeypatch.setattr(news_data, "fetch_earnings_date_for_symbol", fake)
    monkeypatch.setattr(S, "_recent", lambda d, today=None, days=10: str(d)[:10] == "2026-11-12")
    assert S._feed_print_revenue("AMAT", HIST, QREV, {"financialCurrency": "USD"}) == 10409000000
    assert calls == [("AMAT", S.FEED_TIMEOUT_S)]
    # Yahoo already lists the quarter, a foreign filer, an old report, no time left: no call
    assert S._feed_print_revenue("AMAT", HIST, [("2026-10-31", 1.04e10)] + QREV, {}) is None
    assert S._feed_print_revenue("TSM", HIST, QREV, {"financialCurrency": "TWD"}) is None
    assert S._feed_print_revenue("AMAT", HIST[2:], QREV, {}) is None
    assert S._feed_print_revenue("AMAT", HIST, QREV, {}, started=-1e9) is None
    assert len(calls) == 1
    # the feed's row is for another report
    monkeypatch.setattr(news_data, "fetch_earnings_date_for_symbol",
                        lambda sym, timeout=8.0: {"last": {"date": "2026-08-13", "revenue_actual": 9e9}})
    assert S._feed_print_revenue("AMAT", HIST, QREV, {}) is None


def test_a_stale_quarterly_list_gives_no_feed_revenue():
    """Yahoo's newest quarter must be the one before the report's own, or
    the year-ago quarter chosen by date belongs to a different quarter."""
    assert S.print_revenue("2026-11-12", QREV[1:], 10.409e9) == {}
    assert S.print_revenue("2026-11-12", QREV, 10.409e9)["revenue_year_ago"] == 6.800e9

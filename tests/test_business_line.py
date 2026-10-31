"""The business-line check on stock answers (2026-09-30): the ACN answer
named book-to-bill and the implied move and never said what Accenture
sells. Primers and answers below are the live ones from that night."""
from discord_bot import business_line as B

MU = ("SELLS: Micron Technology sells memory and storage chips to data center operators, "
      "smartphone makers and automotive companies.\n"
      "SEGMENTS: Its reported segments are Cloud Memory (approximately 30% of revenue, "
      "high-performance memory for cloud and AI), Core Data Center (approximately 20% of "
      "revenue, server memory products), Mobile & Client (approximately 38% of revenue, chips "
      "for smartphones and PCs), and Automotive & Embedded (approximately 12% of revenue).\n"
      "DRIVERS: Cloud Memory and Core Data Center drive revenue growth now.")
ACN = ("SELLS: Accenture sells information technology consulting and outsourcing services.\n"
       "SEGMENTS: Consulting: approximately 50% of revenue, providing strategy and digital "
       "transformation; Outsourcing: approximately 50% of revenue, managing ongoing business "
       "processes.\n"
       "DRIVERS: Consulting drives revenue growth, while Outsourcing carries steady margins.")
NVDA = ("SELLS: NVIDIA designs and sells graphics processing units.\n"
        "SEGMENTS: Data Center (approximately 90% of revenue) includes GPUs and networking; "
        "Graphics (approximately 10% of revenue) includes gaming GPUs.")
AEVA = ("SELLS: Frequency Modulated Continuous Wave light detection and ranging (LiDAR) sensors "
        "to automotive and industrial robotics customers.\n"
        "SEGMENTS: Operates as a single reportable segment, 100% of revenue.")
ACN_ANSWER = ("→ Accenture reports **today before market open** with consensus revenue at "
              "**$18.2B** and EPS at **$3.21**.\n\n→ Options are pricing a **7.1%** ($13.10) "
              "move.\n\n→ Key battlegrounds for the print are book-to-bill quality and the FY27 "
              "revenue guide.")


def test_segment_names_come_out_of_the_live_primers():
    assert B.segment_names(B._line(MU, "SEGMENTS")) == [
        "Cloud Memory", "Core Data Center", "Mobile & Client", "Automotive & Embedded"]
    assert B.segment_names(B._line(ACN, "SEGMENTS")) == ["Consulting", "Outsourcing"]
    assert B.segment_names(B._line(NVDA, "SEGMENTS")) == ["Data Center", "Graphics"]
    assert B.segment_names(B._line(AEVA, "SEGMENTS")) == []


def test_terms_exclude_the_company_and_cover_a_one_segment_company():
    assert "nvidia" not in B.business_terms(NVDA)
    assert {"lidar", "sensors"} <= B.business_terms(AEVA)
    assert {"cloud memory", "memory"} <= B.business_terms(MU)


def test_the_live_acn_answer_names_no_business_line():
    assert not B.names_business_line(ACN_ANSWER, ACN)
    assert B.names_business_line("→ Consulting, half the revenue, is where AI projects land", ACN)
    assert B.names_business_line("Data Center revenue drove the beat", NVDA)
    assert not B.names_business_line("NVIDIA beat estimates", NVDA)
    assert B.names_business_line("anything", "")          # no primer, nothing to check


def _run(answer, shape, primer=ACN):
    from discord_bot import bot
    meta = {"guards": [], "route_shape": shape, "primer": {"symbol": "ACN", "primer": primer}}
    return bot._business_line_guard(answer, meta), meta["guards"]


def test_an_answer_that_skips_the_business_is_recorded_not_rewritten_2026_10_30():
    """Audit A10/A11: the rewrite put a false cause on a GOOGL miss and
    garbled RDDT. The check now only records the miss."""
    out, guards = _run(ACN_ANSWER, "options_chain")
    assert out == ACN_ANSWER and guards == ["business-line:missing"]


def test_the_named_line_must_be_the_driver_the_primer_names():
    """Owner, 2026-10-01: 'driven by' should name the highlight, not any segment."""
    assert B.driver_terms(MU) >= {"cloud memory", "core data center"}
    assert not B.names_business_line("Mobile & Client chips shipped 3% more", MU)
    assert B.names_business_line("Cloud Memory, AI server chips, carried the beat", MU)
    assert B.names_business_line("Graphics was flat", NVDA), "no DRIVERS line: any segment counts"


ACN_REGIONS = (
    "SELLS: Accenture sells consulting, technology and managed services.\n"
    "SEGMENTS: Americas: approximately 49% of revenue; EMEA: approximately 37% of revenue; "
    "Asia Pacific: approximately 14% of revenue.\n"
    "DRIVERS: Growth is currently driven by enterprise demand for artificial intelligence "
    "transformations, cloud migrations, and digital core modernizations, while margins remain "
    "relatively balanced across geographic markets.")


def test_region_segments_take_the_driver_line_words_not_a_region():
    """ACN reports by region; the highlight is the work, not the Americas."""
    terms = B.driver_terms(ACN_REGIONS)
    assert {"transformations", "migrations"} <= terms and "americas" not in terms
    assert B.names_business_line("bookings from AI transformation work are the tell", ACN_REGIONS)
    assert not B.names_business_line("the Americas drove the quarter", ACN_REGIONS)


def _run_move(answer, move=None, shape="options_chain", earnings=True):
    from discord_bot import bot
    meta = {"guards": [], "route_shape": shape,
            "route_prefetch": ["lookup_options_chain"] + (["lookup_earnings_date"] if earnings else []),
            "chain_move": move or {"symbol": "ACN", "pct": 7.1, "dollars": 13.1,
                                   "expiration": "2026-10-02"}}
    return bot._implied_move_guard(answer, meta), meta["guards"]


def test_an_options_answer_without_the_priced_move_gets_a_line_from_the_chain():
    no_move = ("→ ACN reports today, consensus revenue **$18.2B** (+2.5% y/y).\n\n"
               "→ Goldman expects an in-line quarter.")
    out, guards = _run_move(no_move)
    assert out == no_move + ("\n\n→ Options are pricing a move of about **7.1%** (**$13.10**) "
                             "either way by the Oct 2 expiration, from the at-the-money straddle.")
    assert guards == ["implied-move"]


def test_the_move_check_is_satisfied_by_percent_or_dollars_and_skips_other_shapes():
    for ans in ("→ Options price a ±7.1% move, revenue $18.2B",
                "→ the straddle prices $13.10 either way, revenue $18.2B",
                "→ options price roughly 7% either way, revenue $18.2B"):
        assert _run_move(ans) == (ans, [])
    assert _run_move("→ revenue $18.2B", shape="ticker_opinion")[0] == "→ revenue $18.2B"
    assert _run_move("→ revenue $18.2B", move={"pct": None})[0] == "→ revenue $18.2B"
    assert _run_move("→ open interest 12,400 at $200", earnings=False)[0] == \
        "→ open interest 12,400 at $200", "not a bet on the print"


def test_a_fallback_message_gets_no_added_line():
    fallback = "→ Thought myself in circles and ran out of room. Try asking it more directly."
    assert _run(fallback, "ticker_opinion") == (fallback, [])
    assert _run_move(fallback)[0] == fallback


def test_the_guard_skips_other_shapes_answers_that_already_name_it_and_missing_primers():
    assert _run(ACN_ANSWER, "price") == (ACN_ANSWER, [])
    assert _run("Consulting carries the growth, $18.2B", "ticker_opinion")[1] == []
    assert _run(ACN_ANSWER, "ticker_opinion", primer="") == (ACN_ANSWER, [])

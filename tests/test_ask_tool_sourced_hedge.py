"""A tool payload is a source (2026-09-17).

2026-09-15 12:15 UTC: a LEN earnings answer whose date, consensus EPS
and revenue all came from lookup_earnings_date shipped with the
"Couldn't verify" hedge, because the grounding net asked only whether
Google had sources. The net now accepts an ungrounded answer when a data
tool returned data and every figure in the answer is in what the turn
saw."""
import inspect

from discord_bot import bot as B

LEN_ANSWER = ("→ **LEN** reports Q3 earnings tomorrow **September 16 AMC**, with the street "
              "consensus looking for **$1.32** EPS and **$8.39B** in revenue\n"
              "→ Order velocity and gross margins take center stage")
LEN_PAYLOAD = ('{"symbol": "LEN", "date": "2026-09-16", "hour": "amc", "epsEstimate": 1.32, '
               '"revenueEstimate": 8390000000, "quarter": 3, "year": 2026}')
OK_TRACE = [{"tool": "lookup_earnings_date", "args": {"symbol": "LEN"}, "status": "ok", "result_chars": 200}]


def test_an_answer_built_on_a_tool_payload_is_sourced():
    assert B._tool_sourced(LEN_ANSWER, OK_TRACE, LEN_PAYLOAD)


def test_a_figure_the_payload_lacks_keeps_the_hedge_path():
    assert not B._tool_sourced(LEN_ANSWER.replace("$8.39B", "$9.10B"), OK_TRACE, LEN_PAYLOAD)


def test_a_failed_or_empty_trace_is_not_a_source():
    failed = [{"tool": "lookup_earnings_date", "status": "no_data", "result_chars": 0}]
    assert not B._tool_sourced(LEN_ANSWER, failed, LEN_PAYLOAD)
    assert not B._tool_sourced(LEN_ANSWER, [], LEN_PAYLOAD)


def test_an_answer_with_no_figures_is_not_claimed_as_sourced():
    assert not B._tool_sourced("→ Margins take center stage this quarter.", OK_TRACE, LEN_PAYLOAD)


def test_the_net_consults_tool_sourcing_before_it_hedges():
    src = inspect.getsource(B._ask_07_validation_ladder)
    i_tool = src.index('"in-voice:tool-sourced"')
    assert i_tool < src.index('"hedged(local-skip)"')
    assert i_tool < src.index('"hedged(context-dep-skip)"')
    assert "_tool_sourced(answer, _ask_tool_trace" in src

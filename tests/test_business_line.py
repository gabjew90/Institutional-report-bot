"""The business-line check on stock answers (2026-09-30): the ACN answer
named book-to-bill and the implied move and never said what Accenture
sells. Primers and answers below are the live ones from that night."""
import asyncio
import types

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


class _Client:
    def __init__(self, text):
        self.calls = 0

        async def gen(**kw):
            self.calls += 1
            return types.SimpleNamespace(text=text)
        self.aio = types.SimpleNamespace(models=types.SimpleNamespace(generate_content=gen))


def _run(answer, shape, client, primer=ACN):
    from discord_bot import bot
    from google.genai import types as gt
    meta = {"guards": [], "route_shape": shape, "primer": {"symbol": "ACN", "primer": primer}}
    out = asyncio.run(bot._business_line_guard(answer, meta, client, "m", None, gt, lambda r: None))
    return out, meta["guards"]


def test_the_guard_rewrites_a_stock_answer_that_skips_the_business():
    good = ACN_ANSWER + ("\n\n→ Half of Accenture's revenue is Consulting, the AI-transformation "
                         "work that drives growth, and the other half is Outsourcing.")
    c = _Client(good)
    out, guards = _run(ACN_ANSWER, "options_chain", c)
    assert out == good and guards == ["business-line"] and c.calls == 1


def test_a_rewrite_that_drops_a_figure_or_still_skips_the_business_is_discarded():
    dropped = "→ Accenture's Consulting arm reports today."              # lost $18.2B etc.
    out, guards = _run(ACN_ANSWER, "ticker_opinion", _Client(dropped))
    assert out == ACN_ANSWER and guards[-1] == "business-line:kept-original"
    out, _ = _run(ACN_ANSWER, "ticker_opinion", _Client(ACN_ANSWER))
    assert out == ACN_ANSWER


def test_a_fallback_message_is_never_rewritten_into_a_primer_recital():
    c = _Client("→ Accenture is half Consulting and half Outsourcing.")
    fallback = "→ Thought myself in circles and ran out of room. Try asking it more directly."
    assert _run(fallback, "ticker_opinion", c)[0] == fallback and c.calls == 0


def test_the_rewrite_gets_the_voice_cleanup_and_may_spell_out_units():
    spelled = ("→ Accenture reports today before market open with consensus revenue at $18.2 "
               "billion and EPS at $3.21 — Consulting, half of revenue, carries the growth.\n\n"
               "→ Options are pricing a 7.1% ($13.10) move.")
    out, guards = _run(ACN_ANSWER, "ticker_opinion", _Client(spelled))
    assert guards == ["business-line"], "spelled-out units keep every figure"
    assert "—" not in out and "Consulting" in out


def test_the_guard_skips_other_shapes_answers_that_already_name_it_and_missing_primers():
    c = _Client("x")
    assert _run(ACN_ANSWER, "price", c)[0] == ACN_ANSWER
    assert _run("Consulting carries the growth", "ticker_opinion", c)[0] == "Consulting carries the growth"
    assert _run(ACN_ANSWER, "ticker_opinion", c, primer="")[0] == ACN_ANSWER
    assert c.calls == 0
